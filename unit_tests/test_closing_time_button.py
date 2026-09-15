"""Unit tests for the closing time button handler."""

import asyncio

from unit_tests.async_helpers import run_async_test as _run
from datetime import time
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase

from apps.core.models import Company, UserProfile
from apps.whatsapp.services.webhook_handler import _handle_closing_time_button_async




def _make_company(name="Test Shop"):
    return Company.objects.create(
        name=name,
        slug=name.lower().replace(" ", "-"),
        active=True,
    )


def _make_profile(company, phone="+263771000001"):
    user = User.objects.create_user(username=phone, password="x")
    return UserProfile.objects.create(
        user=user,
        company=company,
        phone_number=phone,
        role=UserProfile.Role.OWNER,
        language="en",
    )


class ClosingTimeButtonTest(TransactionTestCase):

    def setUp(self):
        self.company = _make_company()
        self.profile = _make_profile(self.company)

    def _run_handler(self, button_id):
        with patch("apps.whatsapp.services.webhook_handler._get_profile_by_phone", new=AsyncMock(return_value=self.profile)), \
             patch("apps.whatsapp.services.webhook_handler._send_response", new=AsyncMock()):
            _run(_handle_closing_time_button_async(self.profile.phone_number, button_id))
        self.company.refresh_from_db()

    def test_closing_early_stores_6pm(self):
        self._run_handler("closing_early")
        self.assertEqual(self.company.normal_closing_time, time(18, 0))

    def test_closing_mid_stores_7pm(self):
        self._run_handler("closing_mid")
        self.assertEqual(self.company.normal_closing_time, time(19, 0))

    def test_closing_late_stores_9pm(self):
        self._run_handler("closing_late")
        self.assertEqual(self.company.normal_closing_time, time(21, 0))

    def test_unknown_button_id_does_nothing(self):
        _run(_handle_closing_time_button_async(self.profile.phone_number, "closing_unknown"))
        self.company.refresh_from_db()
        self.assertIsNone(self.company.normal_closing_time)

    def test_sends_confirmation_message(self):
        mock_send = AsyncMock()
        with patch("apps.whatsapp.services.webhook_handler._get_profile_by_phone", new=AsyncMock(return_value=self.profile)), \
             patch("apps.whatsapp.services.webhook_handler._send_response", new=mock_send):
            _run(_handle_closing_time_button_async(self.profile.phone_number, "closing_mid"))
        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        self.assertIn("8:00", args[1])  # summary fires 1h after 7pm = 8pm
