"""Fixes from the second round of fresh-context review on #409: a zero cost, a stale
"Yes" tap, names with a hyphen, rollback of migration 0005, a loss-making product in
"most profitable", and two small period-parsing gaps."""

import datetime
import importlib
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.apps import apps as django_apps
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from django.utils import timezone

from apps.catalog.models import Product, ProductPrice
from apps.core.models import Company
from apps.sales.models import Sale, SaleItem
from apps.sales.services import ProfitSummary, summarize_profit
from apps.whatsapp.services import cost_handler, webhook_handler
from apps.whatsapp.services.business_reports import build_period_summary, resolve_period
from apps.whatsapp.services.cost_handler import (
    _process_cost_confirm_async,
    handle_pending_cost_reply,
    handle_set_cost_intent,
    parse_cost_reply,
)
from apps.whatsapp.services.message_parser import ParsedCostItem
from unit_tests.async_helpers import run_async_test
from unit_tests.test_cost_flow import ASSISTANT, OWNER, _CostFlowBase

TODAY = datetime.date(2026, 10, 6)


class ZeroCostTest(_CostFlowBase):
    def _reply_zero(self):
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)
        return self._reply("bread 0.00")

    def test_a_typed_zero_is_not_saved_without_a_tap(self):
        self.assertTrue(self._reply_zero())

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.send_buttons.assert_awaited_once()
        text = self.send_buttons.await_args.args[1]
        self.assertIn("you pay nothing", text)
        ids = [b["id"] for b in self.send_buttons.await_args.args[2]]
        self.assertEqual(ids, [f"costyes_{self.bread.id}_USD_0.00_none", "costno"])

    def test_a_zero_from_the_cost_intent_needs_a_tap_too(self):
        company = Company.objects.get(id=self.company.id)
        result = SimpleNamespace(cost_items=[ParsedCostItem("bread", Decimal("0"), "USD")])

        run_async_test(handle_set_cost_intent("+263770000000", company, result, "en", user_profile=OWNER))

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.send_buttons.assert_awaited_once()

    def test_tapping_yes_saves_a_genuinely_free_product(self):
        profile = SimpleNamespace(language="en", company_id=self.company.id, role="owner")

        with patch.object(cost_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)):
            run_async_test(_process_cost_confirm_async("+263770000000", f"costyes_{self.bread.id}_USD_0_none"))

        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0"))

    def test_a_small_real_cost_is_not_treated_as_zero(self):
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)

        self._reply("bread 0.05")

        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.05"))


class StaleYesTapTest(_CostFlowBase):
    def _tap(self, button_id):
        profile = SimpleNamespace(language="en", company_id=self.company.id, role="owner")
        with patch.object(cost_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)):
            run_async_test(_process_cost_confirm_async("+263770000000", button_id))

    def test_a_tap_after_the_cost_was_set_another_way_is_refused(self):
        # the prompt was made when bread had no cost (id ends _none) ...
        stale_id = f"costyes_{self.bread.id}_USD_1.20_none"
        # ... then the owner set 0.80 by hand
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("0.80"), cost_currency="USD")

        self._tap(stale_id)

        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.80"))
        self.assertIn("cost changed", self.send.await_args.args[1])

    def test_a_tap_while_the_cost_is_unchanged_still_works(self):
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("1.00"), cost_currency="USD")

        self._tap(f"costyes_{self.bread.id}_USD_8_1.00")

        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("8"))

    def test_the_prompt_records_the_cost_it_saw(self):
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("1.00"), cost_currency="USD")
        self._confirmed_sale(self.bread)
        self._open_question(self.bread)

        self._reply("bread $8")

        ids = [b["id"] for b in self.send_buttons.await_args.args[2]]
        self.assertEqual(ids[0], f"costyes_{self.bread.id}_USD_8_1.00")


class HyphenatedNameTest(SimpleTestCase):
    def test_a_name_with_a_hyphen_matches_its_own_reply(self):
        reply = parse_cost_reply("Coca-Cola 1.20", {7: "Coca-Cola"})
        self.assertEqual([(e.product_id, e.amount) for e in reply.entries], [(7, Decimal("1.20"))])

    def test_the_hyphen_is_optional_in_the_reply(self):
        self.assertIsNotNone(parse_cost_reply("coca cola 1.20", {7: "Coca-Cola"}))

    def test_a_name_containing_a_filler_word_still_matches(self):
        self.assertIsNotNone(parse_cost_reply("bread for kids 0.80", {7: "Bread for kids"}))

    def test_a_different_product_still_does_not_match(self):
        self.assertIsNone(parse_cost_reply("Sprite 1.20", {7: "Coca-Cola"}))


class Migration0008Test(TestCase):
    def test_rollback_function_gives_unknown_costs_the_old_default(self):
        migration = importlib.import_module("apps.catalog.migrations.0008_restore_zero_cost_on_rollback")
        company = Company.objects.create(name="Roll Shop", slug="roll-shop", active=True)
        unknown = Product.objects.create(name="Unknown", company=company)
        known = Product.objects.create(name="Known", company=company, cost=Decimal("1.50"), cost_currency="USD")

        migration.unknown_cost_to_zero(django_apps, None)

        unknown.refresh_from_db()
        known.refresh_from_db()
        self.assertEqual(unknown.cost, Decimal("0.00"))
        self.assertEqual(known.cost, Decimal("1.50"))

    def test_forward_does_nothing(self):
        migration = importlib.import_module("apps.catalog.migrations.0008_restore_zero_cost_on_rollback")
        company = Company.objects.create(name="Fwd Shop", slug="fwd-shop", active=True)
        unknown = Product.objects.create(name="Unknown", company=company)

        migration.nothing(django_apps, None)

        unknown.refresh_from_db()
        self.assertIsNone(unknown.cost)


class MostProfitableOnlyEarningTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Rank Shop", slug="rank-shop", active=True)
        self.bread = Product.objects.create(company=self.company, name="Bread")
        self.eggs = Product.objects.create(company=self.company, name="Eggs")
        self.coke = Product.objects.create(company=self.company, name="Coke")

    def _items(self, *lines):
        sale = Sale.objects.create(company=self.company, status=Sale.Status.CONFIRMED)
        return [SaleItem.objects.create(sale=sale, product=p, quantity=1, unit_price=Decimal(price), currency="USD",
                                        unit_cost=Decimal(cost), cost_currency="USD") for p, price, cost in lines]

    def test_a_loss_making_product_is_not_listed_as_most_profitable(self):
        items = self._items((self.bread, "2.00", "1.00"), (self.eggs, "0.50", "0.70"), (self.coke, "1.00", "0.50"))

        ranked = summarize_profit(items).most_profitable()

        self.assertEqual([n for n, _ in ranked["USD"]], ["Bread", "Coke"])

    def test_one_earner_and_one_loser_is_not_a_ranking(self):
        items = self._items((self.bread, "2.00", "1.00"), (self.eggs, "0.50", "0.70"))

        self.assertEqual(summarize_profit(items).most_profitable(), {})

    def test_a_break_even_product_is_not_listed(self):
        items = self._items((self.bread, "2.00", "1.00"), (self.eggs, "1.00", "1.00"))

        self.assertEqual(summarize_profit(items).most_profitable(), {})

    def test_the_total_profit_still_includes_the_loss(self):
        items = self._items((self.bread, "2.00", "1.00"), (self.eggs, "0.50", "0.70"))

        self.assertEqual(summarize_profit(items).by_currency, {"USD": Decimal("0.80")})


class PeriodParsingGapsTest(TestCase):
    def _parse(self, response):
        from apps.whatsapp.services.message_parser import parse_message_unified
        from services.openrouter.client import ParseJsonResult

        fake = AsyncMock(return_value=ParseJsonResult(response=response, prompt_tokens=1, completion_tokens=1))
        with patch("apps.whatsapp.services.message_parser.OpenRouterClient") as client:
            client.return_value.parse_json_response = fake
            return run_async_test(parse_message_unified("profit last week"))

    def test_an_infinite_count_from_the_model_is_dropped_not_a_crash(self):
        result = self._parse({"intent": "sales_query", "confidence": 1, "timeframe": "weeks",
                              "period_count": float("inf"), "metric": "profit"})
        self.assertIsNone(result.period_count)

    def test_weeks_with_no_count_means_one_week(self):
        start, end, label = resolve_period("weeks", None, TODAY)
        self.assertEqual((start, end), (datetime.date(2026, 9, 30), TODAY))
        self.assertEqual(label, "Last week (30 Sep - 06 Oct)")


class WeeksNoCountRoutingTest(TransactionTestCase):
    def test_weeks_with_no_count_through_the_query_handler(self):
        company = Company.objects.create(name="Wk Shop", slug="wk-shop", currency="USD")
        bread = Product.objects.create(company=company, name="Bread")
        sale = Sale.objects.create(company=company, status=Sale.Status.CONFIRMED,
                                   sale_timestamp=timezone.now() - datetime.timedelta(days=3))
        SaleItem.objects.create(sale=sale, product=bread, quantity=1, unit_price=Decimal("1.00"), currency="USD")
        result = SimpleNamespace(timeframe="weeks", period_count=None, metric="revenue")
        send = AsyncMock()

        with patch.object(webhook_handler, "_send_response", new=send):
            run_async_test(webhook_handler._handle_sales_query(
                "+263770000000", SimpleNamespace(company=company, role="owner"), result, lang="en"))

        self.assertIn("Last week", send.await_args.args[1])
        self.assertIn("$1.00", send.await_args.args[1])


class LocalMidnightTest(TestCase):
    """A sale a minute either side of local midnight lands on the right day."""

    def test_sales_at_the_edges_of_the_local_day(self):
        company = Company.objects.create(name="Mid Shop", slug="mid-shop", currency="USD")
        bread = Product.objects.create(company=company, name="Bread")
        today = timezone.localdate()
        midnight = timezone.make_aware(datetime.datetime.combine(today, datetime.time.min))

        def sale(at, price):
            s = Sale.objects.create(company=company, status=Sale.Status.CONFIRMED)
            Sale.objects.filter(id=s.id).update(sale_timestamp=at)
            SaleItem.objects.create(sale=s, product=bread, quantity=1, unit_price=Decimal(price), currency="USD")

        sale(midnight - datetime.timedelta(seconds=30), "1.00")           # 23:59:30 yesterday
        sale(midnight + datetime.timedelta(seconds=30), "2.00")           # 00:00:30 today
        sale(midnight + datetime.timedelta(hours=23, minutes=59, seconds=30), "4.00")  # 23:59:30 today

        data = build_period_summary(company, today, today)

        self.assertEqual(data["currency_revenues"], {"USD": Decimal("6.00")})
        self.assertEqual(data["sales_count"], 2)
