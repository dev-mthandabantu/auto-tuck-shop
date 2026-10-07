"""Owner-only profit lines from Brighton's review of #409: which cost the profit used (costs go
stale and the bot only asks once), and how many of the shop's products have a cost saved."""

import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.models import Company
from apps.sales.models import Sale, SaleItem
from apps.sales.services import summarize_profit
from apps.whatsapp.services import business_reports
from apps.whatsapp.services.business_reports import (
    build_business_snapshot,
    build_period_summary,
    format_business_summary,
    format_period_summary,
)
from unit_tests.async_helpers import run_async_test
from unit_tests.test_owner_only_profit import ASSISTANT_PHONE, OWNER_PHONE, _Shop


class _Shop2(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Vis Shop", slug="vis-shop", currency="USD")
        self.bread = Product.objects.create(company=self.company, name="Bread")
        self.sugar = Product.objects.create(company=self.company, name="Sugar")
        self.eggs = Product.objects.create(company=self.company, name="Eggs")

    def _sale(self, lines, days_ago=0):
        sale = Sale.objects.create(company=self.company, status=Sale.Status.CONFIRMED,
                                   sale_timestamp=timezone.now() - datetime.timedelta(days=days_ago, minutes=days_ago))
        for product, qty, price, cost, currency in lines:
            SaleItem.objects.create(
                sale=sale, product=product, quantity=qty, unit_price=Decimal(price), currency=currency,
                unit_cost=None if cost is None else Decimal(cost), cost_currency=None if cost is None else currency)

    def _period(self, days=3):
        today = timezone.localdate()
        return build_period_summary(self.company, today - datetime.timedelta(days=days - 1), today)


class CostExampleTest(_Shop2):
    def test_it_names_the_best_selling_costed_product(self):
        self._sale([(self.bread, 2, "1.00", "0.80", "USD"), (self.sugar, 9, "2.00", "1.50", "USD")])

        data = self._period()

        self.assertEqual(data["cost_example"], ("Sugar", Decimal("1.50"), "USD"))

    def test_it_uses_the_most_recent_cost_when_the_cost_changed(self):
        self._sale([(self.bread, 1, "1.50", "0.80", "USD")], days_ago=2)
        self._sale([(self.bread, 1, "1.50", "1.00", "USD")], days_ago=0)

        self.assertEqual(self._period()["cost_example"], ("Bread", Decimal("1.00"), "USD"))

    def test_products_without_a_cost_are_never_the_example(self):
        self._sale([(self.bread, 50, "1.00", None, "USD"), (self.sugar, 1, "2.00", "1.50", "USD")])

        self.assertEqual(self._period()["cost_example"][0], "Sugar")

    def test_no_costs_means_no_example(self):
        self._sale([(self.bread, 5, "1.00", None, "USD")])

        self.assertIsNone(self._period()["cost_example"])

    def test_a_tie_goes_to_the_name_that_sorts_first(self):
        self._sale([(self.sugar, 3, "2.00", "1.50", "USD"), (self.bread, 3, "1.00", "0.80", "USD")])

        self.assertEqual(self._period()["cost_example"][0], "Bread")

    def test_a_cost_in_another_currency_is_not_used(self):
        self._sale([(self.bread, 4, "1.00", None, "USD")])
        SaleItem.objects.filter(product=self.bread).update(unit_cost=Decimal("4.00"), cost_currency="ZWG")

        self.assertIsNone(self._period()["cost_example"])


class CostsSavedTest(_Shop2):
    def test_counts_active_products_with_a_cost(self):
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("0.80"), cost_currency="USD")
        Product.objects.filter(id=self.sugar.id).update(cost=Decimal("0.00"), cost_currency="USD")  # free is still known
        self._sale([(self.bread, 1, "1.00", "0.80", "USD")])

        data = self._period()

        self.assertEqual((data["costed_products"], data["total_products"]), (2, 3))

    def test_inactive_products_are_not_counted(self):
        Product.objects.filter(id=self.eggs.id).update(active=False)
        self._sale([(self.bread, 1, "1.00", "0.80", "USD")])

        self.assertEqual(self._period()["total_products"], 2)

    def test_only_this_shops_products_are_counted(self):
        other = Company.objects.create(name="Other", slug="other-shop")
        Product.objects.create(company=other, name="Bread", cost=Decimal("1.00"), cost_currency="USD")
        self._sale([(self.bread, 1, "1.00", "0.80", "USD")])

        self.assertEqual(self._period()["total_products"], 3)

    def test_daily_snapshot_has_the_same_counts(self):
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("0.80"), cost_currency="USD")
        self._sale([(self.bread, 1, "1.00", "0.80", "USD")])

        snap = build_business_snapshot(self.company, timezone.localdate())

        self.assertEqual((snap.costed_products, snap.total_products), (1, 3))
        self.assertEqual(snap.cost_example, ("Bread", Decimal("0.80"), "USD"))


