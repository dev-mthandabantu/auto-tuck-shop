from decimal import Decimal

from django.db import models
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.currencies import format_price


class Sale(models.Model):
    """A sale transaction."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"

    company = models.ForeignKey(
        "core.Company",
        on_delete=models.CASCADE,
        related_name="sales",
    )
    sale_timestamp = models.DateTimeField(default=timezone.now)
    total_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, default=Decimal("0.00")
    )
    whatsapp_message_id = models.CharField(max_length=100, null=True, blank=True)
    confirmation_message_sid = models.CharField(max_length=100, null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    flagged_as_bot_mistake = models.BooleanField(
        default=False,
        help_text="Flagged by user as a bot misinterpretation",
    )

    class Meta:
        ordering = ["-sale_timestamp"]

    def __str__(self):
        currency = self.company.currency if self.company else "USD"
        total_str = format_price(self.total_amount, currency) if self.total_amount is not None else "mixed currencies"
        return f"Sale #{self.id} - {total_str} ({self.sale_timestamp.date()})"

    def calculate_total(self) -> Decimal | None:
        """Calculate total from sale items. Returns None for mixed-currency sales."""
        items = list(self.items.all())
        if not items:
            return Decimal("0.00")
        currencies = {item.currency for item in items if item.currency}
        if len(currencies) > 1:
            return None
        total = sum(item.line_total for item in items)
        return Decimal(str(total))

    def save(self, *args, **kwargs):
        if self.pk:
            self.total_amount = self.calculate_total()
        super().save(*args, **kwargs)


class SaleItem(models.Model):
    """Individual item in a sale."""

    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="sale_items",
    )
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        currency = self.currency or (self.sale.company.currency if self.sale and self.sale.company else "USD")
        return f"{self.quantity}x {self.product.name} @ {format_price(self.unit_price, currency)}"

    @property
    def line_total(self) -> Decimal:
        """Calculate the total for this line item."""
        if self.unit_price is None:
            return Decimal("0.00")
        return self.quantity * self.unit_price
