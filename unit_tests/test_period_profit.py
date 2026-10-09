"""Profit questions over a date range (#409): "how much profit this week", "how
much did I make these past 2 days / last 2 weeks", and the most-profitable ranking."""

import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.models import Company
from apps.sales.models import Sale, SaleItem
from apps.whatsapp.services import webhook_handler
from apps.whatsapp.services.business_reports import (
    BusinessSnapshot,
    build_business_snapshot,
    build_period_summary,
    format_business_summary,
    format_period_summary,
    resolve_period,
)
from unit_tests.async_helpers import run_async_test

TODAY = datetime.date(2026, 10, 6)  # a Tuesday


class ResolvePeriodTest(SimpleTestCase):
    def test_this_week_runs_monday_to_today(self):
        start, end, label = resolve_period("week", None, TODAY)
        self.assertEqual((start, end), (datetime.date(2026, 10, 5), TODAY))
        self.assertEqual(label, "This week (05 Oct - 06 Oct)")

    def test_labels_are_in_the_owners_language(self):
        self.assertEqual(resolve_period("week", None, TODAY, lang="sn")[2], "Vhiki rino (05 Oct - 06 Oct)")
        self.assertEqual(resolve_period("days", 2, TODAY, lang="sn")[2], "Mazuva 2 apfuura (05 Oct - 06 Oct)")
        self.assertEqual(resolve_period("weeks", 2, TODAY, lang="sn")[2], "Mavhiki 2 apfuura (23 Sep - 06 Oct)")
        self.assertEqual(resolve_period("month", None, TODAY, lang="sn")[2], "Gumiguru")
        self.assertEqual(resolve_period("month", None, TODAY)[2], "October")
        self.assertEqual(resolve_period("today", None, TODAY, force_range=True, lang="sn")[2], "Nhasi (06 Oct)")
        self.assertEqual(resolve_period("yesterday", None, TODAY, force_range=True, lang="sn")[2], "Nezuro (05 Oct)")

    def test_every_month_has_a_name_in_every_language(self):
        for lang in ("en", "sn"):
            for month in range(1, 13):
                self.assertTrue(resolve_period("month", None, datetime.date(2026, month, 15), lang=lang)[2])

    def test_month_and_year(self):
        self.assertEqual(resolve_period("month", None, TODAY)[:2], (datetime.date(2026, 10, 1), TODAY))
        self.assertEqual(resolve_period("year", None, TODAY)[:2], (datetime.date(2026, 1, 1), TODAY))

    def test_past_two_days_is_yesterday_and_today(self):
        start, end, label = resolve_period("days", 2, TODAY)
        self.assertEqual((start, end), (datetime.date(2026, 10, 5), TODAY))
        self.assertEqual(label, "Last 2 days (05 Oct - 06 Oct)")

    def test_last_two_weeks_is_the_last_fourteen_days(self):
        start, end, label = resolve_period("weeks", 2, TODAY)
        self.assertEqual((start, end), (datetime.date(2026, 9, 23), TODAY))
        self.assertEqual(label, "Last 2 weeks (23 Sep - 06 Oct)")

    def test_last_week_with_no_number_is_seven_days(self):
        start, _, label = resolve_period("weeks", 1, TODAY)
        self.assertEqual(start, datetime.date(2026, 9, 30))
        self.assertEqual(label, "Last week (30 Sep - 06 Oct)")

    def test_one_day_or_no_count_is_a_single_day(self):
        self.assertIsNone(resolve_period("days", 1, TODAY))
        self.assertIsNone(resolve_period("days", None, TODAY))
        self.assertIsNone(resolve_period("days", 0, TODAY))

    def test_today_and_yesterday_stay_single_day_for_revenue(self):
        self.assertIsNone(resolve_period("today", None, TODAY))
        self.assertIsNone(resolve_period("yesterday", None, TODAY))

    def test_profit_questions_get_a_text_range_even_for_one_day(self):
        self.assertEqual(resolve_period("today", None, TODAY, force_range=True)[:2], (TODAY, TODAY))
        yesterday = TODAY - datetime.timedelta(days=1)
        self.assertEqual(resolve_period("yesterday", None, TODAY, force_range=True)[:2], (yesterday, yesterday))

    def test_absurdly_long_windows_are_capped(self):
        start, _, _ = resolve_period("days", 100000, TODAY)
        self.assertEqual((TODAY - start).days, 365)
        start, _, _ = resolve_period("weeks", 100000, TODAY)
        self.assertLessEqual((TODAY - start).days, 365)


