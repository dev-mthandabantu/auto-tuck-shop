"""A sale must never be read as a cost (#409, Brighton's review): with a cost question open
for bread, an owner who sells bread again ("bread 2") has made a sale, not set a cost."""

import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.core.models import Company
from apps.whatsapp.services import cost_handler, webhook_handler
from unit_tests.async_helpers import run_async_test
from unit_tests.test_cost_flow import OWNER, _CostFlowBase
from apps.whatsapp.services.cost_handler import parse_cost_reply

PENDING = {1: "Bread", 2: "Coke"}


class NamedAmountShapeTest(SimpleTestCase):
    def test_a_whole_number_after_a_product_is_a_sale_not_a_cost(self):
        for text in ("bread 2", "bread 40", "bread 0", "bread 2, coke 3", "BREAD 12", "bread  7"):
            self.assertIsNone(parse_cost_reply(text, PENDING), text)

    def test_a_decimal_point_makes_it_a_cost(self):
        for text, amount in (("bread 0.80", "0.80"), ("bread 1.00", "1.00"), ("bread 0,80", "0.80"), ("bread 12.50", "12.50")):
            reply = parse_cost_reply(text, PENDING)
            self.assertEqual(str(reply.entries[0].amount), amount, text)

    def test_a_currency_makes_it_a_cost(self):
        for text, currency in (("bread $1", "USD"), ("bread R2", "ZAR"), ("bread 5 zwg", "ZWG"),
                               ("bread ZiG 12", "ZWG"), ("bread 3 usd", "USD"), ("bread 40 rand", "ZAR")):
            reply = parse_cost_reply(text, PENDING)
            self.assertEqual(reply.entries[0].currency, currency, text)

    def test_two_products_both_need_the_shape(self):
        self.assertIsNotNone(parse_cost_reply("bread 0.80, coke $1", PENDING))
        self.assertIsNone(parse_cost_reply("bread 0.80, coke 3", PENDING))  # one is a sale: take none of it

    def test_the_example_the_question_shows_still_works(self):
        self.assertIsNotNone(parse_cost_reply("Bread 1.00, Coke 1.00", PENDING))
        self.assertIsNotNone(parse_cost_reply("$1.00", {1: "Bread"}))

    def test_a_bare_amount_for_one_product_is_unaffected(self):
        for text in ("0.80", "$1", "2", "40"):
            self.assertIsNotNone(parse_cost_reply(text, {1: "Bread"}), text)

    def test_an_amount_before_the_name_is_still_a_sale(self):
        self.assertIsNone(parse_cost_reply("2 bread", PENDING))
        self.assertIsNone(parse_cost_reply("2 bread $1", PENDING))


class SaleWhileQuestionOpenTest(_CostFlowBase):
    def test_bread_2_is_not_saved_and_the_question_closes(self):
        self._open_question(self.bread)

        handled = self._reply("bread 2")

        self.assertFalse(handled)  # carries on to sale parsing
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [])
        self.send.assert_not_awaited()
        self.send_buttons.assert_not_awaited()

    def test_a_real_cost_still_saves(self):
        self._open_question(self.bread)

        self.assertTrue(self._reply("bread 0.80"))
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.80"))

    def test_a_dollar_cost_still_saves(self):
        self._open_question(self.bread)

        self.assertTrue(self._reply("bread $1"))
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("1"))

    def test_no_prompt_is_sent_for_a_whole_number_even_when_it_is_above_the_price(self):
        # the old behaviour asked "Is that right?" and tapping No still lost the sale
        from apps.catalog.models import ProductPrice
        ProductPrice.objects.create(product=self.bread, price=Decimal("1.00"), currency="USD")
        self._open_question(self.bread)

        self.assertFalse(self._reply("bread 2"))
        self.send_buttons.assert_not_awaited()

    def test_through_the_real_entry_point_the_sale_is_parsed_and_recorded(self):
        """The message must reach sale parsing, not stop at the cost reader."""
        self._open_question(self.bread)
        sale_result = SimpleNamespace(intent="sale", confidence=1.0, notes=None, currency="USD",
                                      items=[{"product_name": "bread", "quantity": 2, "unit_price": None}])
        parse = AsyncMock(return_value=sale_result)
        process = AsyncMock()
        send = AsyncMock()
        from apps.core.models import UserProfile
        from django.contrib.auth.models import User
        user = User.objects.create_user(username="+263770009999")
        owner = UserProfile.objects.create(user=user, company=self.company, phone_number="+263770009999",
                                           role=UserProfile.Role.OWNER, language="en")
        owner = UserProfile.objects.select_related("company").get(pk=owner.pk)

        with patch.object(webhook_handler, "parse_message_unified", new=parse), \
             patch.object(webhook_handler, "_send_response", new=send), \
             patch.object(cost_handler, "_send_response", new=send), \
             patch("apps.whatsapp.services.sale_handler.process_sale_message_unified", new=process), \
             patch.object(webhook_handler.settings, "ENABLE_WHATSAPP_TYPING", False):
            run_async_test(webhook_handler._process_message_async("wamid.s1", owner.phone_number, "bread 2", owner))

        parse.assert_awaited_once()
        process.assert_awaited_once()
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [])
