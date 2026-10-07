"""Cost-price flow (#409): asking once after a confirmed sale, reading the reply
without mixing it up with a selling price, and saving it on the product."""

import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from apps.catalog.models import Product, ProductPrice
from apps.core.models import Company
from apps.sales.models import Sale, SaleItem
from apps.whatsapp.services import cost_handler
from apps.whatsapp.services.cost_handler import (
    CostEntry,
    handle_pending_cost_reply,
    handle_set_cost_intent,
    open_cost_question_after_sale,
    parse_cost_reply,
    sale_confirmed_with_cost_question,
    _process_cost_confirm_async,
)
from apps.whatsapp.services.message_parser import ParsedCostItem
from unit_tests.async_helpers import run_async_test

PENDING = {1: "Bread", 2: "Eggs"}
OWNER = SimpleNamespace(role="owner", language="en")
ASSISTANT = SimpleNamespace(role="assistant", language="en")


class ParseCostReplyTest(SimpleTestCase):
    def test_product_then_amount(self):
        reply = parse_cost_reply("bread 0.80", PENDING)
        self.assertEqual(reply.entries, [CostEntry(1, Decimal("0.80"), None)])

    def test_several_products_with_commas_and_conjunctions(self):
        for text in ("bread 0.80, eggs 0.20", "bread 0.80 and eggs 0.20", "bread 0.80 ne eggs 0.20", "bread 0.80\neggs 0.20"):
            reply = parse_cost_reply(text, PENDING)
            self.assertEqual([(e.product_id, e.amount) for e in reply.entries],
                             [(1, Decimal("0.80")), (2, Decimal("0.20"))], text)

    def test_cost_words_and_currency_symbols(self):
        self.assertEqual(parse_cost_reply("bread costs $0.80", PENDING).entries[0].currency, "USD")
        self.assertEqual(parse_cost_reply("bread R8", PENDING).entries[0].currency, "ZAR")
        self.assertEqual(parse_cost_reply("bread 12 zwg", PENDING).entries[0].currency, "ZWG")
        self.assertEqual(parse_cost_reply("bread ZiG 12", PENDING).entries[0].currency, "ZWG")
        self.assertIsNone(parse_cost_reply("bread 0.80", PENDING).entries[0].currency)

    def test_decimal_comma_is_not_a_separator(self):
        reply = parse_cost_reply("bread 0,80", PENDING)
        self.assertEqual(reply.entries, [CostEntry(1, Decimal("0.80"), None)])

    def test_singular_and_plural_match(self):
        reply = parse_cost_reply("egg 0.20", PENDING)
        self.assertEqual(reply.entries[0].product_id, 2)

    def test_bare_amount_only_when_a_single_product_was_asked(self):
        self.assertEqual(parse_cost_reply("$0.80", {1: "Bread"}).entries[0].product_id, 1)
        self.assertIsNone(parse_cost_reply("$0.80", PENDING))

    def test_skip_words(self):
        for text in ("skip", "Later", "no", "hapana", "not now"):
            self.assertTrue(parse_cost_reply(text, PENDING).skipped, text)

    def test_each_shona_skip_word_skips(self):
        for text in ("siya", "zvisiye", "gare gare", "handizivi", "Siya.", "GARE GARE!", "  zvisiye  "):
            reply = parse_cost_reply(text, PENDING)
            self.assertIsNotNone(reply, text)
            self.assertTrue(reply.skipped, text)

    def test_skip_words_only_skip_when_they_are_the_whole_message(self):
        # a product that happens to contain a skip word must still be read as a product
        self.assertIsNone(parse_cost_reply("siya bread", PENDING))
        self.assertIsNone(parse_cost_reply("gare gare 2", PENDING))

    # --- the cost-versus-selling-price protections ---

    def test_quantity_before_the_name_is_a_sale_not_a_cost(self):
        self.assertIsNone(parse_cost_reply("1 bread", PENDING))
        self.assertIsNone(parse_cost_reply("2 bread $1", PENDING))

    def test_a_sale_of_another_product_is_not_a_cost(self):
        self.assertIsNone(parse_cost_reply("2 coke $3", PENDING))
        self.assertIsNone(parse_cost_reply("coke 3", PENDING))

    def test_one_unmatched_part_rejects_the_whole_reply(self):
        self.assertIsNone(parse_cost_reply("bread 0.80, coke 3", PENDING))

    def test_same_product_twice_is_ambiguous(self):
        self.assertIsNone(parse_cost_reply("bread 0.80, bread 1", PENDING))

    def test_text_without_an_amount_or_with_extra_words(self):
        self.assertIsNone(parse_cost_reply("thanks", PENDING))
        self.assertIsNone(parse_cost_reply("bread 0.80 yesterday", PENDING))
        self.assertIsNone(parse_cost_reply("", PENDING))

    def test_absurd_amount_is_rejected(self):
        self.assertIsNone(parse_cost_reply("bread 999999999", PENDING))


