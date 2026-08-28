"""Unit tests for weekly report card generation and comparison context.

These tests do not require an API key or database — they test pure logic
and image generation in isolation.

Run with:
    python manage.py test unit_tests.test_daily_reports
"""

import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.whatsapp.services.business_reports import BusinessSnapshot, build_comparison_context, format_business_summary
from apps.whatsapp.services.report_card import generate_stat_card


def _make_snapshot(revenue=Decimal("100.00"), sales_count=5, report_date=None, currency_revenues=None):
    return BusinessSnapshot(
        company_id=1,
        report_date=report_date or datetime.date(2026, 5, 25),
        currency="USD",
        sales_count=sales_count,
        items_sold=10,
        revenue=revenue,
        currency_revenues=currency_revenues or {"USD": revenue},
        cost=Decimal("50.00"),
        gross_profit=revenue - Decimal("50.00"),
        top_products=[("Coca Cola", 5), ("Bread", 3)],
        low_stock_items=[],
        out_of_stock_items=[],
    )


class FormatBusinessSummaryTest(TestCase):
    """Regression tests for #246 — revenue must never be labeled with
    company.currency when it wasn't the currency actually present in the
    day's sales."""

    def test_single_currency_not_company_default(self):
        snapshot = _make_snapshot(
            revenue=Decimal("204.00"),
            sales_count=2,
            currency_revenues={"ZAR": Decimal("204.00")},
        )
        result = format_business_summary(snapshot)
        self.assertIn("R204.00", result)
        self.assertNotIn("$204.00", result)
        self.assertNotIn("$204", result)

    def test_multi_currency_unaffected(self):
        snapshot = _make_snapshot(
            revenue=Decimal("60.00"),
            sales_count=2,
            currency_revenues={"USD": Decimal("10.00"), "ZAR": Decimal("50.00")},
        )
        result = format_business_summary(snapshot)
        self.assertIn("$10.00", result)
        self.assertIn("R50.00", result)
        self.assertIn("+", result)

    def test_zero_sales_unaffected(self):
        snapshot = _make_snapshot(
            revenue=Decimal("0.00"),
            sales_count=0,
            currency_revenues={},
        )
        result = format_business_summary(snapshot, lang="en")
        self.assertIn("Quiet day", result)


class ReportCardGenerationTest(TestCase):

    def test_returns_png_bytes(self):
        snapshot = _make_snapshot()
        comparison = {
            "is_best_day_this_week": True,
            "best_day_label": "Today",
            "week_revenues": [{"USD": Decimal("80.00")}, {"USD": Decimal("100.00")}],
            "week_sales_count": 10,
            "week_currency_revenues": {"USD": Decimal("180.00")},
        }
        result = generate_stat_card(snapshot, comparison, shop_name="Test Shop")
        self.assertIsInstance(result, bytes)
        self.assertTrue(result.startswith(b"\x89PNG"), "Result should be a valid PNG")
        self.assertGreater(len(result), 1000, "PNG should be a reasonable size")

    def test_no_top_products(self):
        snapshot = BusinessSnapshot(
            company_id=1,
            report_date=datetime.date(2026, 5, 25),
            currency="USD",
            sales_count=2,
            items_sold=2,
            revenue=Decimal("10.00"),
            currency_revenues={"USD": Decimal("10.00")},
            cost=Decimal("5.00"),
            gross_profit=Decimal("5.00"),
            top_products=[],
            low_stock_items=[],
            out_of_stock_items=[],
        )
        comparison = {
            "is_best_day_this_week": False,
            "week_revenues": [{"USD": Decimal("10.00")}],
            "week_sales_count": 2,
            "week_currency_revenues": {"USD": Decimal("10.00")},
        }
        result = generate_stat_card(snapshot, comparison)
        self.assertTrue(result.startswith(b"\x89PNG"))

    def test_not_best_day(self):
        snapshot = _make_snapshot(revenue=Decimal("60.00"))
        comparison = {
            "is_best_day_this_week": False,
            "week_revenues": [{"USD": Decimal("80.00")}, {"USD": Decimal("60.00")}],
            "week_sales_count": 8,
            "week_currency_revenues": {"USD": Decimal("140.00")},
        }
        result = generate_stat_card(snapshot, comparison)
        self.assertTrue(result.startswith(b"\x89PNG"))

    def test_multi_currency_grouped_bars(self):
        snapshot = _make_snapshot(
            revenue=Decimal("142.00"),
            currency_revenues={"USD": Decimal("142.00"), "ZAR": Decimal("480.00")},
        )
        comparison = {
            "is_best_day_this_week": True,
            "best_day_label": "Today",
            "week_revenues": [
                {"USD": Decimal("80.00")},
                {"USD": Decimal("142.00"), "ZAR": Decimal("480.00")},
            ],
            "week_sales_count": 12,
            "week_currency_revenues": {"USD": Decimal("222.00"), "ZAR": Decimal("480.00")},
        }
        result = generate_stat_card(snapshot, comparison, shop_name="Test Shop")
        self.assertTrue(result.startswith(b"\x89PNG"))


