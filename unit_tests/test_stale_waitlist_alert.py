"""Unit tests for the stale_waitlist_alert management command — #387.

Alerts the admin when a PENDING WaitlistEntry has gone unnotified past
NEVER_ALERTED_MINUTES — covers the no-shop-name case (notification never
attempted) and the button+template double-failure case.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.core.models import WaitlistEntry


def _make_entry(company_name="", admin_alerted_at=None, stale_alert_sent_at=None, minutes_old=31, phone_number="+263771000900"):
    entry = WaitlistEntry.objects.create(
        phone_number=phone_number,
        status=WaitlistEntry.Status.PENDING,
        company_name=company_name,
        admin_alerted_at=admin_alerted_at,
        stale_alert_sent_at=stale_alert_sent_at,
    )
    WaitlistEntry.objects.filter(id=entry.id).update(
        created_at=timezone.now() - timedelta(minutes=minutes_old)
    )
    return entry


class StaleWaitlistAlertCommandTest(TestCase):

    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.get_whatsapp_client")
    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.sentry_sdk.capture_message")
    def test_fires_alert_for_never_alerted_entry(self, mock_capture, mock_get_client):
        _make_entry()
        mock_client = mock_get_client.return_value
        mock_client.send_message = AsyncMock(return_value=True)

        call_command("stale_waitlist_alert")

        mock_capture.assert_called_once()
        self.assertEqual(mock_capture.call_args.kwargs.get("level"), "error")
        mock_client.send_message.assert_awaited_once()

    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.get_whatsapp_client")
    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.sentry_sdk.capture_message")
    def test_no_alert_when_entry_already_alerted(self, mock_capture, mock_get_client):
        _make_entry(company_name="Vatombe Trading", admin_alerted_at=timezone.now() - timedelta(minutes=31))
        mock_client = mock_get_client.return_value
        mock_client.send_message = AsyncMock(return_value=True)

        call_command("stale_waitlist_alert")

        mock_capture.assert_not_called()
        mock_client.send_message.assert_not_awaited()

    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.get_whatsapp_client")
    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.sentry_sdk.capture_message")
    def test_message_reports_no_shop_name_count(self, mock_capture, mock_get_client):
        _make_entry(company_name="", phone_number="+263771000901")
        _make_entry(company_name="Vatombe Trading", phone_number="+263771000902")
        mock_client = mock_get_client.return_value
        mock_client.send_message = AsyncMock(return_value=True)

        call_command("stale_waitlist_alert")

        message = mock_client.send_message.call_args.args[1]
        self.assertIn("2 pending", message)
        self.assertIn("1 with no shop name", message)

    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.get_whatsapp_client")
    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.sentry_sdk.capture_message")
    def test_already_pinged_entry_excluded_from_whatsapp_but_not_sentry(self, mock_capture, mock_get_client):
        _make_entry(stale_alert_sent_at=timezone.now() - timedelta(minutes=5))
        mock_client = mock_get_client.return_value
        mock_client.send_message = AsyncMock(return_value=True)

        call_command("stale_waitlist_alert")

        mock_capture.assert_called_once()
        mock_client.send_message.assert_not_awaited()

    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.get_whatsapp_client")
    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.sentry_sdk.capture_message")
    def test_entry_not_repinged_on_second_run(self, mock_capture, mock_get_client):
        _make_entry()
        mock_client = mock_get_client.return_value
        mock_client.send_message = AsyncMock(return_value=True)

        call_command("stale_waitlist_alert")
        mock_client.send_message.assert_awaited_once()

        mock_client.send_message.reset_mock()
        call_command("stale_waitlist_alert")
        mock_client.send_message.assert_not_awaited()

    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.get_whatsapp_client")
    @patch("apps.whatsapp.management.commands.stale_waitlist_alert.sentry_sdk.capture_message")
    def test_not_marked_pinged_when_send_fails(self, mock_capture, mock_get_client):
        entry = _make_entry()
        mock_client = mock_get_client.return_value
        mock_client.send_message = AsyncMock(return_value=False)

        call_command("stale_waitlist_alert")

        entry.refresh_from_db()
        self.assertIsNone(entry.stale_alert_sent_at)