class TextTest(SimpleTestCase):
    def _data(self, **overrides):
        data = {
            "sales_count": 4, "revenue": Decimal("10.00"), "currency_revenues": {"USD": Decimal("10.00")},
            "currency": "USD", "top_products": [("Coke", 5)], "currency_profits": {"USD": Decimal("4.00")},
            "uncosted_lines": 0, "profit_products": {}, "uncosted_products": [],
            "cost_example": ("Sugar", Decimal("40.00"), "ZAR"), "costed_products": 8, "total_products": 12,
        }
        data.update(overrides)
        return data

    def test_profit_answer_shows_both_lines(self):
        text = format_period_summary("This week", self._data(), lang="en", metric="profit")
        self.assertIn("Costs saved: 8 of 12 products.", text)
        self.assertIn('Profit uses Sugar at R40.00. Changed? Send "Sugar costs R44.00".', text)

    def test_the_cost_used_line_appears_once_however_many_products_were_sold(self):
        text = format_period_summary("This week", self._data(), lang="en", metric="profit")
        self.assertEqual(text.count("Profit uses"), 1)

    def test_revenue_answer_also_shows_them_when_profit_is_shown(self):
        text = format_period_summary("This week", self._data(), lang="en", metric="revenue")
        self.assertIn("Costs saved: 8 of 12 products.", text)
        self.assertIn("Profit uses Sugar", text)

    def test_the_example_new_cost_is_a_tenth_higher(self):
        text = format_period_summary("This week", self._data(cost_example=("Bread", Decimal("0.80"), "USD")),
                                     lang="en", metric="profit")
        self.assertIn('Send "Bread costs $0.88"', text)

    def test_a_zero_cost_gets_a_sensible_example(self):
        text = format_period_summary("This week", self._data(cost_example=("Sample", Decimal("0.00"), "USD")),
                                     lang="en", metric="profit")
        self.assertIn('Send "Sample costs $1.00"', text)

    def test_with_no_profit_it_still_shows_where_they_stand_but_not_a_cost_used(self):
        text = format_period_summary("This week", self._data(
            currency_profits={}, uncosted_lines=4, uncosted_products=["Coke"], cost_example=None, costed_products=0),
            lang="en", metric="profit")
        self.assertIn("Costs saved: 0 of 12 products.", text)
        self.assertNotIn("Profit uses", text)

    def test_no_products_means_no_coverage_line(self):
        text = format_period_summary("This week", self._data(total_products=0, costed_products=0), lang="en", metric="profit")
        self.assertNotIn("Costs saved", text)

    def test_nothing_at_all_when_there_was_no_priced_sale(self):
        text = format_period_summary("This week", self._data(
            currency_profits={}, uncosted_lines=0, cost_example=None), lang="en", metric="profit")
        self.assertNotIn("Costs saved", text)

    def test_hidden_from_non_owners(self):
        for metric in ("profit", "revenue"):
            text = format_period_summary("This week", self._data(), lang="en", metric=metric, show_profit=False)
            self.assertNotIn("Costs saved", text, metric)
            self.assertNotIn("Profit uses", text, metric)

    def test_shona_renders(self):
        text = format_period_summary("Vhiki rino", self._data(), lang="sn", metric="profit")
        self.assertIn("Zvine cost: 8 pa12.", text)
        self.assertIn("Purofiti yashandisa cost yeSugar: R40.00.", text)


class DailySummaryTextTest(_Shop2):
    def test_the_daily_summary_shows_them_to_the_owner_view_only(self):
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("0.80"), cost_currency="USD")
        self._sale([(self.bread, 2, "1.00", "0.80", "USD")])
        snap = build_business_snapshot(self.company, timezone.localdate())

        owner = format_business_summary(snap, lang="en", show_profit=True)
        staff = format_business_summary(snap, lang="en", show_profit=False)

        self.assertIn("Costs saved: 1 of 3 products.", owner)
        self.assertIn('Profit uses Bread at $0.80. Changed? Send "Bread costs $0.88".', owner)
        self.assertNotIn("Costs saved", staff)
        self.assertNotIn("Profit uses", staff)


class AssistantNeverSeesThemTest(_Shop):
    def test_the_daily_message_to_an_assistant_has_neither_line(self):
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("1.00"), cost_currency="USD")
        self._sale((self.bread, 2, "2.00", "1.00"))
        client = MagicMock()
        client.send_message = AsyncMock(return_value=True)
        client.send_image = AsyncMock(return_value=True)

        async def go():
            with patch.object(business_reports, "get_whatsapp_client", return_value=client), \
                 patch.object(business_reports.settings, "USE_TEMPLATE_MESSAGES", False), \
                 patch("apps.whatsapp.services.report_card.generate_stat_card", side_effect=RuntimeError("no card")):
                return await business_reports.send_daily_summary(self.company)

        run_async_test(go())

        sent = {c.args[0]: c.args[1] for c in client.send_message.await_args_list}
        self.assertIn("Costs saved:", sent[OWNER_PHONE])
        self.assertIn("Profit uses Bread", sent[OWNER_PHONE])
        for leak in ("Costs saved", "Profit uses", "costs "):
            self.assertNotIn(leak, sent[ASSISTANT_PHONE], leak)