def _patch_send():
    return (
        patch.object(cost_handler, "_send_response", new=AsyncMock()),
        patch.object(cost_handler, "_send_response_with_buttons", new=AsyncMock()),
    )


class _CostFlowBase(TransactionTestCase):
    # The handlers reach the ORM through sync_to_async on another thread, so
    # writes must be committed to be visible (same reason as test_sale_confirmation).
    def setUp(self):
        self.company = Company.objects.create(name="Cost Shop", slug="cost-shop", active=True)
        self.bread = Product.objects.create(name="Bread", company=self.company)
        self.eggs = Product.objects.create(name="Eggs", company=self.company)
        p1, p2 = _patch_send()
        self.send = p1.start()
        self.send_buttons = p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def _confirmed_sale(self, *products, currency="USD", when=None):
        sale = Sale.objects.create(company=self.company, status=Sale.Status.CONFIRMED)
        if when:
            Sale.objects.filter(id=sale.id).update(sale_timestamp=when)
        for product in products:
            SaleItem.objects.create(sale=sale, product=product, quantity=1,
                                    unit_price=Decimal("1.00"), currency=currency)
            if not product.prices.exists():
                ProductPrice.objects.create(product=product, price=Decimal("1.00"), currency=currency)
        return sale

    def _open_question(self, *products):
        Company.objects.filter(id=self.company.id).update(
            pending_cost_product_ids=[p.id for p in products], pending_cost_asked_at=timezone.now()
        )

    def _reply(self, text):
        company = Company.objects.get(id=self.company.id)
        return run_async_test(handle_pending_cost_reply("+263770000000", company, text, "en", user_profile=OWNER))

    def _sent_text(self):
        return " ".join(str(c.args[1]) for c in self.send.await_args_list)


class AskForCostsTest(_CostFlowBase):
    def _open(self, sale):
        return run_async_test(open_cost_question_after_sale(self.company.id, sale.id))

    def test_opens_a_question_for_every_product_without_a_cost(self):
        sale = self._confirmed_sale(self.bread, self.eggs)

        products = self._open(sale)

        self.assertCountEqual([p.name for p in products], ["Bread", "Eggs"])
        company = Company.objects.get(id=self.company.id)
        self.assertCountEqual(company.pending_cost_product_ids, [self.bread.id, self.eggs.id])
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost_ask_count, 1)

    def test_skips_products_that_already_have_a_cost(self):
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("0.80"), cost_currency="USD")
        sale = self._confirmed_sale(self.bread, self.eggs)

        products = self._open(sale)

        self.assertEqual([p.name for p in products], ["Eggs"])

    def test_nothing_to_ask_when_every_product_has_a_cost(self):
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("0.80"), cost_currency="USD")
        sale = self._confirmed_sale(self.bread)

        self.assertEqual(self._open(sale), [])
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [])

    def test_stops_asking_after_two_asks(self):
        Product.objects.filter(id=self.bread.id).update(cost_ask_count=2)
        sale = self._confirmed_sale(self.bread)

        self.assertEqual(self._open(sale), [])

    def test_never_asks_for_more_than_five_products_at_once(self):
        products = [Product.objects.create(name=f"Item {i}", company=self.company) for i in range(8)]
        sale = self._confirmed_sale(*products)

        self.assertEqual(len(self._open(sale)), 5)
        self.assertEqual(len(Company.objects.get(id=self.company.id).pending_cost_product_ids), 5)

    def test_message_is_one_text_with_recorded_and_the_question(self):
        text = sale_confirmed_with_cost_question([self.bread], "en")
        self.assertIn("Sale recorded", text)
        self.assertIn("one *Bread*", text)
        self.assertIn("e.g. *$1.00*", text)  # a bare amount is enough for a single product
        self.assertNotIn("these", text)

    def test_message_example_names_the_products_when_there_are_several(self):
        text = sale_confirmed_with_cost_question([self.bread, self.eggs], "en")
        self.assertIn("• Bread\n• Eggs", text)
        self.assertIn("Reply like: *Bread 1.00, Eggs 1.00*", text)

    def test_shona_message_renders(self):
        text = sale_confirmed_with_cost_question([self.bread], "sn")
        self.assertIn("Bread", text)
        self.assertIn("$1.00", text)


