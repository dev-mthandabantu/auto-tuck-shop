from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict

from django.db import transaction

from apps.catalog.models import Product, ProductPrice
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
    price_ambiguous: bool  # True if a price was stated but total-vs-unit was unclear


class SaleCreationResult(TypedDict):
    sale: Sale
    unmatched_items: list[str]


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
    # Validate prices before touching the database
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

    sale = Sale.objects.create(
        whatsapp_message_id=whatsapp_message_id,
        company=company,
    )
    unmatched_items: list[str] = []
    detected_currency = currency or "USD"

    for item in items:
        product = find_product_by_name(item["product_name"], company=company)

        if not product:
            # Create new product if not found
            product = Product.objects.create(
                name=item["product_name"],
                company=company,
            )
            # If a price is provided, set it as the initial price with currency
            if item.get("unit_price") is not None:
                ProductPrice.objects.create(
                    product=product,
                    price=item["unit_price"],
                    currency=item.get("currency") or detected_currency,
                )

        # Determine price and currency
        unit_price = item.get("unit_price")
        item_currency = None

        if unit_price is not None:
            # Use per-item currency if the LLM provided one, else fall back to sale-level
            item_currency = item.get("currency") or detected_currency

            # Update stored price if different from current price
            current_price = product.current_price
            if current_price is None or unit_price != current_price:
                ProductPrice.objects.create(
                    product=product,
                    price=unit_price,
                    currency=item_currency,
                )
        elif not item.get("price_ambiguous"):
            # No price was mentioned at all — fall back to the stored price.
            price_with_currency = product.current_price_with_currency
            if price_with_currency:
                unit_price, item_currency = price_with_currency

        if unit_price is None:
            raise MissingPriceError(
                f"No price for '{item['product_name']}'. Please resend with a price."
            )

        SaleItem.objects.create(
            sale=sale,
            product=product,
            quantity=item["quantity"],
            unit_price=unit_price,
            currency=item_currency,
        )

    # Recalculate total
    sale.total_amount = sale.calculate_total()
    sale.save()

    return {"sale": sale, "unmatched_items": unmatched_items}
