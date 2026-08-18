from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict

from django.db import transaction

from apps.catalog.models import Product
from apps.sales.models import Sale, SaleItem

if TYPE_CHECKING:
    from apps.core.models import Company

logger = logging.getLogger(__name__)

# DecimalField(max_digits=10, decimal_places=2) → max abs value is 10^8 - 0.01
MAX_UNIT_PRICE = Decimal("99999999.99")


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


class SaleCreationResult(TypedDict):
    sale: Sale | None
    unmatched_items: list[str]
    ambiguous_items: list[str]


def find_product_by_name(name: str, company: Company) -> Product | None:
    """Find a product by name (case-insensitive exact match), scoped to company."""
    return Product.objects.filter(
        active=True, company=company
    ).filter(name__iexact=name).first()


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

    Returns the created sale and a list of unmatched product names.
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
    unmatched_items: list[str] = []
    ambiguous_items: list[str] = []
    detected_currency = currency or "USD"

    for item in items:
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
                unmatched_items.append(item["product_name"])
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
