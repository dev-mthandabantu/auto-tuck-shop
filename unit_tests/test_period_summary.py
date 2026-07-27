"""Unit tests for build_period_summary and format_period_summary.

Tests use a real test database — no API key required.

Run with:
    python manage.py test unit_tests.test_period_summary
"""

import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.models import Company
from apps.sales.models import Sale, SaleItem
from apps.whatsapp.services.business_reports import build_period_summary, format_period_summary


def _make_company():
    return Company.objects.create(name="Test Shop", slug="test-shop-ps", currency="USD")


def _make_product(company, name):
    return Product.objects.create(company=company, name=name)


def _make_sale(company, items, days_ago=0):
    """Create a confirmed Sale with SaleItems. items = list of (product, qty, unit_price, currency)."""
    ts = timezone.now() - datetime.timedelta(days=days_ago)
    sale = Sale.objects.create(company=company, sale_timestamp=ts, status=Sale.Status.CONFIRMED)
    for product, qty, unit_price, currency in items:
        SaleItem.objects.create(sale=sale, product=product, quantity=qty, unit_price=unit_price, currency=currency)
    return sale


class BuildPeriodSummaryTest(TestCase):

    def setUp(self):
        self.company = _make_company()
        self.coke = _make_product(self.company, "Coke")
        self.bread = _make_product(self.company, "Bread")

    def _summary(self, days=7):
        today = timezone.localdate()
        start = today - datetime.timedelta(days=days - 1)
        return build_period_summary(self.company, start, today)

    def test_single_currency_revenue(self):
        _make_sale(self.company, [(self.coke, 3, Decimal("1.00"), "USD")])
        _make_sale(self.company, [(self.bread, 2, Decimal("0.50"), "USD")])
        data = self._summary()
        self.assertEqual(data["sales_count"], 2)
        self.assertEqual(data["revenue"], Decimal("4.00"))
        self.assertEqual(data["currency_revenues"], {"USD": Decimal("4.00")})

    def test_multi_currency_revenue(self):
        _make_sale(self.company, [(self.coke, 2, Decimal("1.00"), "USD")])
        _make_sale(self.company, [(self.bread, 1, Decimal("10.00"), "ZAR")])
        data = self._summary()
        self.assertEqual(data["sales_count"], 2)
        self.assertIn("USD", data["currency_revenues"])
        self.assertIn("ZAR", data["currency_revenues"])
        self.assertEqual(data["currency_revenues"]["USD"], Decimal("2.00"))
        self.assertEqual(data["currency_revenues"]["ZAR"], Decimal("10.00"))

    def test_total_amount_none_does_not_crash(self):
        """Mixed-currency sale sets total_amount=None — must still aggregate correctly from SaleItems."""
        sale = Sale.objects.create(
            company=self.company,
            status=Sale.Status.CONFIRMED,
            total_amount=None,
        )
        SaleItem.objects.create(sale=sale, product=self.coke, quantity=1, unit_price=Decimal("2.00"), currency="USD")
        SaleItem.objects.create(sale=sale, product=self.bread, quantity=1, unit_price=Decimal("5.00"), currency="ZAR")
        data = self._summary()
        self.assertEqual(data["revenue"], Decimal("7.00"))

    def test_no_sales(self):
        data = self._summary()
        self.assertEqual(data["sales_count"], 0)
        self.assertEqual(data["revenue"], Decimal("0.00"))
        self.assertEqual(data["currency_revenues"], {})

    def test_top_products_sorted_by_quantity(self):
        _make_sale(self.company, [
            (self.coke, 5, Decimal("1.00"), "USD"),
            (self.bread, 2, Decimal("0.50"), "USD"),
        ])
        data = self._summary()
        self.assertEqual(data["top_products"][0][0], "Coke")
        self.assertEqual(data["top_products"][1][0], "Bread")

    def test_excludes_sales_outside_date_range(self):
        _make_sale(self.company, [(self.coke, 1, Decimal("1.00"), "USD")], days_ago=10)
        data = self._summary(days=7)
        self.assertEqual(data["sales_count"], 0)


class FormatPeriodSummaryTest(TestCase):

    def test_no_sales(self):
        data = {"sales_count": 0, "revenue": Decimal("0.00"), "currency_revenues": {}, "currency": "USD", "top_products": []}
        self.assertIn("No sales recorded", format_period_summary("This week", data))

    def test_single_currency(self):
        data = {
            "sales_count": 3,
            "revenue": Decimal("6.00"),
            "currency_revenues": {"USD": Decimal("6.00")},
            "currency": "USD",
            "top_products": [("Coke", 3)],
        }
        result = format_period_summary("This week", data)
        self.assertIn("$6.00", result)
        self.assertIn("3 sales", result)
        self.assertIn("Coke", result)

    def test_single_currency_not_company_default(self):
        """Regression test for #246 — the period's actual currency (ZAR) must
        be shown even though the company's configured default is USD."""
        data = {
            "sales_count": 1,
            "revenue": Decimal("10.00"),
            "currency_revenues": {"ZAR": Decimal("10.00")},
            "currency": "USD",
            "top_products": [],
        }
        result = format_period_summary("This week", data)
        self.assertIn("R10.00", result)
        self.assertNotIn("$10.00", result)

    def test_multi_currency_shows_both(self):
        data = {
            "sales_count": 2,
            "revenue": Decimal("12.00"),
            "currency_revenues": {"USD": Decimal("2.00"), "ZAR": Decimal("10.00")},
            "currency": "USD",
            "top_products": [],
        }
        result = format_period_summary("This week", data)
        self.assertIn("$2.00", result)
        self.assertIn("R10.00", result)
        self.assertIn("+", result)

    def test_singular_sale_word(self):
        data = {
            "sales_count": 1,
            "revenue": Decimal("1.00"),
            "currency_revenues": {"USD": Decimal("1.00")},
            "currency": "USD",
            "top_products": [],
        }
        result = format_period_summary("This week", data)
        self.assertIn("1 sale", result)
        self.assertNotIn("1 sales", result)