class ConfirmButtonSendsOneMessageTest(_CostFlowBase):
    """Tapping 'Looks good' sends a single message: recorded + the cost question."""

    def _tap_confirm(self, sale):
        from apps.whatsapp.services import sale_handler

        profile = SimpleNamespace(language="en", company_id=self.company.id, role="owner")
        sale_send = AsyncMock()
        with patch.object(sale_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)), \
             patch.object(sale_handler, "_send_response", new=sale_send), \
             patch.object(sale_handler, "_send_response_with_buttons", new=AsyncMock()), \
             patch.object(sale_handler, "_company_needs_closing_time", new=AsyncMock(return_value=False)):
            run_async_test(sale_handler.process_sale_button_action_async("confirm", "+263770000000", sale.confirmation_message_sid))
        return sale_send

    def _pending_sale(self, *products):
        sale = Sale.objects.create(company=self.company, confirmation_message_sid="sid_x")
        for product in products:
            SaleItem.objects.create(sale=sale, product=product, quantity=2, unit_price=Decimal("2.00"), currency="USD")
        return sale

    def test_unknown_cost_gets_the_merged_message(self):
        sale = self._pending_sale(self.bread)

        send = self._tap_confirm(sale)

        self.assertEqual(send.await_count, 1)
        text = send.await_args.args[1]
        self.assertIn("Sale recorded", text)
        self.assertIn("how much do you pay", text)
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [self.bread.id])

    def test_known_cost_gets_the_plain_confirmation_and_no_question(self):
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("0.80"), cost_currency="USD")
        sale = self._pending_sale(self.bread)

        send = self._tap_confirm(sale)

        self.assertEqual(send.await_count, 1)
        self.assertNotIn("how much do you pay", send.await_args.args[1])
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [])
        self.assertEqual(SaleItem.objects.get(sale=sale).unit_cost, Decimal("0.80"))

    def test_a_failure_opening_the_question_still_confirms_the_sale(self):
        sale = self._pending_sale(self.bread)

        with patch.object(cost_handler, "open_cost_question_after_sale", new=AsyncMock(side_effect=RuntimeError("boom"))):
            send = self._tap_confirm(sale)

        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.CONFIRMED)
        self.assertEqual(send.await_args.args[1], "✓ Great, sale recorded!")