class _Base(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Period Shop", slug="period-shop", currency="USD")
        self.bread = Product.objects.create(company=self.company, name="Bread")
        self.eggs = Product.objects.create(company=self.company, name="Eggs")
        self.coke = Product.objects.create(company=self.company, name="Coke")

    def _sale(self, lines, days_ago=0, status=Sale.Status.CONFIRMED):
        """lines = (product, qty, price, cost|None, currency)."""
        ts = timezone.now() - datetime.timedelta(days=days_ago)
        sale = Sale.objects.create(company=self.company, sale_timestamp=ts, status=status)
        for product, qty, price, cost, currency in lines:
            SaleItem.objects.create(
                sale=sale, product=product, quantity=qty, unit_price=Decimal(price), currency=currency,
                unit_cost=None if cost is None else Decimal(cost),
                cost_currency=None if cost is None else currency,
            )

    def _summary(self, days=7):
        today = timezone.localdate()
        return build_period_summary(self.company, today - datetime.timedelta(days=days - 1), today)


class PeriodProfitTest(_Base):
    def test_full_cost_data(self):
        self._sale([(self.bread, 2, "1.00", "0.80", "USD")], days_ago=0)
        self._sale([(self.eggs, 1, "0.50", "0.20", "USD")], days_ago=1)

        data = self._summary(days=2)

        self.assertEqual(data["currency_profits"], {"USD": Decimal("0.70")})
        self.assertEqual(data["uncosted_lines"], 0)

    def test_partial_cost_data(self):
        self._sale([(self.bread, 2, "1.00", "0.80", "USD"), (self.eggs, 3, "0.50", None, "USD")])

        data = self._summary()

        self.assertEqual(data["currency_profits"], {"USD": Decimal("0.40")})
        self.assertEqual(data["uncosted_lines"], 1)

    def test_no_cost_data(self):
        self._sale([(self.bread, 2, "1.00", None, "USD")])

        data = self._summary()

        self.assertEqual(data["currency_profits"], {})
        self.assertEqual(data["uncosted_lines"], 1)

    def test_each_currency_separately(self):
        self._sale([(self.bread, 1, "1.00", "0.50", "USD"), (self.bread, 1, "20.00", "8.00", "ZAR")])

        data = self._summary()

        self.assertEqual(data["currency_profits"], {"USD": Decimal("0.50"), "ZAR": Decimal("12.00")})

    def test_cost_change_over_the_period_keeps_each_sales_own_cost(self):
        self._sale([(self.bread, 1, "1.50", "0.80", "USD")], days_ago=2)
        self._sale([(self.bread, 1, "1.50", "1.00", "USD")], days_ago=0)

        self.assertEqual(self._summary(days=3)["currency_profits"], {"USD": Decimal("1.20")})

    def test_cancelled_and_pending_sales_do_not_count(self):
        self._sale([(self.bread, 5, "1.00", "0.80", "USD")], status=Sale.Status.CANCELLED)
        self._sale([(self.bread, 5, "1.00", "0.80", "USD")], status=Sale.Status.PENDING)

        self.assertEqual(self._summary()["currency_profits"], {})

    def test_sales_outside_the_window_do_not_count(self):
        self._sale([(self.bread, 1, "1.00", "0.80", "USD")], days_ago=10)

        self.assertEqual(self._summary(days=2)["currency_profits"], {})


class MostProfitableTest(_Base):
    def test_ranks_by_profit_not_by_units_sold(self):
        # Coke sells most units but earns least; bread earns most.
        self._sale([
            (self.coke, 10, "1.00", "0.90", "USD"),   # profit 1.00
            (self.bread, 2, "2.00", "1.00", "USD"),   # profit 2.00
            (self.eggs, 3, "0.50", "0.10", "USD"),    # profit 1.20
        ])

        data = self._summary()

        self.assertEqual(
            data["profit_products"]["USD"],
            [("Bread", Decimal("2.00")), ("Eggs", Decimal("1.20")), ("Coke", Decimal("1.00"))],
        )
        self.assertEqual(data["top_products"][0][0], "Coke")  # best seller is a different answer

    def test_products_without_a_cost_are_not_ranked(self):
        self._sale([(self.bread, 2, "2.00", "1.00", "USD"), (self.eggs, 3, "0.50", None, "USD"),
                    (self.coke, 1, "1.00", "0.50", "USD")])

        names = [n for n, _ in self._summary()["profit_products"]["USD"]]

        self.assertEqual(names, ["Bread", "Coke"])

    def test_one_product_is_not_a_ranking(self):
        self._sale([(self.bread, 2, "2.00", "1.00", "USD")])

        self.assertEqual(self._summary()["profit_products"], {})

    def test_ranked_within_each_currency_never_across(self):
        self._sale([(self.bread, 1, "1.00", "0.50", "USD"), (self.eggs, 1, "2.00", "1.00", "USD"),
                    (self.coke, 1, "500.00", "100.00", "ZWG")])

        ranking = self._summary()["profit_products"]

        self.assertIn("USD", ranking)
        self.assertNotIn("ZWG", ranking)  # only one ZWG product

    def test_only_the_top_three_are_kept(self):
        extra = [Product.objects.create(company=self.company, name=f"P{i}") for i in range(3)]
        self._sale([(p, 1, "2.00", "1.00", "USD") for p in [self.bread, self.eggs, self.coke, *extra]])

        self.assertEqual(len(self._summary()["profit_products"]["USD"]), 3)

    def test_daily_summary_also_shows_the_ranking(self):
        self._sale([(self.bread, 2, "2.00", "1.00", "USD"), (self.eggs, 1, "0.50", "0.10", "USD")])

        snapshot = build_business_snapshot(self.company, timezone.localdate())
        text = format_business_summary(snapshot, lang="en")

        self.assertIn("Most profitable:", text)
        self.assertLess(text.index("Bread"), text.rindex("Eggs"))


class FormatPeriodProfitTest(SimpleTestCase):
    def _data(self, **overrides):
        data = {
            "sales_count": 4, "revenue": Decimal("10.00"), "currency_revenues": {"USD": Decimal("10.00")},
            "currency": "USD", "top_products": [("Coke", 5)],
            "currency_profits": {"USD": Decimal("4.00")}, "uncosted_lines": 0, "profit_products": {},
        }
        data.update(overrides)
        return data

    def test_revenue_question_also_shows_profit(self):
        text = format_period_summary("This week", self._data(), lang="en")
        self.assertIn("This week: $10.00 from 4 sales", text)
        self.assertIn("Profit: $4.00", text)
        self.assertIn("Top sellers:", text)

    def test_profit_question_leads_with_sales_then_profit(self):
        """#418: sales first, then profit (it read backwards on staging)."""
        text = format_period_summary("Last 2 days", self._data(), lang="en", metric="profit")
        lines = text.split("\n")
        self.assertEqual(lines[0], "Last 2 days")
        self.assertEqual(lines[1], "Sales: $10.00 (4 sales)")
        self.assertEqual(lines[2], "Profit: $4.00")
        self.assertNotIn("Top sellers", text)

    def test_partial_data_says_some_sales_have_no_cost(self):
        text = format_period_summary("This week", self._data(uncosted_lines=2), lang="en", metric="profit")
        self.assertIn("2 sold without one", text)

    def test_no_cost_data_asks_for_costs_on_a_profit_question(self):
        text = format_period_summary("This week", self._data(currency_profits={}, uncosted_lines=4),
                                     lang="en", metric="profit")
        self.assertNotIn("Profit:", text)
        self.assertIn("see profit", text)
        self.assertIn("$10.00", text)  # revenue is still given

    def test_revenue_question_without_costs_has_no_profit_nag(self):
        text = format_period_summary("This week", self._data(currency_profits={}, uncosted_lines=4), lang="en")
        self.assertNotIn("profit", text.lower())

    def test_two_currencies_listed_side_by_side(self):
        text = format_period_summary(
            "This week", self._data(currency_profits={"USD": Decimal("1.00"), "ZAR": Decimal("12.00")}),
            lang="en", metric="profit")
        self.assertIn("$1.00", text)
        self.assertIn("R12.00", text)

    def test_profit_answer_layout(self):
        """#418: sales, profit, the ranking, then one closing line about costs."""
        ranking = {"USD": [("Sugar", Decimal("28.00")), ("Bread", Decimal("16.00"))]}
        data = self._data(profit_products=ranking, cost_example=("Bread", Decimal("16.00"), "USD"),
                          costed_products=2, total_products=2)
        lines = format_period_summary("Today (7 Oct)", data, lang="en", metric="profit").split("\n")
        self.assertEqual(lines, [
            "Today (7 Oct)",
            "Sales: $10.00 (4 sales)",
            "Profit: $4.00",
            "",
            "Most profitable:",
            "  • Sugar: $28.00",
            "  • Bread: $16.00",
            "",
            'Costs saved for 2 of 2 products. Price changed? Send "Bread costs $17.00".',
        ])

    def test_most_profitable_block(self):
        ranking = {"USD": [("Bread", Decimal("2.00")), ("Eggs", Decimal("1.20"))]}
        text = format_period_summary("This week", self._data(profit_products=ranking), lang="en", metric="profit")
        self.assertIn("Most profitable:", text)
        self.assertIn("• Bread: $2.00", text)

    def test_shona_profit_lines(self):
        text = format_period_summary("This week", self._data(), lang="sn", metric="profit")
        self.assertIn("Purofiti: $4.00", text)

    def test_shona_answer_has_no_english_wording_left(self):
        ranking = {"USD": [("Bread", Decimal("2.00")), ("Eggs", Decimal("1.20"))]}
        for metric in ("profit", "revenue"):
            text = format_period_summary("Nhasi (06 Oct)", self._data(profit_products=ranking), lang="sn", metric=metric)
            for english in ("Revenue", "Sales:", "from 4 sales", "Top sellers", "Most profitable", "Profit",
                            "Costs saved", "Price changed"):
                self.assertNotIn(english, text, (metric, english))

    def test_shona_no_sales(self):
        data = self._data(sales_count=0)
        self.assertEqual(format_period_summary("Nhasi (06 Oct)", data, lang="sn"), "Nhasi (06 Oct): Hapana zvatengeswa.")


class QueryRoutingTest(TransactionTestCase):
    """_handle_sales_query turns the parsed question into the right date range."""

    def setUp(self):
        self.company = Company.objects.create(name="Route Shop", slug="route-shop", currency="USD")
        bread = Product.objects.create(company=self.company, name="Bread")
        for days_ago, cost in ((0, "0.50"), (1, "0.50"), (5, "0.50")):
            sale = Sale.objects.create(
                company=self.company, status=Sale.Status.CONFIRMED,
                sale_timestamp=timezone.now() - datetime.timedelta(days=days_ago),
            )
            SaleItem.objects.create(sale=sale, product=bread, quantity=1, unit_price=Decimal("1.00"),
                                    currency="USD", unit_cost=Decimal(cost), cost_currency="USD")
        self.profile = SimpleNamespace(company=self.company, role="owner")

    def _ask(self, **fields):
        result = SimpleNamespace(**{"timeframe": None, "period_count": None, "metric": "revenue", **fields})
        send = AsyncMock()
        with patch.object(webhook_handler, "_send_response", new=send):
            run_async_test(webhook_handler._handle_sales_query("+263770000000", self.profile, result, lang="en"))
        return send

    def test_past_two_days_counts_today_and_yesterday_only(self):
        send = self._ask(timeframe="days", period_count=2)

        text = send.await_args.args[1]
        self.assertIn("Last 2 days", text)
        self.assertIn("$2.00 from 2 sales", text)

    def test_last_two_weeks_includes_the_sale_five_days_ago(self):
        send = self._ask(timeframe="weeks", period_count=2)

        text = send.await_args.args[1]
        self.assertIn("Last 2 weeks", text)
        self.assertIn("$3.00 from 3 sales", text)

    def test_profit_this_week_is_a_text_answer_with_profit(self):
        send = self._ask(timeframe="week", metric="profit")

        text = send.await_args.args[1]
        self.assertIn("Profit: $", text)

    def test_profit_today_is_text_not_a_card(self):
        send = self._ask(timeframe="today", metric="profit")

        text = send.await_args.args[1]
        self.assertTrue(text.startswith("Today ("))
        self.assertIn("Profit: $0.50", text)


class ParserQueryFieldsTest(TestCase):
    def _parse(self, response):
        from apps.whatsapp.services.message_parser import parse_message_unified
        from services.openrouter.client import ParseJsonResult

        fake = AsyncMock(return_value=ParseJsonResult(response=response, prompt_tokens=1, completion_tokens=1))
        with patch("apps.whatsapp.services.message_parser.OpenRouterClient") as client:
            client.return_value.parse_json_response = fake
            return run_async_test(parse_message_unified("how much profit last 2 weeks"))

    def test_profit_over_weeks(self):
        result = self._parse({"intent": "sales_query", "confidence": 1, "timeframe": "weeks",
                              "period_count": 2, "metric": "profit"})
        self.assertEqual((result.timeframe, result.period_count, result.metric), ("weeks", 2, "profit"))

    def test_metric_defaults_to_revenue_and_bad_counts_are_dropped(self):
        result = self._parse({"intent": "sales_query", "confidence": 1, "timeframe": "days",
                              "period_count": "lots", "metric": "something"})
        self.assertEqual((result.period_count, result.metric), (None, "revenue"))

    def test_negative_count_is_dropped(self):
        result = self._parse({"intent": "sales_query", "confidence": 1, "timeframe": "days", "period_count": -3})
        self.assertIsNone(result.period_count)


class UncostedProductsTest(_Base):
    """Name the products sold without a cost so the owner can recall and price them."""

    def test_period_data_lists_them_most_units_first(self):
        self._sale([(self.bread, 1, "1.00", None, "USD"), (self.eggs, 6, "0.50", None, "USD"),
                    (self.coke, 2, "1.00", "0.50", "USD")])

        data = self._summary()

        self.assertEqual(data["uncosted_products"], ["Eggs", "Bread"])  # coke has a cost

    def test_same_product_sold_twice_is_listed_once(self):
        self._sale([(self.bread, 1, "1.00", None, "USD")])
        self._sale([(self.bread, 2, "1.00", None, "USD")], days_ago=1)

        self.assertEqual(self._summary(days=2)["uncosted_products"], ["Bread"])

    def test_cancelled_sales_do_not_add_names(self):
        self._sale([(self.bread, 1, "1.00", None, "USD")], status=Sale.Status.CANCELLED)

        self.assertEqual(self._summary()["uncosted_products"], [])

    def test_a_cost_in_another_currency_is_not_called_missing(self):
        SaleItem.objects.create(
            sale=Sale.objects.create(company=self.company, status=Sale.Status.CONFIRMED),
            product=self.bread, quantity=1, unit_price=Decimal("1.00"), currency="USD",
            unit_cost=Decimal("4.00"), cost_currency="ZWG",
        )

        data = self._summary()

        self.assertEqual(data["uncosted_products"], [])
        self.assertEqual(data["uncosted_lines"], 1)  # still left out of profit, and counted

    def test_daily_snapshot_lists_them_too(self):
        self._sale([(self.bread, 1, "1.00", None, "USD")])

        snapshot = build_business_snapshot(self.company, timezone.localdate())

        self.assertEqual(snapshot.uncosted_products, ["Bread"])
        self.assertIn("No cost yet: Bread.", format_business_summary(snapshot, lang="en"))


class UncostedProductsTextTest(SimpleTestCase):
    def _data(self, **overrides):
        data = {
            "sales_count": 3, "revenue": Decimal("8.00"), "currency_revenues": {"USD": Decimal("8.00")},
            "currency": "USD", "top_products": [], "currency_profits": {}, "uncosted_lines": 3,
            "profit_products": {}, "uncosted_products": ["book", "pen", "tape"],
        }
        data.update(overrides)
        return data

    def test_no_cost_answer_names_the_products_and_shows_how_to_add_one(self):
        text = format_period_summary("Yesterday (05 Oct)", self._data(), lang="en", metric="profit")
        self.assertIn("e.g. *book costs 0.80*", text)  # the example uses a product they actually sold
        self.assertIn("No cost yet: book, pen, tape.", text)
        self.assertIn("Sales: $8.00 (3 sales)", text)

    def test_partial_answer_names_the_products_left_out(self):
        text = format_period_summary(
            "This week", self._data(currency_profits={"USD": Decimal("4.00")}, uncosted_products=["pen"]),
            lang="en", metric="profit")
        self.assertIn("Profit: $4.00", text)
        self.assertIn("No cost yet: pen.", text)

    def test_long_lists_are_cut_with_a_count(self):
        names = [f"item{i}" for i in range(8)]
        text = format_period_summary("This week", self._data(uncosted_products=names), lang="en", metric="profit")
        self.assertIn("item0, item1, item2, item3, item4 +3 more.", text)
        self.assertNotIn("item5", text)

    def test_without_names_it_falls_back_to_a_count_and_a_generic_example(self):
        text = format_period_summary("This week", self._data(uncosted_products=[]), lang="en", metric="profit")
        self.assertIn("e.g. *bread costs 0.80*", text)
        partial = format_period_summary(
            "This week", self._data(currency_profits={"USD": Decimal("1.00")}, uncosted_products=[]),
            lang="en", metric="profit")
        self.assertIn("3 sold without one", partial)

    def test_shona_renders(self):
        text = format_period_summary("This week", self._data(), lang="sn", metric="profit")
        self.assertIn("ndinotenga book pa$0.80", text)
        self.assertIn("book, pen, tape", text)


class CurrencyMismatchNoteTest(SimpleTestCase):
    """A cost in another currency must not be reported as 'no cost set'."""

    def _data(self, **overrides):
        data = {
            "sales_count": 2, "revenue": Decimal("2.00"), "currency_revenues": {"USD": Decimal("2.00")},
            "currency": "USD", "top_products": [], "currency_profits": {}, "uncosted_lines": 2,
            "profit_products": {}, "uncosted_products": [], "mismatched_currency_lines": 2,
        }
        data.update(overrides)
        return data

    def test_only_mismatched_lines_says_so_and_does_not_ask_for_a_cost(self):
        text = format_period_summary("This week", self._data(), lang="en", metric="profit")
        self.assertIn("different currency", text)
        self.assertNotIn("Add what you pay", text)

    def test_partial_names_the_missing_products_and_mentions_the_mismatch(self):
        text = format_period_summary(
            "This week",
            self._data(currency_profits={"USD": Decimal("1.00")}, uncosted_products=["eggs"], uncosted_lines=3),
            lang="en", metric="profit")
        self.assertIn("No cost yet: eggs.", text)
        self.assertIn("different currency", text)

    def test_no_priced_lines_means_no_profit_text_at_all(self):
        text = format_period_summary(
            "This week",
            self._data(uncosted_lines=0, mismatched_currency_lines=0, uncosted_products=[]),
            lang="en", metric="profit")
        self.assertNotIn("Add what you pay", text)
        self.assertNotIn("different currency", text)



class CurrencyMismatchSnapshotTest(TestCase):
    def test_snapshot_end_to_end(self):
        company = Company.objects.create(name="Mix Shop", slug="mix-shop", currency="USD")
        bread = Product.objects.create(company=company, name="Bread")
        sale = Sale.objects.create(company=company, status=Sale.Status.CONFIRMED)
        SaleItem.objects.create(sale=sale, product=bread, quantity=1, unit_price=Decimal("500.00"), currency="ZWG",
                                unit_cost=Decimal("1.00"), cost_currency="USD")

        snapshot = build_business_snapshot(company, timezone.localdate())
        text = format_business_summary(snapshot, lang="en")

        self.assertEqual(snapshot.mismatched_currency_lines, 1)
        self.assertIn("different currency", text)
        self.assertNotIn("Add what you pay", text)
