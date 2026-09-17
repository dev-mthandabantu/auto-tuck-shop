"""Tests for the pilot dashboard bot-mistake rate (#378).

"Fix mistake" cancels the sale and flags it in the same write
(apps/whatsapp/services/sale_handler.py), so a flagged sale is never
CONFIRMED. The dashboard used to require both, making mistake_count
structurally always 0.

Run with:
    python manage.py test unit_tests.test_pilot_metrics_mistake_rate
"""

import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Company
from apps.sales.models import Sale


def _make_company(**kwargs):
    defaults = {"name": "Mistake Rate Shop", "slug": "mistake-rate-shop", "active": True}
    defaults.update(kwargs)
    return Company.objects.create(**defaults)


def _make_sale(company, status, days_ago=1, flagged=False):
    return Sale.objects.create(
        company=company,
        sale_timestamp=timezone.now() - datetime.timedelta(days=days_ago),
        status=status,
        flagged_as_bot_mistake=flagged,
    )


class PilotMetricsMistakeRateTests(TestCase):
    def setUp(self):
        staff = User.objects.create_user(username="staff", password="x", is_staff=True)
        self.client.force_login(staff)
        self.url = reverse("pilot_metrics")

    def _shop_row(self, company, days=30):
        response = self.client.get(self.url, {"days": days})
        self.assertEqual(response.status_code, 200)
        rows = {shop.id: shop for shop in response.context["sales_per_shop"]}
        return rows[company.id]

    def test_cancelled_flagged_sale_counts_toward_mistake_rate(self):
        """One confirmed + one cancelled-and-flagged sale renders 1/1 = 100%."""
        company = _make_company()
        _make_sale(company, Sale.Status.CONFIRMED)
        _make_sale(company, Sale.Status.CANCELLED, flagged=True)

        shop = self._shop_row(company)

        self.assertEqual(shop.confirmed_sales, 1)
        self.assertEqual(shop.mistake_count, 1)
        self.assertEqual(shop.mistake_pct, 100)

    def test_mistake_rate_matches_documented_ratio(self):
        """The issue's example: 45 confirmed, 4 flagged -> 8.9%, rendered as 9%."""
        company = _make_company(slug="mistake-rate-shop-ratio")
        for _ in range(45):
            _make_sale(company, Sale.Status.CONFIRMED)
        for _ in range(4):
            _make_sale(company, Sale.Status.CANCELLED, flagged=True)

        shop = self._shop_row(company)

        self.assertEqual(shop.confirmed_sales, 45)
        self.assertEqual(shop.mistake_count, 4)
        self.assertEqual(shop.mistake_pct, 9)

    def test_unflagged_sales_produce_zero_rate(self):
        """A plain cancellation (not "Fix mistake") must not count as a mistake."""
        company = _make_company(slug="mistake-rate-shop-clean")
        _make_sale(company, Sale.Status.CONFIRMED)
        _make_sale(company, Sale.Status.CANCELLED)

        shop = self._shop_row(company)

        self.assertEqual(shop.mistake_count, 0)
        self.assertEqual(shop.mistake_pct, 0)

    def test_flagged_but_pending_sale_is_not_counted(self):
        """A flagged sale that never reached CANCELLED is an incomplete write,
        not a reported bot mistake.

        Unreachable today: sale_handler._get_and_update_sale sets status and
        flag in one save() inside transaction.atomic(). This test exists to
        make that invariant fail loudly if #32 splits the flag from the cancel.
        """
        company = _make_company(slug="mistake-rate-shop-pending")
        _make_sale(company, Sale.Status.CONFIRMED)
        _make_sale(company, Sale.Status.PENDING, flagged=True)

        shop = self._shop_row(company)

        self.assertEqual(shop.confirmed_sales, 1)
        self.assertEqual(shop.mistake_count, 0)
        self.assertEqual(shop.mistake_pct, 0)

    def test_flagged_sale_outside_window_is_excluded(self):
        """The lookback window still applies to flagged sales."""
        company = _make_company(slug="mistake-rate-shop-window")
        _make_sale(company, Sale.Status.CONFIRMED, days_ago=1)
        _make_sale(company, Sale.Status.CANCELLED, days_ago=60, flagged=True)

        shop = self._shop_row(company, days=30)

        self.assertEqual(shop.confirmed_sales, 1)
        self.assertEqual(shop.mistake_count, 0)
        self.assertEqual(shop.mistake_pct, 0)