class PendingCostReplyTest(_CostFlowBase):
    def test_reply_saves_cost_echoes_selling_price_and_closes_the_question(self):
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)

        handled = self._reply("bread 0.80")

        self.assertTrue(handled)
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.80"))
        self.assertEqual(self.bread.cost_currency, "USD")  # from the sale price, none typed
        text = self._sent_text()
        self.assertIn("✓ Cost saved — Bread costs $0.80.", text)
        self.assertIn("Customer pays: $1.00", text)
        self.assertIn("You pay: $0.80", text)
        self.assertIn("Profit: $0.20 each", text)
        self.assertIn("calculate your profit", text)
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [])

    def test_batch_reply_saves_every_product(self):
        self._confirmed_sale(self.bread, self.eggs)
        self._open_question(self.bread, self.eggs)

        self._reply("bread 0.80, eggs 0.20")

        self.bread.refresh_from_db()
        self.eggs.refresh_from_db()
        self.assertEqual((self.bread.cost, self.eggs.cost), (Decimal("0.80"), Decimal("0.20")))

    def test_no_profit_line_when_cost_and_price_are_in_different_currencies(self):
        self._confirmed_sale(self.bread, currency="USD")
        self._open_question(self.bread)

        self._reply("bread ZWG 12")

        text = self._sent_text()
        self.assertIn("costs", text)
        self.assertNotIn("Profit", text)

    def test_confirmed_high_cost_shows_a_negative_profit(self):
        self._confirmed_sale(self.bread)
        profile = SimpleNamespace(language="en", company_id=self.company.id, role="owner")

        with patch.object(cost_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)):
            run_async_test(_process_cost_confirm_async("+263770000000", f"costyes_{self.bread.id}_USD_8_none"))

        self.assertIn("Profit: -$7.00 each", self._sent_text())

    def test_typed_currency_wins_over_the_sale_currency(self):
        self._confirmed_sale(self.bread, currency="USD")
        self._open_question(self.bread)

        self._reply("bread ZWG 12")

        self.bread.refresh_from_db()
        self.assertEqual((self.bread.cost, self.bread.cost_currency), (Decimal("12.00"), "ZWG"))

    def test_a_sale_message_falls_through_and_closes_the_question(self):
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)

        handled = self._reply("2 coke $3")

        self.assertFalse(handled)
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [])
        self.send.assert_not_awaited()

    def test_one_bread_is_a_sale_not_a_cost_of_one_dollar(self):
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)

        self.assertFalse(self._reply("1 bread"))
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)

    def test_skip_saves_nothing(self):
        self._open_question(self.bread)

        self.assertTrue(self._reply("skip"))
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [])

    def test_no_open_question_means_nothing_is_read_as_a_cost(self):
        self.assertFalse(self._reply("bread 0.80"))
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)

    def test_expired_question_is_ignored(self):
        Company.objects.filter(id=self.company.id).update(
            pending_cost_product_ids=[self.bread.id],
            pending_cost_asked_at=timezone.now() - datetime.timedelta(hours=5),
        )

        self.assertFalse(self._reply("bread 0.80"))
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)

    def test_cost_not_lower_than_the_price_is_not_saved_without_confirmation(self):
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)

        self.assertTrue(self._reply("bread $8"))

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.send_buttons.assert_awaited_once()
        ids = [b["id"] for b in self.send_buttons.await_args.args[2]]
        self.assertEqual(ids, [f"costyes_{self.bread.id}_USD_8_none", "costno"])

    def test_confirming_the_high_cost_saves_it(self):
        self._confirmed_sale(self.bread)
        profile = SimpleNamespace(language="en", company_id=self.company.id, role="owner")

        with patch.object(cost_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)):
            run_async_test(_process_cost_confirm_async("+263770000000", f"costyes_{self.bread.id}_USD_8_none"))

        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("8"))

    def test_declining_the_high_cost_saves_nothing(self):
        profile = SimpleNamespace(language="en", company_id=self.company.id, role="owner")

        with patch.object(cost_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)):
            run_async_test(_process_cost_confirm_async("+263770000000", "costno"))

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)

    def test_malformed_confirm_buttons_save_nothing(self):
        profile = SimpleNamespace(language="en", company_id=self.company.id, role="owner")
        for bad in (f"costyes_{self.bread.id}_USD_NaN_none", f"costyes_{self.bread.id}_USD_Infinity_none",
                    f"costyes_{self.bread.id}_USD_-5_none", f"costyes_{self.bread.id}_USD_999999999_none",
                    f"costyes_{self.bread.id}_XXX_8_none", f"costyes_{self.bread.id}_USD_8_NaN",
                    f"costyes_{self.bread.id}_USD_8", "costyes_abc_USD_8_none", "costyes_1"):
            with patch.object(cost_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)):
                run_async_test(_process_cost_confirm_async("+263770000000", bad))
            self.bread.refresh_from_db()
            self.assertIsNone(self.bread.cost, bad)

    def test_cost_confirm_cannot_touch_another_shops_product(self):
        other = Company.objects.create(name="Other", slug="other", active=True)
        profile = SimpleNamespace(language="en", company_id=other.id, role="owner")

        with patch.object(cost_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)):
            run_async_test(_process_cost_confirm_async("+263770000000", f"costyes_{self.bread.id}_USD_8_none"))

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)


