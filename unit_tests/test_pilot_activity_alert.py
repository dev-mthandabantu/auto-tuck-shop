"""Unit tests for apps/core/services.py count_active_pilot_shops and the
pilot_activity_alert management command — #273.

Detects the pilot "going dark": real (non-test) shops with no confirmed
sale in the alert window.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Company, UserProfile
from apps.core.pilot_test_phones import PILOT_TEST_PHONES
from apps.core.services import PILOT_SILENCE_WINDOW_DAYS, count_active_pilot_shops
from apps.sales.models import Sale


def _make_company(name="Test Shop", owner_phone="+263700000001", active=True):
    company = Company.objects.create(name=name, slug=name.lower().replace(" ", "-"), active=active)
    user = User.objects.create_user(username=owner_phone.replace("+", ""))
    UserProfile.objects.create(
        user=user, company=company, role=UserProfile.Role.OWNER, phone_number=owner_phone
    )
    return company


def _make_sale(company, status, age: timedelta):
    sale = Sale.objects.create(company=company, status=status)
    Sale.objects.filter(pk=sale.pk).update(sale_timestamp=timezone.now() - age)
    return sale


class CountActivePilotShopsTest(TestCase):

    def test_zero_when_no_real_shops_have_recent_sales(self):
        company = _make_company()
        _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=PILOT_SILENCE_WINDOW_DAYS + 1))

        self.assertEqual(count_active_pilot_shops(), 0)

    def test_counts_shop_with_recent_confirmed_sale(self):
        company = _make_company()
        _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=1))

        self.assertEqual(count_active_pilot_shops(), 1)

    def test_excludes_test_phone_shops(self):
        test_phone = next(iter(PILOT_TEST_PHONES))
        company = _make_company(name="Test Account", owner_phone=test_phone)
        _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=1))

        self.assertEqual(count_active_pilot_shops(), 0)

    def test_ignores_pending_and_cancelled_sales(self):
        company = _make_company()
        _make_sale(company, Sale.Status.PENDING, age=timedelta(days=1))
        _make_sale(company, Sale.Status.CANCELLED, age=timedelta(days=1))

        self.assertEqual(count_active_pilot_shops(), 0)

    def test_counts_each_active_company_once(self):
        company = _make_company()
        _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=1))
        _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=2))

        self.assertEqual(count_active_pilot_shops(), 1)

    def test_multiple_real_shops_counted_independently(self):
        company_a = _make_company(name="Shop A", owner_phone="+263700000002")
        company_b = _make_company(name="Shop B", owner_phone="+263700000003")
        _make_sale(company_a, Sale.Status.CONFIRMED, age=timedelta(days=1))
        _make_sale(company_b, Sale.Status.CONFIRMED, age=timedelta(days=PILOT_SILENCE_WINDOW_DAYS + 1))

        self.assertEqual(count_active_pilot_shops(), 1)

    def test_excludes_inactive_company_shops(self):
        company = _make_company(active=False)
        _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=1))

        self.assertEqual(count_active_pilot_shops(), 0)

    def test_inactive_company_sale_does_not_mask_pilot_silence(self):
        active_company = _make_company(name="Real Shop", owner_phone="+263700000004", active=True)
        _make_sale(active_company, Sale.Status.CONFIRMED, age=timedelta(days=PILOT_SILENCE_WINDOW_DAYS + 1))

        inactive_company = _make_company(name="Offboarded Shop", owner_phone="+263700000005", active=False)
        _make_sale(inactive_company, Sale.Status.CONFIRMED, age=timedelta(days=1))

        self.assertEqual(count_active_pilot_shops(), 0)

    def test_respects_custom_window(self):
        company = _make_company()
        _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=10))

        self.assertEqual(count_active_pilot_shops(days=14), 1)
        self.assertEqual(count_active_pilot_shops(days=7), 0)


class PilotActivityAlertCommandTest(TestCase):

    @patch("apps.whatsapp.management.commands.pilot_activity_alert.get_whatsapp_client")
    @patch("apps.whatsapp.management.commands.pilot_activity_alert.sentry_sdk.capture_message")
    def test_fires_alert_when_no_real_shop_is_active(self, mock_capture, mock_get_client):
        mock_client = mock_get_client.return_value
        mock_client.send_message = AsyncMock(return_value=True)

        call_command("pilot_activity_alert")

        mock_capture.assert_called_once()
        self.assertEqual(mock_capture.call_args.kwargs.get("level"), "error")
        mock_client.send_message.assert_awaited_once()

    @patch("apps.whatsapp.management.commands.pilot_activity_alert.get_whatsapp_client")
    @patch("apps.whatsapp.management.commands.pilot_activity_alert.sentry_sdk.capture_message")
    def test_no_alert_when_a_real_shop_is_active(self, mock_capture, mock_get_client):
        company = _make_company()
        _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=1))
        mock_client = mock_get_client.return_value
        mock_client.send_message = AsyncMock(return_value=True)

        call_command("pilot_activity_alert")

        mock_capture.assert_not_called()
        mock_client.send_message.assert_not_awaited()
