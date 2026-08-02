"""Unit tests for pilot health alerting."""

from datetime import timedelta
from io import StringIO
from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Company, UserProfile
from apps.sales.models import Sale
from apps.whatsapp.services.pilot_health import build_pilot_health_snapshot


def _create_company(name: str, phone: str, slug: str, active: bool = True) -> Company:
    company = Company.objects.create(name=name, slug=slug, active=active)
    user = User.objects.create_user(username=phone, password="x")
    UserProfile.objects.create(
        user=user,
        company=company,
        role=UserProfile.Role.OWNER,
        phone_number=phone,
    )
    return company


def _create_confirmed_sale(company: Company, days_ago: int = 0) -> None:
    Sale.objects.create(
        company=company,
        sale_timestamp=timezone.now() - timedelta(days=days_ago),
        total_amount=10,
        status=Sale.Status.CONFIRMED,
    )


class PilotHealthSnapshotTest(TestCase):
    def test_excludes_test_phones_from_real_shop_count(self):
        real_shop = _create_company("Real Shop", "+263771000001", "real-shop")
        test_shop = _create_company("Test Shop", "+27644178150", "test-shop")
        _create_confirmed_sale(real_shop, days_ago=1)
        _create_confirmed_sale(test_shop, days_ago=1)

        snapshot = build_pilot_health_snapshot(days=7)

        self.assertEqual(snapshot["real_shop_count"], 1)
        self.assertEqual(snapshot["active_count"], 1)
        self.assertEqual(snapshot["active_shops"][0]["name"], "Real Shop")
        self.assertEqual(snapshot["quiet_shops"], [])


class PilotHealthCommandTest(TestCase):
    def test_alerts_when_no_real_shop_is_active(self):
        _create_company("Quiet Shop", "+263771000002", "quiet-shop")
        stdout = StringIO()

        with patch(
            "apps.whatsapp.management.commands.pilot_health_alert.send_pilot_health_alert",
            new=AsyncMock(return_value={"channel": "sentry", "sent": True, "message": "alert"}),
        ) as mock_alert:
            call_command("pilot_health_alert", stdout=stdout, days=7, min_active_shops=1)

        self.assertEqual(mock_alert.await_count, 1)
        output = stdout.getvalue()
        self.assertIn("Pilot health: 0/1 real shops active", output)
        self.assertIn("Alert sent via sentry", output)

    def test_does_not_alert_when_threshold_is_met(self):
        shop = _create_company("Busy Shop", "+263771000003", "busy-shop")
        _create_confirmed_sale(shop, days_ago=0)
        stdout = StringIO()

        with patch(
            "apps.whatsapp.management.commands.pilot_health_alert.send_pilot_health_alert",
            new=AsyncMock(),
        ) as mock_alert:
            call_command("pilot_health_alert", stdout=stdout, days=7, min_active_shops=1)

        self.assertEqual(mock_alert.await_count, 0)
        output = stdout.getvalue()
        self.assertIn("Pilot health: 1/1 real shops active", output)
        self.assertIn("Pilot health is within the expected threshold.", output)