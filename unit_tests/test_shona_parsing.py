"""Regression tests for Shona language message parsing.

These tests call the live LLM and require OPENROUTER_API_KEY to be set.
They are skipped automatically if the key is absent (local dev without .env).

Run with:
    python manage.py test unit_tests.test_shona_parsing
"""

import asyncio
from decimal import Decimal

from django.test import TestCase

from apps.whatsapp.services.message_parser import parse_message_unified
from services.openrouter.client import OpenRouterError


class ShonaParsingTestCase(TestCase):

    def _parse(self, message):
        try:
            return asyncio.run(parse_message_unified(message))
        except Exception as e:
            if "401" in str(e) or "API request failed" in str(e) or "OpenRouter" in type(e).__name__:
                self.skipTest(f"OpenRouter unavailable (no valid API key): {e}")
            raise

    # ------------------------------------------------------------------
    # imwe / imwe neimwe (per-unit price marker)
    # ------------------------------------------------------------------

    def test_imwe_per_unit_price_usd(self):
        """'coke 5 imwe $1' → 5 cokes at $1 each."""
        result = self._parse("coke 5 imwe $1")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertIn("coke", item["product_name"].lower())
        self.assertEqual(item["quantity"], 5)
        self.assertEqual(item["unit_price"], 1.0)
        self.assertEqual(result.currency, "USD")

    def test_imwe_per_unit_price_zwg(self):
        """'bread 3 imwe ZWG2' → 3 bread at ZWG 2 each."""
        result = self._parse("bread 3 imwe ZWG2")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertIn("bread", item["product_name"].lower())
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item["unit_price"], 2.0)
        self.assertEqual(result.currency, "ZWG")

    def test_imwe_neimwe_per_unit(self):
        """'airtime 10 imwe neimwe ZWG5' → 10 airtime at ZWG 5 each."""
        result = self._parse("airtime 10 imwe neimwe ZWG5")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item["quantity"], 10)
        self.assertEqual(item["unit_price"], 5.0)
        self.assertEqual(result.currency, "ZWG")

    # ------------------------------------------------------------------
    # rimwe / noun-class per-unit variants — regression for #88
    # ------------------------------------------------------------------

    def test_rimwe_price_before_marker(self):
        """'Ndatengesa 5 mazepe $1 rimwe' → 5 mazepe at $1 each (price BEFORE marker)."""
        result = self._parse("Ndatengesa 5 mazepe $1 rimwe")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertIn("mazepe", item["product_name"].lower())
        self.assertEqual(item["quantity"], 5)
        self.assertEqual(item["unit_price"], Decimal("1.00"))
        self.assertEqual(result.currency, "USD")

    def test_humwe_hweiita_price_after(self):
        """'Ndatengesa 15 uswa humwe hweiita $4' → 15 uswa at $4 each."""
        result = self._parse("Ndatengesa 15 uswa humwe hweiita $4")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertIn("uswa", item["product_name"].lower())
        self.assertEqual(item["quantity"], 15)
        self.assertEqual(item["unit_price"], Decimal("4.00"))
        self.assertEqual(result.currency, "USD")

    def test_humwe_per_unit_rand(self):
        """'10 bread humwe R2' → 10 bread at R2 each."""
        result = self._parse("10 bread humwe R2")
        self.assertEqual(result.intent, "sale")
        item = result.items[0]
        self.assertEqual(item["quantity"], 10)
        self.assertEqual(item["unit_price"], Decimal("2.00"))
        self.assertEqual(result.currency, "ZAR")

    def test_umwe_no_space_quantity_rand(self):
        """'4munyu umwe R20' → 4 munyu (salt) at R20 each — regression for #100."""
        result = self._parse("4munyu umwe R20")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertIn("munyu", item["product_name"].lower())
        self.assertEqual(item["quantity"], 4)
        self.assertEqual(item["unit_price"], Decimal("20.00"))
        self.assertEqual(result.currency, "ZAR")

    # ------------------------------------------------------------------
    # Mixed-currency — regression for #74
    # ------------------------------------------------------------------

    def test_mixed_currency_usd_and_zwg(self):
        """'2 coke $3 each, 1 bread ZWG500' → separate currencies per item."""
        result = self._parse("2 coke $3 each, 1 bread ZWG500")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 2)
        by_name = {item["product_name"].lower(): item for item in result.items}
        coke = next((v for k, v in by_name.items() if "coke" in k), None)
        bread = next((v for k, v in by_name.items() if "bread" in k), None)
        self.assertIsNotNone(coke)
        self.assertIsNotNone(bread)
        self.assertEqual(coke["unit_price"], Decimal("3.00"))
        self.assertEqual(coke.get("currency"), "USD")
        self.assertEqual(bread["unit_price"], Decimal("500.00"))
        self.assertEqual(bread.get("currency"), "ZWG")

    # ------------------------------------------------------------------
    # Shona number words
    # ------------------------------------------------------------------

    def test_shona_number_words(self):
        """'mazai maviri ne chips matatu' → 2 mazai, 3 chips, no prices."""
        result = self._parse("mazai maviri ne chips matatu")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 2)
        quantities = {item["product_name"].lower(): item["quantity"] for item in result.items}
        self.assertEqual(quantities.get("mazai"), 2)
        self.assertEqual(quantities.get("chips"), 3)
        for item in result.items:
            self.assertIsNone(item["unit_price"])

    # ------------------------------------------------------------------
    # Code-switching (Shona + English mixed)
    # ------------------------------------------------------------------

    def test_ne_joins_items(self):
        """'2 coke ne 3 fanta' → 2 coke, 3 fanta."""
        result = self._parse("2 coke ne 3 fanta")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 2)
        quantities = {item["product_name"].lower(): item["quantity"] for item in result.items}
        self.assertEqual(quantities.get("coke"), 2)
        self.assertEqual(quantities.get("fanta"), 3)

    def test_mixed_shona_english_with_price(self):
        """'5 bread imwe R3 ne 2 coke' → 5 bread @ R3, 2 coke no price."""
        result = self._parse("5 bread imwe R3 ne 2 coke")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 2)
        by_name = {item["product_name"].lower(): item for item in result.items}
        bread = next((v for k, v in by_name.items() if "bread" in k), None)
        coke = next((v for k, v in by_name.items() if "coke" in k), None)
        self.assertIsNotNone(bread)
        self.assertIsNotNone(coke)
        self.assertEqual(bread["quantity"], 5)
        self.assertEqual(bread["unit_price"], 3.0)
        self.assertEqual(coke["quantity"], 2)
        self.assertIsNone(coke["unit_price"])

    def test_cents_means_usd(self):
        """"4 games for 25 cents each" → USD 0.25 each (cents in Zim = US cents)."""
        result = self._parse("4 games for 25 cents each")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item["quantity"], 4)
        self.assertAlmostEqual(item["unit_price"], 0.25)
        self.assertEqual(item["currency"], "USD")

    # ------------------------------------------------------------------
    # Other intent (non-sale messages)
    # ------------------------------------------------------------------

    def test_will_start_tomorrow_is_deferral(self):
        """"Will start tomorrow" → intent=other, sub_intent=deferral."""
        result = self._parse("Will start tomorrow")
        self.assertEqual(result.intent, "other")
        self.assertEqual(result.other_sub_intent, "deferral")

    def test_will_get_back_to_you_is_deferral(self):
        """"Will get back to you tomorrow" → intent=other, sub_intent=deferral."""
        result = self._parse("Will get back to you tomorrow")
        self.assertEqual(result.intent, "other")
        self.assertEqual(result.other_sub_intent, "deferral")

    def test_greeting_is_other_greeting(self):
        """"Hi" → intent=other, sub_intent=greeting (no response sent)."""
        result = self._parse("Hi")
        self.assertEqual(result.intent, "other")
        self.assertEqual(result.other_sub_intent, "greeting")

    # ------------------------------------------------------------------
    # ×-quantity suffix — regression for #190
    # ------------------------------------------------------------------

    def test_multiplication_sign_quantity(self):
        """'Plate Beef $1×16' → 16 × Plate Beef @ $1.00 (× suffix = quantity)."""
        result = self._parse("Plate Beef $1×16")
        self.assertEqual(result.intent, "sale")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertIn("beef", item["product_name"].lower())
        self.assertEqual(item["quantity"], 16)
        self.assertEqual(item["unit_price"], Decimal("1.00"))
        self.assertEqual(result.currency, "USD")
