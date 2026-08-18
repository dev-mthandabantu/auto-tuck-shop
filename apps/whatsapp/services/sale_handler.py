"""Sale processing and sale button actions."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from django.db import close_old_connections, models, transaction

from apps.catalog.models import ProductPrice
from apps.core.currencies import format_price
from apps.core.models import Company
from apps.sales.models import Sale
from apps.sales.services import create_sale_from_parsed_items, PriceOverflowError
from apps.whatsapp.services.webhook_handler import (
    db_sync_to_async,
    DEFAULT_LANGUAGE,
    _extract_phone_number,
    _get_profile_by_phone,
    _send_response,
    _send_response_with_buttons,
    run_async,
    t,
)
from utils.timing import track

logger = logging.getLogger(__name__)


async def _create_sale(parsed_items, message_id, company, currency=None):
    async with track("create_sale"):
        @db_sync_to_async
        def _create_sale_sync():
            return create_sale_from_parsed_items(
                items=parsed_items,
                whatsapp_message_id=message_id,
                company=company,
                currency=currency,
            )
        return await _create_sale_sync()


@db_sync_to_async
def _get_sale_items(sale):
    return list(sale.items.select_related("product").all())


@db_sync_to_async
def _store_response_message_sid(sale_id: int, message_sid: str) -> None:
    Sale.objects.filter(id=sale_id).update(confirmation_message_sid=message_sid)


@db_sync_to_async
def _update_company_currency(company_id: int, currency: str) -> None:
    Company.objects.filter(id=company_id).update(currency=currency)


@db_sync_to_async
def _set_pending_clarification(company_id: int, sale_id: int) -> None:
    Company.objects.filter(id=company_id).update(pending_clarification_sale_id=sale_id)


@db_sync_to_async
def _clear_pending_clarification(company_id: int) -> None:
    Company.objects.filter(id=company_id).update(pending_clarification_sale_id=None)


@db_sync_to_async
def _apply_currency_to_sale(company_id: int, sale_id: int, currency: str) -> bool:
    """Apply currency to all items in the pending clarification sale. Returns True if found."""
    from apps.sales.models import SaleItem
    from decimal import Decimal
    updated = SaleItem.objects.filter(sale_id=sale_id).update(currency=currency)
    if updated:
        total = SaleItem.objects.filter(sale_id=sale_id).aggregate(
            total=models.Sum(models.F("unit_price") * models.F("quantity"))
        )["total"] or Decimal("0")
        Sale.objects.filter(id=sale_id).update(total_amount=total)
        Company.objects.filter(id=company_id).update(pending_clarification_sale_id=None)
    return bool(updated)


@db_sync_to_async
def _get_pending_clarification_sale_id(company_id: int) -> int | None:
    company = Company.objects.filter(id=company_id).only("pending_clarification_sale_id").first()
    return company.pending_clarification_sale_id if company else None


@db_sync_to_async
def _get_confirmed_sale(original_message_sid: str | None):
    if not original_message_sid:
        return None

    with transaction.atomic():
        try:
            # select_for_update locks this row for the transaction: a
            # concurrent confirm/cancel on the same sale blocks here instead
            # of also reading PENDING and racing this one.
            sale = Sale.objects.select_for_update().get(
                confirmation_message_sid=original_message_sid,
                status=Sale.Status.PENDING,
            )
        except Sale.DoesNotExist:
            return None

        for item in sale.items.select_related("product").all():
            if item.unit_price is not None:
                product = item.product
                current_price = product.current_price
                if current_price is None or item.unit_price != current_price:
                    ProductPrice.objects.create(
                        product=product,
                        price=item.unit_price,
                        currency=item.currency,
                    )
        sale.status = Sale.Status.CONFIRMED
        sale.save(update_fields=["status"])

    return sale, sale.whatsapp_message_id


@db_sync_to_async
def _is_first_confirmed_sale(company_id: int) -> bool:
    return Sale.objects.filter(
        company_id=company_id,
        status=Sale.Status.CONFIRMED,
    ).count() == 1


@db_sync_to_async
def _company_needs_closing_time(company_id: int) -> bool:
    company = Company.objects.filter(id=company_id).only("normal_closing_time").first()
    return company is not None and company.normal_closing_time is None


@db_sync_to_async
def _get_and_update_sale(original_message_sid: str | None, new_status: str, bot_mistake: bool = False):
    if not original_message_sid:
        return None

    with transaction.atomic():
        try:
            # Sales start PENDING; only a still-pending sale can be confirmed
            # or cancelled here (a sale already CONFIRMED/CANCELLED means
            # this button was already processed). select_for_update locks
            # the row for the transaction so a concurrent confirm/cancel on
            # the same sale blocks here instead of racing this one.
            sale = Sale.objects.select_for_update().get(
                confirmation_message_sid=original_message_sid,
                status=Sale.Status.PENDING,
            )
        except Sale.DoesNotExist:
            return None

        sale.status = new_status
        if bot_mistake:
            sale.flagged_as_bot_mistake = True
        sale.save(update_fields=["status", "flagged_as_bot_mistake"])
        return sale, sale.whatsapp_message_id


async def process_sale_message_unified(
    message_id: str,
    sender: str,
    text: str,
    company,
    result,
    is_from_audio: bool = False,
    lang: str = DEFAULT_LANGUAGE,
) -> None:
    if not result.items:
        await _send_response(sender, t("sale.no_products", lang=lang))
        return

    if result.currency and company:
        await _update_company_currency(company.id, result.currency)
        company.currency = result.currency

    # Create the sale; create_sale_from_parsed_items now returns ambiguous and
    # unmatched items rather than raising MissingPriceError so we can salvage
    # valid lines and ask the user to clarify only the problematic ones.
    try:
        sale_result = await _create_sale(result.items, message_id, company, currency=result.currency)
    except PriceOverflowError:
        logger.warning("Price overflow for message %s from %s", message_id, sender)
        await _send_response(sender, t("sale.price_too_large", lang=lang), reply_to=message_id)
        return

    sale = sale_result["sale"]
    unmatched = sale_result.get("unmatched_items", [])
    ambiguous = sale_result.get("ambiguous_items", [])

    response_lines = []
    sale_items = await _get_sale_items(sale) if sale else []
    currency_totals: dict[str, Decimal] = {}

    for item in sale_items:
        currency_totals[item.currency] = (
            currency_totals.get(item.currency, Decimal("0"))
            + item.unit_price * item.quantity
        )
        response_lines.append(t(
            "sale.item_with_price", lang=lang,
            quantity=item.quantity, product=item.product.name,
            price=format_price(item.unit_price, item.currency),
        ))

    if len(currency_totals) == 1:
        currency, total = next(iter(currency_totals.items()))
        response_lines.append(t("sale.total", lang=lang, total=format_price(total, currency)))
    else:
        for currency, total in currency_totals.items():
            response_lines.append(t("sale.subtotal", lang=lang, currency=currency, total=format_price(total, currency)))

    has_sale_items = bool(sale_items)
    if unmatched:
        response_lines.append(t("sale.unmatched", lang=lang, items=", ".join(unmatched)))

    if ambiguous:
        if has_sale_items:
            response_lines.append(t(
                "sale.price_ambiguous_notice", lang=lang, items=", ".join(ambiguous)
            ))
        else:
            response_lines.append(t(
                "sale.price_ambiguous_only_notice", lang=lang, items=", ".join(ambiguous)
            ))

    if has_sale_items:
        buttons = [
            {"id": f"confirm_{sale.id}", "title": t("sale.btn_confirm", lang=lang)},
            {"id": f"fix_{sale.id}", "title": t("sale.btn_fix", lang=lang)},
        ]
        message_sid = await _send_response_with_buttons(
            sender, "\n".join(response_lines), buttons, reply_to=message_id
        )
        if message_sid:
            await _store_response_message_sid(sale.id, message_sid)
    else:
        await _send_response(sender, "\n".join(response_lines), reply_to=message_id)

    # If all priced items have no currency, ask the owner to clarify
    priced_items = [i for i in sale_items if i.unit_price is not None]
    if priced_items and all(not i.currency for i in priced_items) and company:
        await _set_pending_clarification(company.id, sale.id)
        await asyncio.sleep(2)
        await _send_response_with_buttons(
            sender,
            t("currency_clarification.ask", lang=lang),
            [
                {"id": "currency_usd", "title": t("currency_clarification.btn_usd", lang=lang)},
                {"id": "currency_zwg", "title": t("currency_clarification.btn_zwg", lang=lang)},
                {"id": "currency_zar", "title": t("currency_clarification.btn_zar", lang=lang)},
            ],
        )


async def process_sale_button_action_async(
    action: str,
    sender: str,
    original_message_sid: str | None = None,
) -> None:
    phone_number = _extract_phone_number(sender)
    profile = await _get_profile_by_phone(phone_number)
    lang = profile.language if profile else DEFAULT_LANGUAGE

    if action == "confirm":
        result = await _get_confirmed_sale(original_message_sid)
        if result:
            sale, original_whatsapp_message_id = result
            await _send_response(sender, t("sale.confirmed_ok", lang=lang), reply_to=original_whatsapp_message_id)
            # After first confirmed sale, ask for closing time via buttons
            if await _company_needs_closing_time(sale.company_id) and await _is_first_confirmed_sale(sale.company_id):
                await asyncio.sleep(3)
                await _send_response_with_buttons(
                    sender,
                    t("closing.setup_prompt_buttons", lang=lang),
                    [
                        {"id": "closing_early", "title": t("closing.btn_early", lang=lang)},
                        {"id": "closing_mid",   "title": t("closing.btn_mid",   lang=lang)},
                        {"id": "closing_late",  "title": t("closing.btn_late",  lang=lang)},
                    ],
                )
        else:
            await _send_response(sender, t("sale.already_processed", lang=lang))
    else:
        result = await _get_and_update_sale(original_message_sid, Sale.Status.CANCELLED, bot_mistake=True)
        if result:
            sale, original_whatsapp_message_id = result
            await _send_response(sender, t("sale.bot_mistake", lang=lang), reply_to=original_whatsapp_message_id)
        else:
            await _send_response(sender, t("sale.already_processed", lang=lang))


def handle_sale_button_action(action: str, sender: str, original_message_sid: str | None = None) -> None:
    try:
        close_old_connections()
        run_async(process_sale_button_action_async(action, sender, original_message_sid))
    except Exception as e:
        logger.exception(f"Error handling sale {action}: {e}")


_CURRENCY_BUTTON_MAP = {
    "currency_usd": "USD",
    "currency_zwg": "ZWG",
    "currency_zar": "ZAR",
}


async def _process_currency_clarification_async(sender: str, currency: str) -> None:
    phone_number = _extract_phone_number(sender)
    profile = await _get_profile_by_phone(phone_number)
    lang = profile.language if profile else DEFAULT_LANGUAGE
    if not profile or not profile.company_id:
        return

    sale_id = await _get_pending_clarification_sale_id(profile.company_id)
    if not sale_id:
        await _send_response(sender, t("currency_clarification.not_found", lang=lang))
        return

    updated = await _apply_currency_to_sale(profile.company_id, sale_id, currency)
    if updated:
        await _send_response(sender, t("currency_clarification.confirmed", lang=lang, currency=currency))
    else:
        await _send_response(sender, t("currency_clarification.not_found", lang=lang))


def handle_currency_clarification(sender: str, button_id: str) -> None:
    currency = _CURRENCY_BUTTON_MAP.get(button_id)
    if not currency:
        return
    try:
        close_old_connections()
        run_async(_process_currency_clarification_async(sender, currency))
    except Exception as e:
        logger.exception(f"Error handling currency clarification {button_id}: {e}")