class BackfillTest(_CostFlowBase):
    def test_first_cost_fills_todays_unknown_lines_only(self):
        today_sale = self._confirmed_sale(self.bread)
        old_sale = self._confirmed_sale(self.bread, when=timezone.now() - datetime.timedelta(days=3))
        snapshotted = self._confirmed_sale(self.bread)
        SaleItem.objects.filter(sale=snapshotted).update(unit_cost=Decimal("0.70"), cost_currency="USD")
        self._open_question(self.bread)

        self._reply("bread 0.80")

        self.assertEqual(SaleItem.objects.get(sale=today_sale).unit_cost, Decimal("0.80"))
        self.assertIsNone(SaleItem.objects.get(sale=old_sale).unit_cost)
        self.assertEqual(SaleItem.objects.get(sale=snapshotted).unit_cost, Decimal("0.70"))

    def test_backfill_skips_lines_sold_in_another_currency(self):
        zar_sale = self._confirmed_sale(self.bread, currency="ZAR")
        self._open_question(self.bread)

        self._reply("bread $0.80")

        self.assertIsNone(SaleItem.objects.get(sale=zar_sale).unit_cost)

    def test_pending_and_cancelled_sales_are_not_backfilled(self):
        pending = self._confirmed_sale(self.bread)
        Sale.objects.filter(id=pending.id).update(status=Sale.Status.PENDING)
        cancelled = self._confirmed_sale(self.bread)
        Sale.objects.filter(id=cancelled.id).update(status=Sale.Status.CANCELLED)
        self._open_question(self.bread)

        self._reply("bread 0.80")

        self.assertIsNone(SaleItem.objects.get(sale=pending).unit_cost)
        self.assertIsNone(SaleItem.objects.get(sale=cancelled).unit_cost)


class SetCostIntentTest(_CostFlowBase):
    def _run(self, *items):
        company = Company.objects.get(id=self.company.id)
        result = SimpleNamespace(cost_items=list(items))
        run_async_test(handle_set_cost_intent("+263770000000", company, result, "en", user_profile=OWNER))

    def test_updates_the_cost_of_an_existing_product(self):
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("0.80"), cost_currency="USD")

        self._run(ParsedCostItem("bread", Decimal("0.90"), "USD"))

        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.90"))

    def test_update_never_rewrites_a_sale_that_already_has_its_cost(self):
        sale = self._confirmed_sale(self.bread)
        SaleItem.objects.filter(sale=sale).update(unit_cost=Decimal("0.80"), cost_currency="USD")

        self._run(ParsedCostItem("bread", Decimal("0.90"), "USD"))

        self.assertEqual(SaleItem.objects.get(sale=sale).unit_cost, Decimal("0.80"))

    def test_unknown_product_is_not_created(self):
        self._run(ParsedCostItem("airtime", Decimal("0.90"), "USD"))

        self.assertFalse(Product.objects.filter(company=self.company, name__iexact="airtime").exists())
        self.assertIn("airtime", self._sent_text())

    def test_missing_amount_asks_for_it(self):
        self._run(ParsedCostItem("bread", None, None))

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertIn("How much", self._sent_text())

    def test_cost_is_never_written_to_the_selling_price(self):
        ProductPrice.objects.create(product=self.bread, price=Decimal("1.00"), currency="USD")

        self._run(ParsedCostItem("bread", Decimal("0.80"), "USD"))

        self.assertEqual(self.bread.current_price, Decimal("1.00"))
        self.assertEqual(ProductPrice.objects.filter(product=self.bread).count(), 1)


