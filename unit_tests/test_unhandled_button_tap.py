"""Unit tests for the nudge-button-tap catch-all handler — #209.

Any button_id with no dedicated handler (e.g. a quick-reply on a
Meta-approved template like ats_reengagement) is routed here so the tap can
be recorded as an engagement signal, even though we don't know what the
button was for specifically.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase

from apps.core.models import Company, UserProfile
from apps.whatsapp.services.webhook_handler import _handle_unhandled_button_tap_async


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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


class UnhandledButtonTapTest(TransactionTestCase):

    def setUp(self):
        self.company = _make_company()
        self.profile = _make_profile(self.company)

    def test_records_tap_for_known_sender(self):
        self.assertIsNone(self.company.last_unhandled_button_tap_at)
        with patch("apps.whatsapp.services.webhook_handler._get_profile_by_phone", new=AsyncMock(return_value=self.profile)):
            _run(_handle_unhandled_button_tap_async(self.profile.phone_number))
        self.company.refresh_from_db()
        self.assertIsNotNone(self.company.last_unhandled_button_tap_at)

    def test_unknown_sender_does_not_raise(self):
        """A button tap from a phone number with no UserProfile (shouldn't
        normally happen — Meta only lets known recipients tap a template
        button — but must not crash the webhook if it does)."""
        with patch("apps.whatsapp.services.webhook_handler._get_profile_by_phone", new=AsyncMock(return_value=None)):
            _run(_handle_unhandled_button_tap_async("+263799999999"))
