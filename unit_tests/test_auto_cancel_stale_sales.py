"""Unit tests for apps/sales/services.py auto_cancel_stale_pending_sales.

Fast-follow to #298 (sales start PENDING and only affect price/stock once
confirmed) — PENDING sales the owner never confirms or rejects previously
sat around forever with no cleanup.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.core.models import Company
from apps.sales.models import Sale
from apps.sales.services import STALE_PENDING_THRESHOLD, auto_cancel_stale_pending_sales


def _make_company(name="Test Shop"):
    return Company.objects.create(name=name, slug=name.lower().replace(" ", "-"), active=True)


def _make_sale(company, status, age: timedelta):
    sale = Sale.objects.create(company=company, status=status)
    # sale_timestamp has auto_now_add-like default=timezone.now — backdate it directly.
    Sale.objects.filter(pk=sale.pk).update(sale_timestamp=timezone.now() - age)
    return sale


class AutoCancelStalePendingSalesTest(TestCase):

    def setUp(self):
        self.company = _make_company()

    def test_pending_sale_older_than_threshold_is_cancelled(self):
        sale = _make_sale(self.company, Sale.Status.PENDING, age=STALE_PENDING_THRESHOLD + timedelta(minutes=1))

        cancelled_count = auto_cancel_stale_pending_sales()

        sale.refresh_from_db()
        self.assertEqual(cancelled_count, 1)
        self.assertEqual(sale.status, Sale.Status.CANCELLED)

    def test_pending_sale_younger_than_threshold_is_untouched(self):
        sale = _make_sale(self.company, Sale.Status.PENDING, age=STALE_PENDING_THRESHOLD - timedelta(minutes=1))

        cancelled_count = auto_cancel_stale_pending_sales()

        sale.refresh_from_db()
        self.assertEqual(cancelled_count, 0)
        self.assertEqual(sale.status, Sale.Status.PENDING)

    def test_confirmed_sale_is_untouched_regardless_of_age(self):
        sale = _make_sale(self.company, Sale.Status.CONFIRMED, age=STALE_PENDING_THRESHOLD + timedelta(days=30))

        cancelled_count = auto_cancel_stale_pending_sales()

        sale.refresh_from_db()
        self.assertEqual(cancelled_count, 0)
        self.assertEqual(sale.status, Sale.Status.CONFIRMED)

    def test_already_cancelled_sale_is_untouched(self):
        sale = _make_sale(self.company, Sale.Status.CANCELLED, age=STALE_PENDING_THRESHOLD + timedelta(days=30))

        cancelled_count = auto_cancel_stale_pending_sales()

        sale.refresh_from_db()
        self.assertEqual(cancelled_count, 0)
        self.assertEqual(sale.status, Sale.Status.CANCELLED)

    def test_multiple_stale_sales_across_companies_are_all_cancelled(self):
        other_company = _make_company(name="Other Shop")
        sale_a = _make_sale(self.company, Sale.Status.PENDING, age=STALE_PENDING_THRESHOLD + timedelta(hours=1))
        sale_b = _make_sale(other_company, Sale.Status.PENDING, age=STALE_PENDING_THRESHOLD + timedelta(days=5))
        fresh_sale = _make_sale(self.company, Sale.Status.PENDING, age=timedelta(hours=1))

        cancelled_count = auto_cancel_stale_pending_sales()

        sale_a.refresh_from_db()
        sale_b.refresh_from_db()
        fresh_sale.refresh_from_db()
        self.assertEqual(cancelled_count, 2)
        self.assertEqual(sale_a.status, Sale.Status.CANCELLED)
        self.assertEqual(sale_b.status, Sale.Status.CANCELLED)
        self.assertEqual(fresh_sale.status, Sale.Status.PENDING)

    def test_uses_provided_now_for_deterministic_cutoff(self):
        fixed_now = timezone.now()
        sale = Sale.objects.create(company=self.company, status=Sale.Status.PENDING)
        Sale.objects.filter(pk=sale.pk).update(
            sale_timestamp=fixed_now - STALE_PENDING_THRESHOLD - timedelta(minutes=1)
        )

        cancelled_count = auto_cancel_stale_pending_sales(now=fixed_now)

        sale.refresh_from_db()
        self.assertEqual(cancelled_count, 1)
        self.assertEqual(sale.status, Sale.Status.CANCELLED)