class ParserSetCostTest(TestCase):
    """The set_cost branch of parse_message_unified, with the LLM stubbed."""

    def _parse(self, response):
        from apps.whatsapp.services.message_parser import parse_message_unified
        from services.openrouter.client import ParseJsonResult

        fake = AsyncMock(return_value=ParseJsonResult(response=response, prompt_tokens=1, completion_tokens=1))
        with patch("apps.whatsapp.services.message_parser.OpenRouterClient") as client:
            client.return_value.parse_json_response = fake
            return run_async_test(parse_message_unified("bread costs me $0.80"))

    def test_set_cost_items_are_parsed(self):
        result = self._parse({"intent": "set_cost", "confidence": 0.9,
                              "items": [{"product_name": "bread", "cost": 0.8, "currency": "USD"}]})
        self.assertEqual(result.intent, "set_cost")
        self.assertEqual(result.cost_items, [ParsedCostItem("bread", Decimal("0.8"), "USD")])
        self.assertEqual(result.items, [])  # never leaks into sale items

    def test_bad_values_are_dropped_not_trusted(self):
        result = self._parse({"intent": "set_cost", "confidence": 0.9, "items": [
            {"product_name": "bread", "cost": -1, "currency": "XXX"},
            {"product_name": "", "cost": 1},
            {"product_name": "eggs", "cost": "abc"},
        ]})
        self.assertEqual(result.cost_items, [ParsedCostItem("bread", None, None), ParsedCostItem("eggs", None, None)])


class BareAmountVersusClosingPromptTest(_CostFlowBase):
    """'6' can be the answer to an open closing-time prompt, so a bare number must
    not be saved as a cost while one is waiting (review finding on #409)."""

    def _reply_with_prompt(self, text):
        company = Company.objects.get(id=self.company.id)
        return run_async_test(handle_pending_cost_reply(
            "+263770000000", company, text, "en", user_profile=OWNER, closing_prompt_open=True))

    def test_bare_number_is_left_alone_and_the_question_stays_open(self):
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)

        self.assertFalse(self._reply_with_prompt("6"))

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [self.bread.id])

    def test_an_amount_with_a_currency_is_still_a_cost(self):
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)

        self.assertTrue(self._reply_with_prompt("$0.80"))

        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.80"))

    def test_product_and_amount_is_still_a_cost(self):
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)

        self.assertTrue(self._reply_with_prompt("bread 6.00"))

    def test_without_a_closing_prompt_a_bare_number_is_a_cost(self):
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)

        self.assertTrue(self._reply("0.80"))

    def test_parser_flag(self):
        self.assertIsNone(parse_cost_reply("6", {1: "Bread"}, allow_bare=False))
        self.assertIsNotNone(parse_cost_reply("$6", {1: "Bread"}, allow_bare=False))
        self.assertIsNotNone(parse_cost_reply("bread 6.00", {1: "Bread"}, allow_bare=False))


class VoiceReplyTest(_CostFlowBase):
    """A spoken answer to the cost question goes through the same check as a typed one."""

    def _speak(self, transcript):
        from django.test import override_settings
        from apps.whatsapp.services import media_handler

        company = Company.objects.get(id=self.company.id)
        profile = SimpleNamespace(company=company, language="en", role="owner")
        client = SimpleNamespace(download_media=AsyncMock(return_value=(b"audio", "audio/ogg")))
        parse = AsyncMock(side_effect=AssertionError("must not reach the LLM"))
        sale = AsyncMock()
        with override_settings(ELEVENLABS_API_KEY="test"), \
             patch.object(media_handler, "get_whatsapp_client", return_value=client), \
             patch.object(media_handler, "_upload_to_r2", new=AsyncMock(return_value=None)), \
             patch("services.elevenlabs.ElevenLabsClient") as eleven, \
             patch("apps.whatsapp.services.message_parser._get_active_products", new=AsyncMock(return_value=[])), \
             patch("apps.whatsapp.services.message_parser.parse_message_unified", new=parse), \
             patch("apps.whatsapp.services.sale_handler.process_sale_message_unified", new=sale):
            eleven.return_value.transcribe_audio = AsyncMock(return_value=transcript)
            run_async_test(media_handler.process_audio_message_async("wamid.v1", "+263770000000", "m1", profile))
        return parse, sale

    def test_spoken_cost_reply_is_saved_and_never_recorded_as_a_sale(self):
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)

        parse, sale = self._speak("bread 0.80")

        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.80"))
        parse.assert_not_awaited()
        sale.assert_not_awaited()
