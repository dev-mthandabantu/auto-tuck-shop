from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict

import emoji
from django.db import transaction
from django.utils import timezone

from apps.catalog.models import Product
from apps.sales.models import Sale, SaleItem

if TYPE_CHECKING:
    from apps.core.models import Company

logger = logging.getLogger(__name__)

# DecimalField(max_digits=10, decimal_places=2) → max abs value is 10^8 - 0.01
MAX_UNIT_PRICE = Decimal("99999999.99")

# How long a sale can sit unconfirmed before it's auto-cancelled.
STALE_PENDING_THRESHOLD = timezone.timedelta(hours=24)


class PriceOverflowError(ValueError):
    """Raised when a parsed price exceeds the database field limit."""
    pass


class MissingPriceError(ValueError):
    """Raised when a sale item has no price and no stored product price to fall back on."""
    pass


class ParsedSaleItem(TypedDict, total=False):
    product_name: str
    quantity: int
    unit_price: Decimal | None
    currency: str | None  # per-item currency; overrides top-level currency when set
    price_ambiguous: bool | None


class UnmatchedItem(TypedDict):
    product_name: str
    quantity: int


class SaleCreationResult(TypedDict):
    sale: Sale | None
    unmatched_items: list[UnmatchedItem]
    ambiguous_items: list[str]


# Matches a trailing/leading " emoji" (or "emoji ") word the LLM sometimes
# appends/prepends to an otherwise-plain name, e.g. "house emoji" -> "house".
_EMOJI_SUFFIX_RE = re.compile(r"^\s*emoji\s+|\s+emoji\s*$", re.IGNORECASE)

# English words the LLM uses in place of an emoji, mapped to that emoji's
# demojized canonical name (see canonicalize_product_name's own emoji.demojize
# call) — #356's repro had the LLM return raw "🚗" on one call and "car" on
# another for the same sale. Without this map those two calls create two
# separate Product rows (split price/report history) because "car" is plain
# text and skips the emoji branch entirely. Keyed on the lowercased, "emoji"
# suffix-stripped name; only covers the pilot's demo emoji plus common
# synonyms actually seen from the LLM — extend as new mismatches surface.
_TEXT_TO_EMOJI_CANONICAL = {
    "car": "automobile",
    "automobile": "automobile",
    "house": "house",
    "home": "house",
    "banana": "banana",
}


def canonicalize_product_name(name: str) -> str:
    """Resolve an emoji-derived product name to a stable, deterministic label.

    The LLM has no canonicalization step of its own, so the same product can
    come back as a raw emoji character, that emoji's English word, or the
    word suffixed with "emoji" — on different calls for the same sale (see
    #356). Two sales of the same real product should always match to the
    same Product row, so this collapses all three forms to one canonical
    name before it ever reaches find_product_by_name / Product.objects.create.

    Emoji-only strings (once the "emoji" suffix is stripped) are demojized.
    Plain text is then checked against a known word->emoji-canonical map so
    it converges with its emoji counterpart; anything else is left as typed.
    """
    stripped = _EMOJI_SUFFIX_RE.sub("", name).strip()
    if not stripped:
        return name

    matches = emoji.emoji_list(stripped)
    matched_chars = sum(m["match_end"] - m["match_start"] for m in matches)
    if matched_chars != len(stripped):
        # Not composed entirely of emoji characters. Map known English words
        # for the pilot's demo emoji onto the same canonical name so they
        # converge with the raw-emoji path; anything else is left as-is
        # (after the "emoji" suffix strip, which is safe for plain text too).
        return _TEXT_TO_EMOJI_CANONICAL.get(stripped.lower(), stripped)

    seen: set[str] = set()
    names: list[str] = []
    for m in matches:
        canonical = emoji.demojize(m["emoji"]).strip(":").replace("_", " ")
        if canonical not in seen:
            seen.add(canonical)
            names.append(canonical)
    return " ".join(names)


def _singular_plural_variants(name: str) -> list[str]:
    """Return the other singular/plural form(s) of the last word in `name`,
    to try as a fallback lookup — never as a rename. Deliberately narrow
    (4+ char last word) since this only feeds a lookup against EXISTING
    products, not a transform applied to the name that gets stored — see
    #359's regression against an already-existing product literally named
    "Games" (arcade credits, not the plural of "game"): renaming the
    incoming name outright would have silently mismatched that shop's real
    product. Trying both forms as a lookup fallback carries no such risk —
    it only ever matches a product the company already has under that exact
    name.

    Handles two pluralization patterns:
    - bare "-s": "banana" <-> "bananas".
    - "-es" after s/x/z/ch/sh: "box" <-> "boxes", "glass" <-> "glasses",
      "match" <-> "matches". A word in this class only ever gets the "-es"
      form generated, never bare "+s" too — "box" -> ["boxes"], not
      ["boxs", "boxes"]. Appending bare "s" to a word ending in
      s/x/z/ch/sh always produces a malformed non-word ("glasss", "boxs",
      "matchs"): it can never match a real product, so generating it is
      pure waste at best. The two patterns are mutually exclusive per
      word, decided by the same suffix check in both directions.
    """
    words = name.split(" ")
    last = words[-1]
    if len(last) <= 3:
        return []
    lower = last.lower()
    variants: list[str] = []

    if lower.endswith("es") and lower[:-2] and (
        lower[:-2].endswith(("s", "x", "z", "ch", "sh"))
    ):
        variants.append(last[:-2])
    elif lower.endswith("s") and not lower.endswith("ss"):
        variants.append(last[:-1])
    elif lower.endswith(("s", "x", "z", "ch", "sh")):
        variants.append(last + "es")
    else:
        variants.append(last + "s")

    return [" ".join(words[:-1] + [v]) for v in variants]


