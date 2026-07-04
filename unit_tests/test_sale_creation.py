"""Unit tests for apps/sales/services.py sale creation, focused on price resolution."""

from decimal import Decimal

from django.test import TestCase

from apps.catalog.models import Product, ProductPrice
from apps.core.models import Company
from apps.sales.services import MissingPriceError, create_sale_from_parsed_items
from apps.whatsapp.services.message_parser import _apply_rule_based_price_hints


def _make_company(name="Test Shop"):
    return Company.objects.create(name=name, slug=name.lower().replace(" ", "-"), active=True)


class CreateSaleFromParsedItemsTest(TestCase):

    def setUp(self):
        self.company = _make_company()

    def test_falls_back_to_stored_price_when_no_price_mentioned(self):
        """No price in the message at all — reuse the last known stored price."""
        product = Product.objects.create(name="Games", company=self.company)
        ProductPrice.objects.create(product=product, price=Decimal("5.00"), currency="ZAR")

        result = create_sale_from_parsed_items(
            items=[{"product_name": "Games", "quantity": 4, "unit_price": None, "currency": None}],
            company=self.company,
        )
        item = result["sale"].items.get()
        self.assertEqual(item.unit_price, Decimal("5.00"))
        self.assertEqual(item.currency, "ZAR")

    def test_ambiguous_price_does_not_fall_back_to_stored_price(self):
        """Regression for #220: a price WAS stated but was ambiguous (total vs
        per-unit, e.g. '4 games $1 sale') — must not silently substitute the
        stored/catalog price. Should raise MissingPriceError so the owner is
        asked to clarify instead."""
        product = Product.objects.create(name="Games", company=self.company)
        ProductPrice.objects.create(product=product, price=Decimal("5.00"), currency="ZAR")

        with self.assertRaises(MissingPriceError):
            create_sale_from_parsed_items(
                items=[{
                    "product_name": "Games",
                    "quantity": 4,
                    "unit_price": None,
                    "currency": None,
                    "price_ambiguous": True,
                }],
                company=self.company,
            )

    def test_stated_price_overrides_stored_price(self):
        """A clearly-parsed stated price always overrides the stored catalog price."""
        product = Product.objects.create(name="Games", company=self.company)
        ProductPrice.objects.create(product=product, price=Decimal("5.00"), currency="ZAR")

        result = create_sale_from_parsed_items(
            items=[{"product_name": "Games", "quantity": 4, "unit_price": Decimal("0.25"), "currency": "USD"}],
            company=self.company,
        )
        item = result["sale"].items.get()
        self.assertEqual(item.unit_price, Decimal("0.25"))
        self.assertEqual(item.currency, "USD")


class PriceHintsTest(TestCase):
    """Direct tests for _apply_rule_based_price_hints — no LLM needed."""

    def test_price_equals_qty_resolves_to_one_dollar(self):
        """'7 plates beef $7' — price == qty → $1 each, not ambiguous."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("7.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $7", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("1.00"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_price_divisible_by_qty_resolves_unit_price(self):
        """'7 plates beef $14' — 14 ÷ 7 = $2 exactly → $2 each, not ambiguous."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("14.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $14", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("2.00"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_non_divisible_price_stays_ambiguous(self):
        """'7 plates beef $15' — 15 ÷ 7 = $2.142… (non-exact) → ambiguous."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("15.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $15", items, "USD")
        self.assertIsNone(result[0]["unit_price"])
        self.assertTrue(result[0].get("price_ambiguous"))