class ComparisonContextTest(TestCase):

    def _make_company(self, name="Test Shop"):
        company = MagicMock()
        company.id = 1
        company.name = name
        company.currency = "USD"
        return company

    @patch("apps.whatsapp.services.business_reports.build_business_snapshot")
    def test_returns_new_shape(self, mock_build):
        today = datetime.date(2026, 5, 25)  # Monday — 1 call
        mock_build.side_effect = [
            _make_snapshot(revenue=Decimal("80.00"), report_date=today),
        ]
        company = self._make_company()
        result = build_comparison_context(company, report_date=today)
        self.assertIn("week_revenues", result)
        self.assertIn("week_sales_count", result)
        self.assertIn("week_currency_revenues", result)
        self.assertIn("is_best_day_this_week", result)
        self.assertIn("best_day_label", result)
        self.assertNotIn("delta", result)
        self.assertNotIn("yesterday_revenue", result)
        self.assertIsInstance(result["week_revenues"][0], dict)

    @patch("apps.whatsapp.services.business_reports.build_business_snapshot")
    def test_is_best_day_this_week_true_when_highest(self, mock_build):
        today = datetime.date(2026, 5, 27)  # Wednesday — 3 calls
        monday = datetime.date(2026, 5, 25)
        tuesday = datetime.date(2026, 5, 26)

        mock_build.side_effect = [
            _make_snapshot(revenue=Decimal("40.00"), sales_count=3, report_date=monday),
            _make_snapshot(revenue=Decimal("50.00"), sales_count=4, report_date=tuesday),
            _make_snapshot(revenue=Decimal("100.00"), sales_count=8, report_date=today),
        ]
        company = self._make_company()
        result = build_comparison_context(company, report_date=today)
        self.assertTrue(result["is_best_day_this_week"])
        self.assertEqual(result["best_day_label"], "Today")
        self.assertEqual(result["week_sales_count"], 15)
        self.assertEqual(result["week_currency_revenues"]["USD"], Decimal("190.00"))
        self.assertEqual(len(result["week_revenues"]), 3)

    @patch("apps.whatsapp.services.business_reports.build_business_snapshot")
    def test_is_best_day_false_when_not_highest(self, mock_build):
        today = datetime.date(2026, 5, 27)  # Wednesday — 3 calls
        monday = datetime.date(2026, 5, 25)
        tuesday = datetime.date(2026, 5, 26)

        mock_build.side_effect = [
            _make_snapshot(revenue=Decimal("80.00"), sales_count=6, report_date=monday),
            _make_snapshot(revenue=Decimal("120.00"), sales_count=9, report_date=tuesday),
            _make_snapshot(revenue=Decimal("60.00"), sales_count=5, report_date=today),
        ]
        company = self._make_company()
        result = build_comparison_context(company, report_date=today)
        self.assertFalse(result["is_best_day_this_week"])
        self.assertEqual(result["week_sales_count"], 20)

    @patch("apps.whatsapp.services.business_reports.build_business_snapshot")
    def test_multi_currency_week_revenues(self, mock_build):
        today = datetime.date(2026, 5, 26)  # Tuesday — 2 calls
        monday = datetime.date(2026, 5, 25)

        mock_build.side_effect = [
            _make_snapshot(revenue=Decimal("80.00"), report_date=monday,
                           currency_revenues={"USD": Decimal("80.00")}),
            _make_snapshot(revenue=Decimal("142.00"), report_date=today,
                           currency_revenues={"USD": Decimal("142.00"), "ZAR": Decimal("480.00")}),
        ]
        company = self._make_company()
        result = build_comparison_context(company, report_date=today)
        self.assertEqual(result["week_revenues"][0], {"USD": Decimal("80.00")})
        self.assertEqual(result["week_revenues"][1], {"USD": Decimal("142.00"), "ZAR": Decimal("480.00")})
        self.assertEqual(result["week_currency_revenues"]["USD"], Decimal("222.00"))
        self.assertEqual(result["week_currency_revenues"]["ZAR"], Decimal("480.00"))
