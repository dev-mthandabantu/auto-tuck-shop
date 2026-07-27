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

    def test_cents_price_not_treated_as_total(self):
        """Regression: '3 chips $1.50' — a price with cents is essentially
        never a stated total in this domain. Must stay $1.50 each, NOT be
        treated as a total-to-divide (which produced the wrong $0.50/each
        and, worse, previously flagged it ambiguous since 1.50 % 3 != 0)."""
        items = [{"product_name": "chips", "quantity": 3, "unit_price": Decimal("1.50"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("3 chips $1.50", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("1.50"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_cents_price_below_qty_not_treated_as_total(self):
        """'4 games $0.25' — cents price smaller than qty. Locks in the
        no-cents-means-skip-the-check reasoning explicitly, rather than
        relying on the coincidental non-zero remainder (0.25 % 4 != 0)."""
        items = [{"product_name": "games", "quantity": 4, "unit_price": Decimal("0.25"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("4 games $0.25", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("0.25"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_llm_already_resolved_whole_number_total_trusted(self):
        """Regression: the LLM sometimes pre-resolves a stated total into a
        per-unit price itself (observed live: '7 plates beef $7' -> LLM
        returns unit_price=1.00 directly, with a note explaining the
        division). The literal price typed in the message ($7) differs from
        the LLM's returned unit_price (1.00) — that mismatch means the LLM
        already did the math, so the heuristic must NOT re-check divisibility
        against 1.00 (1 % 7 != 0 would wrongly flag it ambiguous)."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("1.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $7", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("1.00"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_llm_already_resolved_second_case(self):
        """Same shape as above for '7 plates beef $14' -> LLM pre-resolved to
        unit_price=2.00 (literal stated price 14 differs from 2.00 -> trust
        the LLM, don't re-check 2 % 7 != 0)."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("2.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $14", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("2.00"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_llm_passthrough_whole_number_still_divides(self):
        """Confirms the ORIGINAL #173 behavior is preserved when the LLM
        passes the raw stated price through unresolved: same message
        '7 plates beef $7', but this time unit_price=7.00 (matches the
        literal stated price -> LLM did NOT pre-resolve) -> the heuristic
        should still divide it to $1.00 each, exactly as before."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("7.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $7", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("1.00"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_llm_passthrough_non_divisible_still_ambiguous(self):
        """Original #220 case unchanged: '4 games $1 sale', unit_price=1.00
        matches the literal stated price (1) -> passthrough case -> 1 % 4 != 0
        -> genuinely ambiguous, clear and flag."""
        items = [{"product_name": "games", "quantity": 4, "unit_price": Decimal("1.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("4 games $1 sale", items, "USD")
        self.assertIsNone(result[0]["unit_price"])
        self.assertTrue(result[0].get("price_ambiguous"))

    def test_extraction_ambiguous_falls_back_to_existing_behavior(self):
        """A message with no clear single currency-adjacent price token (no
        '$'/'R'/etc. marker at all) makes _extract_literal_stated_price
        return None — the heuristic should fall through to the pre-existing
        cents/divisibility logic unchanged, not skip processing entirely."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("14.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef 14", items, "USD")
        # No currency marker -> extraction returns None -> falls back to
        # divisibility check on the raw LLM price (14 % 7 == 0 -> $2 each).
        self.assertEqual(result[0]["unit_price"], Decimal("2.00"))
        self.assertFalse(result[0].get("price_ambiguous"))