def find_product_by_name(name: str, company: Company) -> Product | None:
    """Find a product by name (case-insensitive exact match), scoped to
    company. Falls back to the other singular/plural form of the name if no
    exact match exists (#359: "bananas" finds an existing "banana" product,
    and vice versa) — this only ever matches a product the company already
    has; it never renames or creates anything, so an existing product like
    "Games" is untouched when no "Game" product exists to match against.

    Ordered by id (oldest first) so a case-variant duplicate (e.g. both
    "Banana" and "banana" already existing) resolves to the same row every
    time instead of depending on undefined DB ordering. This doesn't merge
    or clean up existing duplicates — see #360 for that — it just makes an
    already-ambiguous lookup deterministic rather than silently random.
    """
    products = Product.objects.filter(active=True, company=company)
    product = products.filter(name__iexact=name).order_by("id").first()
    if product:
        return product

    for variant in _singular_plural_variants(name):
        product = products.filter(name__iexact=variant).order_by("id").first()
        if product:
            return product
    return None


@transaction.atomic
def create_sale_from_parsed_items(
    items: list[ParsedSaleItem],
    whatsapp_message_id: str | None = None,
    company: Company = None,
    currency: str | None = None,
) -> SaleCreationResult:
    """
    Create a sale from parsed sale items.

    Args:
        items: List of parsed sale items
        whatsapp_message_id: Optional WhatsApp message ID
        company: The company this sale belongs to
        currency: The detected currency from the message

    Returns the created sale and a list of unmatched items (name + quantity).
    """
    # Validate prices before touching the database (ignore ambiguous items
    # which have had their unit_price cleared by the parser)
    for item in items:
        price = item.get("unit_price")
        if price is not None and abs(Decimal(str(price))) > MAX_UNIT_PRICE:
            logger.warning(
                "Price overflow rejected: product=%s, unit_price=%s",
                item["product_name"],
                price,
            )
            raise PriceOverflowError(
                f"Price {price} for '{item['product_name']}' is too large. "
                f"Maximum allowed is {MAX_UNIT_PRICE}."
            )

    sale: Sale | None = None
    unmatched_items: list[UnmatchedItem] = []
    ambiguous_items: list[str] = []
    detected_currency = currency or "USD"

    for item in items:
        item["product_name"] = canonicalize_product_name(item["product_name"])
        product = find_product_by_name(item["product_name"], company=company)

        # Determine price and currency
        unit_price = item.get("unit_price")
        item_currency = None

        if unit_price is not None:
            # Use per-item currency if the LLM provided one, else fall back to sale-level
            item_currency = item.get("currency") or detected_currency
        elif product and not item.get("price_ambiguous"):
            # No price was mentioned at all — fall back to the stored price.
            price_with_currency = product.current_price_with_currency
            if price_with_currency:
                unit_price, item_currency = price_with_currency

        if unit_price is None:
            # If this item was flagged as ambiguous by the parser (e.g. "3 chips $1"
            # where it's unclear whether $1 is total or per-item), record it as
            # an ambiguous item and skip creating a SaleItem for now. Similarly,
            # if no stored price exists, mark as unmatched. We do not raise an
            # exception here to avoid rolling back the entire sale; valid lines
            # will be saved and the caller will inform the user which lines need
            # clarification or prices.
            if item.get("price_ambiguous"):
                ambiguous_items.append(item["product_name"])
                continue
            else:
                unmatched_items.append({
                    "product_name": item["product_name"],
                    "quantity": item["quantity"],
                })
                continue

        if not product:
            product = Product.objects.create(
                name=item["product_name"],
                company=company,
            )

        if sale is None:
            sale = Sale.objects.create(
                whatsapp_message_id=whatsapp_message_id,
                company=company,
            )

        SaleItem.objects.create(
            sale=sale,
            product=product,
            quantity=item["quantity"],
            unit_price=unit_price,
            currency=item_currency,
        )

    if sale is not None:
        sale.total_amount = sale.calculate_total()
        sale.save()

    return {
        "sale": sale,
        "unmatched_items": unmatched_items,
        "ambiguous_items": ambiguous_items,
    }


def auto_cancel_stale_pending_sales(now=None) -> int:
    """Cancel PENDING sales the owner never confirmed or rejected within
    STALE_PENDING_THRESHOLD. Returns the number of sales cancelled."""
    now = now or timezone.now()
    cutoff = now - STALE_PENDING_THRESHOLD
    stale_sales = Sale.objects.filter(status=Sale.Status.PENDING, sale_timestamp__lt=cutoff)
    count = stale_sales.count()
    if count:
        stale_sales.update(status=Sale.Status.CANCELLED)
        logger.info("Auto-cancelled %s stale PENDING sale(s) older than %s", count, cutoff)
    return count
