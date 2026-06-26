"""Service for parsing sale messages using OpenRouter LLM."""

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from time import monotonic

from asgiref.sync import sync_to_async
from django.db import close_old_connections

from apps.catalog.models import Product
from apps.sales.services import ParsedSaleItem
from services.openrouter import OpenRouterClient
from utils.timing import track

logger = logging.getLogger(__name__)

_PRODUCT_CACHE_TTL_SECONDS = 60
_PRODUCT_CACHE: dict[int | None, tuple[float, list[Product]]] = {}


def _apply_rule_based_price_hints(
    message: str,
    parsed_items: list[ParsedSaleItem],
    detected_currency: str | None,
) -> tuple[list[ParsedSaleItem], str | None]:
    """Correct per-unit vs total-price ambiguity using simple heuristics.

    When quantity > 1 and no per-unit marker is present (each, per, @, imwe, etc.),
    treat the price as a total rather than a unit price and clear unit_price so the
    LLM result doesn't silently multiply it again downstream.
    """
    if not message or not parsed_items:
        return parsed_items, detected_currency

    per_unit_tokens = (
        r"\beach\b",
        r"\bone\b",
        r"\bevery\b",
        r"\bindividual\b",
        r"\bevery\s+single\b",
        r"\bone\s+by\s+one\b",
        r"\bper\b",
        r"\bapiece\b",
        r"\brespectively\b",
        r"\bany\s+one\b",
        r"\bparticular\b",
        r"\bseparate\b",
        r"\bimwe\b",     # Shona: one/each (Class 9)
        r"\brimwe\b",    # Shona: one/each (Class 5)
        r"\bchimwe\b",   # Shona: one/each (Class 7)
        r"\bhumwe\b",    # Shona: one/each (Class 14)
        r"\bmumwe\b",    # Shona: one/each (Class 1/3)
        r"\brumwe\b",    # Shona: one/each (Class 11)
        r"\bkamwe\b",    # Shona: one/each (Class 12)
        r"\bumwe\b",     # Shona: one/each (Class 8)
    )
    per_unit_pattern = re.compile(r"(?:" + "|".join(per_unit_tokens) + r")", re.IGNORECASE)
    has_per_unit_marker = (
        bool(per_unit_pattern.search(message))
        or "@" in message
        or bool(re.search(r"\d[×x]\d", message))  # e.g. $1×16 or $1x16
    )

    adjusted: list[ParsedSaleItem] = []
    for item in parsed_items:
        try:
            qty = int(item.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1

        if item.get("unit_price") is not None and qty > 1 and not has_per_unit_marker:
            item = {**item, "unit_price": None}

        adjusted.append(item)

    return adjusted, detected_currency


@dataclass
class ParsedSaleMessage:
    """Result of parsing a sale message."""
    items: list[ParsedSaleItem]
    currency: str | None = None


@dataclass
class UnifiedMessageResult:
    """Result of unified message parsing (intent + data extraction)."""
    intent: str  # "sale" | "add_assistant" | "sales_query" | "other"
    confidence: float

    # Sale-specific (populated if intent="sale")
    items: list[ParsedSaleItem] = field(default_factory=list)
    currency: str | None = None

    # Add assistant-specific (populated if intent="add_assistant")
    phone_number: str | None = None

    # Sales query-specific (populated if intent="sales_query")
    timeframe: str | None = None  # "today", "yesterday", "week", "month", "X_days"
    product_filter: str | None = None

    # Other-specific (populated if intent="other")
    other_sub_intent: str | None = None  # "deferral" | "greeting" | "question" | "unknown"

    # Optional metadata
    notes: str | None = None


@sync_to_async
def _get_active_products(company=None):
    """Fetch active products scoped to a company, with a 60-second in-process cache."""
    company_id = company.id if company else None
    cached = _PRODUCT_CACHE.get(company_id)
    now = monotonic()
    if cached and (now - cached[0]) < _PRODUCT_CACHE_TTL_SECONDS:
        return cached[1]

    close_old_connections()
    queryset = Product.objects.filter(active=True)
    if company:
        queryset = queryset.filter(company=company)
    products = list(queryset)
    _PRODUCT_CACHE[company_id] = (now, products)
    return products


@sync_to_async
def _save_parse_log(
    msg_id: str,
    intent: str,
    confidence: float | None,
    raw_response: dict | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    parse_error: str = "",
) -> None:
    from apps.whatsapp.models import WhatsAppMessage, LlmParseLog
    try:
        wa_msg = WhatsAppMessage.objects.get(whatsapp_message_id=msg_id)
        LlmParseLog.objects.update_or_create(
            message=wa_msg,
            defaults=dict(
                intent=intent,
                confidence=confidence,
                raw_response=raw_response,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                parse_error=parse_error,
            ),
        )
    except WhatsAppMessage.DoesNotExist:
        pass  # mock/test messages without a DB record — skip silently


async def parse_image_for_sale(
    image_url: str,
    company=None,
) -> UnifiedMessageResult:
    """Use Gemini vision to extract sale data from an image (photo of tally sheet, receipt, etc.).

    Returns a UnifiedMessageResult with intent="sale" and items if sale data was found,
    or intent="other" if the image contains no readable sale data.
    """
    async with track("image_parse"):
        products = await _get_active_products(company)
        from services.openrouter import build_image_parsing_prompt
        messages = build_image_parsing_prompt(image_url, products)

        client = OpenRouterClient(model="google/gemini-2.5-flash")

        try:
            parse_result = await client.parse_json_response(messages)
        except Exception as e:
            logger.exception(f"Failed to parse image: {e}")
            raise

        response = parse_result.response
        intent = response.get("intent", "other")
        confidence = response.get("confidence", 0.5)

        logger.info(
            f"Image LLM response: intent={intent} confidence={confidence} notes={response.get('notes')} "
            f"[tokens: prompt={parse_result.prompt_tokens} completion={parse_result.completion_tokens}]"
        )

        result = UnifiedMessageResult(
            intent=intent,
            confidence=confidence,
            notes=response.get("notes"),
        )

        if intent == "sale":
            parsed_items: list[ParsedSaleItem] = []
            for item in response.get("items", []):
                try:
                    unit_price = None
                    if item.get("unit_price") is not None:
                        try:
                            unit_price = Decimal(str(item["unit_price"]))
                        except (InvalidOperation, ValueError):
                            pass

                    item_currency = item.get("currency")
                    if item_currency and item_currency not in ("USD", "ZWG", "ZAR", "BWP", "EUR", "GBP"):
                        item_currency = None

                    parsed_items.append(
                        ParsedSaleItem(
                            product_name=str(item.get("product_name", "")),
                            quantity=int(item.get("quantity", 1)),
                            unit_price=unit_price,
                            currency=item_currency,
                        )
                    )
                except (ValueError, TypeError) as e:
                    logger.warning(f"Skipping invalid image item {item}: {e}")
                    continue

            currency = response.get("currency")
            if currency and currency in ("USD", "ZWG", "ZAR", "BWP", "EUR", "GBP"):
                result.currency = currency

            result.items = parsed_items

            if not parsed_items:
                result.intent = "other"

        return result


async def parse_message_unified(
    message: str,
    company=None,
    message_id: str | None = None,
) -> UnifiedMessageResult:
    """Unified LLM call: detect intent and extract data in one shot."""
    async with track("unified_parse"):
        products = await _get_active_products(company)
        from services.openrouter import build_unified_parsing_prompt
        messages = build_unified_parsing_prompt(message, products)

        client = OpenRouterClient()

        try:
            parse_result = await client.parse_json_response(messages)
        except Exception as e:
            logger.exception(f"Failed to parse message: {e}")
            if message_id:
                try:
                    await _save_parse_log(message_id, "", None, None, None, None, parse_error=str(e))
                except Exception:
                    logger.exception("Failed to save LlmParseLog for error")
            raise

        response = parse_result.response
        prompt_tokens = parse_result.prompt_tokens
        completion_tokens = parse_result.completion_tokens

        logger.info(
            f"LLM response for '{message[:80]}': {response} "
            f"[tokens: prompt={prompt_tokens} completion={completion_tokens} message={message_id}]"
        )

        intent = response.get("intent", "sale")
        confidence = response.get("confidence", 0.5)

        result = UnifiedMessageResult(
            intent=intent,
            confidence=confidence,
            notes=response.get("notes"),
        )

        if intent == "sale":
            parsed_items: list[ParsedSaleItem] = []

            for item in response.get("items", []):
                try:
                    unit_price = None
                    if item.get("unit_price") is not None:
                        try:
                            unit_price = Decimal(str(item["unit_price"]))
                        except (InvalidOperation, ValueError):
                            pass

                    item_currency = item.get("currency")
                    if item_currency and item_currency not in ("USD", "ZWG", "ZAR", "BWP", "EUR", "GBP"):
                        item_currency = None

                    parsed_items.append(
                        ParsedSaleItem(
                            product_name=str(item.get("product_name", "")),
                            quantity=int(item.get("quantity", 1)),
                            unit_price=unit_price,
                            currency=item_currency,
                        )
                    )
                except (ValueError, TypeError) as e:
                    logger.warning(f"Skipping invalid item {item}: {e}")
                    continue

            result.items, inferred_currency = _apply_rule_based_price_hints(
                message, parsed_items, response.get("currency")
            )

            currency = inferred_currency or response.get("currency")
            if currency and currency in ("USD", "ZWG", "ZAR", "BWP", "EUR", "GBP"):
                result.currency = currency
            elif currency:
                logger.warning(f"Unknown currency {currency}, ignoring")

        elif intent == "add_assistant":
            result.phone_number = response.get("phone_number")

        elif intent == "other":
            sub = response.get("other_sub_intent", "unknown")
            if sub in ("deferral", "greeting", "question", "unknown"):
                result.other_sub_intent = sub
            else:
                result.other_sub_intent = "unknown"

        elif intent == "sales_query":
            result.timeframe = response.get("timeframe", "today")
            result.product_filter = response.get("product_filter")

        if message_id:
            try:
                await _save_parse_log(
                    message_id, intent, confidence, response, prompt_tokens, completion_tokens
                )
            except Exception:
                logger.exception("Failed to save LlmParseLog")

        return result
