"""Profit from frozen sale-line costs (#409): full, partial and no cost data,
cost changes over time, several currencies, and the daily summary text."""

import datetime
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.models import Company
from apps.sales.models import Sale, SaleItem
from apps.sales.services import summarize_profit
from apps.whatsapp.services.business_reports import (
    BusinessSnapshot,
    build_business_snapshot,
    format_business_summary,
)


def _line(price, cost, qty=1, currency="USD", cost_currency="USD"):
    """An unsaved SaleItem — summarize_profit only reads attributes."""
    return SaleItem(
        quantity=qty,
        unit_price=None if price is None else Decimal(price),
        currency=currency,
        unit_cost=None if cost is None else Decimal(cost),
        cost_currency=cost_currency,
    )


class SummarizeProfitTest(SimpleTestCase):
    def test_full_cost_data(self):
        result = summarize_profit([_line("1.00", "0.80", qty=2), _line("3.00", "2.00")])
        self.assertEqual(result.by_currency, {"USD": Decimal("1.40")})
        self.assertEqual(result.uncosted_lines, 0)

    def test_partial_cost_data_counts_only_known_lines(self):
        result = summarize_profit([_line("1.00", "0.80", qty=2), _line("5.00", None)])
        self.assertEqual(result.by_currency, {"USD": Decimal("0.40")})
        self.assertEqual(result.unknown_cost_lines, 1)

    def test_no_cost_data_gives_no_profit_not_zero(self):
        result = summarize_profit([_line("1.00", None), _line("2.00", None, cost_currency=None)])
        self.assertEqual(result.by_currency, {})
        self.assertEqual(result.unknown_cost_lines, 2)

    def test_no_lines(self):
        result = summarize_profit([])
        self.assertEqual(result.by_currency, {})
        self.assertEqual(result.uncosted_lines, 0)

    def test_free_product_is_known_cost_of_zero(self):
        result = summarize_profit([_line("2.00", "0.00", qty=3)])
        self.assertEqual(result.by_currency, {"USD": Decimal("6.00")})
        self.assertEqual(result.uncosted_lines, 0)

    def test_cost_change_between_sales_keeps_each_lines_own_cost(self):
        # Same product, cost rose from 0.80 to 1.00 between the two sales.
        result = summarize_profit([_line("1.50", "0.80"), _line("1.50", "1.00")])
        self.assertEqual(result.by_currency, {"USD": Decimal("1.20")})

    def test_selling_below_cost_is_a_negative_profit(self):
        result = summarize_profit([_line("0.50", "0.80", qty=2)])
        self.assertEqual(result.by_currency, {"USD": Decimal("-0.60")})

    def test_each_currency_is_reported_separately(self):
        result = summarize_profit([
            _line("1.00", "0.80", currency="USD", cost_currency="USD"),
            _line("50.00", "30.00", qty=2, currency="ZWG", cost_currency="ZWG"),
        ])
        self.assertEqual(result.by_currency, {"USD": Decimal("0.20"), "ZWG": Decimal("40.00")})

    def test_same_product_sold_in_two_currencies_gets_a_profit_for_each(self):
        result = summarize_profit([
            _line("1.00", "0.50", currency="USD", cost_currency="USD"),
            _line("20.00", "8.00", currency="ZAR", cost_currency="ZAR"),
            _line("1.00", "0.50", currency="USD", cost_currency="USD"),
        ])
        self.assertEqual(result.by_currency, {"USD": Decimal("1.00"), "ZAR": Decimal("12.00")})

    def test_cost_in_another_currency_is_left_out_not_converted(self):
        result = summarize_profit([_line("1.00", "4.00", currency="USD", cost_currency="ZWG")])
        self.assertEqual(result.by_currency, {})
        self.assertEqual(result.mismatched_currency_lines, 1)
        self.assertEqual(result.unknown_cost_lines, 0)

    def test_cost_without_a_currency_is_left_out(self):
        result = summarize_profit([_line("1.00", "0.80", cost_currency=None)])
        self.assertEqual(result.by_currency, {})
        self.assertEqual(result.mismatched_currency_lines, 1)

    def test_unpriced_lines_are_ignored_entirely(self):
        result = summarize_profit([_line(None, "0.80")])
        self.assertEqual(result.by_currency, {})
        self.assertEqual(result.uncosted_lines, 0)


class SnapshotProfitTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Profit Shop", slug="profit-shop", active=True)
        self.bread = Product.objects.create(name="Bread", company=self.company)
        self.eggs = Product.objects.create(name="Eggs", company=self.company)

    def _sale(self, status, *lines):
        sale = Sale.objects.create(company=self.company, status=status)
        for product, price, cost, qty in lines:
            SaleItem.objects.create(
                sale=sale, product=product, quantity=qty, unit_price=Decimal(price), currency="USD",
                unit_cost=None if cost is None else Decimal(cost),
                cost_currency=None if cost is None else "USD",
            )
        return sale

    def test_snapshot_profit_uses_only_confirmed_sales(self):
        self._sale(Sale.Status.CONFIRMED, (self.bread, "1.00", "0.80", 2))
        self._sale(Sale.Status.CANCELLED, (self.bread, "1.00", "0.80", 10))
        self._sale(Sale.Status.PENDING, (self.bread, "1.00", "0.80", 10))

        snapshot = build_business_snapshot(self.company, timezone.localdate())

        self.assertEqual(snapshot.currency_profits, {"USD": Decimal("0.40")})
        self.assertEqual(snapshot.uncosted_lines, 0)

    def test_snapshot_flags_lines_without_cost(self):
        self._sale(Sale.Status.CONFIRMED, (self.bread, "1.00", "0.80", 1), (self.eggs, "0.50", None, 4))

        snapshot = build_business_snapshot(self.company, timezone.localdate())

        self.assertEqual(snapshot.currency_profits, {"USD": Decimal("0.20")})
        self.assertEqual(snapshot.uncosted_lines, 1)
        self.assertEqual(snapshot.revenue, Decimal("3.00"))  # revenue still counts every priced line

    def test_snapshot_with_no_costs_has_no_profit(self):
        self._sale(Sale.Status.CONFIRMED, (self.eggs, "0.50", None, 4))

        snapshot = build_business_snapshot(self.company, timezone.localdate())

        self.assertEqual(snapshot.currency_profits, {})
        self.assertEqual(snapshot.uncosted_lines, 1)


def _snapshot(profits, uncosted=0, sales_count=3):
    return BusinessSnapshot(
        company_id=1,
        report_date=datetime.date(2026, 10, 6),
        currency="USD",
        sales_count=sales_count,
        items_sold=5,
        revenue=Decimal("10.00"),
        currency_revenues={"USD": Decimal("10.00")},
        top_products=[],
        low_stock_items=[],
        out_of_stock_items=[],
        currency_profits=profits,
        uncosted_lines=uncosted,
    )


class SummaryProfitTextTest(SimpleTestCase):
    def test_full_data_shows_profit_and_no_warning(self):
        text = format_business_summary(_snapshot({"USD": Decimal("4.00")}), lang="en")
        self.assertIn("Profit: $4.00", text)
        self.assertNotIn("without one", text)

    def test_partial_data_says_how_many_lines_have_no_cost(self):
        text = format_business_summary(_snapshot({"USD": Decimal("4.00")}, uncosted=2), lang="en")
        self.assertIn("Profit: $4.00", text)
        self.assertIn("2 sold without one", text)

    def test_no_cost_data_asks_for_costs_instead_of_showing_zero(self):
        text = format_business_summary(_snapshot({}, uncosted=3), lang="en")
        self.assertNotIn("Profit:", text)
        self.assertIn("see profit", text)

    def test_quiet_day_has_no_profit_text(self):
        text = format_business_summary(_snapshot({}, sales_count=0), lang="en")
        self.assertNotIn("rofit", text)

    def test_two_currencies_are_listed_side_by_side(self):
        text = format_business_summary(_snapshot({"USD": Decimal("1.00"), "ZAR": Decimal("12.00")}), lang="en")
        self.assertIn("$1.00", text)
        self.assertIn("R12.00", text)

    def test_loss_has_the_minus_in_front_of_the_symbol(self):
        text = format_business_summary(_snapshot({"USD": Decimal("-0.60")}), lang="en")
        self.assertIn("Profit: -$0.60", text)

    def test_shona_summary_renders_profit_strings(self):
        text = format_business_summary(_snapshot({"USD": Decimal("4.00")}, uncosted=1), lang="sn")
        self.assertIn("Purofiti: $4.00", text)
        self.assertIn("1", text)
