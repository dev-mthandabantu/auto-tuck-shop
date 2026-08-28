"""Regression tests for #316: capturing a shop owner's reply to the
unnamed-shop followup as the real Company.name.

This is the other half of #316 — maybe_send_unnamed_shop_followup (tested in
test_nudge_service.py) only sends the question; these tests cover the
pre-LLM interception in _process_message_async that captures the answer.

Run with:
    python manage.py test unit_tests.test_unnamed_shop_name_capture
"""

import asyncio
from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase

from apps.core.models import Company, UserProfile
from apps.whatsapp.services.message_parser import UnifiedMessageResult
from apps.whatsapp.services.webhook_handler import _process_message_async


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_profile(name="Unnamed Shop", followup_sent=True, phone="+263771000400"):
    company = Company.objects.create(
        name=name, slug=f"unnamed-shop-capture-{phone[-4:]}", active=True,
        unnamed_shop_followup_sent=followup_sent,
    )
    user = User.objects.create_user(username=phone, password="x")
    return UserProfile.objects.create(
        user=user, company=company, phone_number=phone,
        role=UserProfile.Role.OWNER, language="en",
    )


class UnnamedShopNameCaptureTest(TransactionTestCase):

    def _run_message(self, profile, text):
        send_mock = AsyncMock()
        # A fallthrough (not intercepted) reaches parse_message_unified for
        # real — give it a harmless "other/deferral" result so it doesn't
        # continue into sale processing (which would need a real currency
        # string, not a bare Mock, to satisfy Company.currency's 3-char field).
        parse_mock = AsyncMock(return_value=UnifiedMessageResult(
            intent="other", confidence=0.9, other_sub_intent="deferral"
        ))
        with (
            patch("apps.whatsapp.services.webhook_handler.parse_message_unified", new=parse_mock),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
        ):
            _run(_process_message_async("msg", profile.phone_number, text, profile))
        return send_mock, parse_mock

    def test_captures_name_reply_after_followup_sent(self):
        profile = _make_profile(followup_sent=True)
        send_mock, parse_mock = self._run_message(profile, "Mai Ati Mini Eatery")

        profile.company.refresh_from_db()
        self.assertEqual(profile.company.name, "Mai Ati Mini Eatery")
        send_mock.assert_called_once()
        msg = send_mock.call_args[0][1]
        self.assertIn("Mai Ati Mini Eatery", msg)
        parse_mock.assert_not_called()  # intercepted before the LLM, not a fallthrough

    def test_filler_reply_is_not_captured_as_a_name(self):
        """'ok'/'thanks' must not become the shop's permanent name — falls
        through to normal LLM processing instead, same _is_silent_greeting
        gate already used for greeting silence."""
        profile = _make_profile(followup_sent=True)
        send_mock, parse_mock = self._run_message(profile, "ok")

        profile.company.refresh_from_db()
        self.assertEqual(profile.company.name, "Unnamed Shop")
        parse_mock.assert_called_once()  # fell through, normal processing continued

    def test_no_interception_before_followup_was_sent(self):
        """If we haven't asked yet, don't treat an incoming message as a name
        attempt — falls through to normal processing."""
        profile = _make_profile(followup_sent=False)
        send_mock, parse_mock = self._run_message(profile, "Mai Ati Mini Eatery")

        profile.company.refresh_from_db()
        self.assertEqual(profile.company.name, "Unnamed Shop")
        parse_mock.assert_called_once()

    def test_no_interception_once_a_real_name_is_already_set(self):
        """Once the name is real, later messages are never captured as a
        name again, even if unnamed_shop_followup_sent is still True."""
        profile = _make_profile(name="Mai Ati Mini Eatery", followup_sent=True)
        send_mock, parse_mock = self._run_message(profile, "2 Mazoe $1 each")

        profile.company.refresh_from_db()
        self.assertEqual(profile.company.name, "Mai Ati Mini Eatery")
        parse_mock.assert_called_once()

    def test_sale_shaped_reply_is_not_captured_as_a_name(self):
        """Regression for PR #341 review: the very first message after the
        followup is sent could legitimately be a sale, not an answer to the
        name question ("2 Mazoe $1 each" has a digit but isn't a filler
        word, so _is_silent_greeting alone wouldn't have caught it). Must
        fall through to normal sale processing, not get captured as the
        shop's new name."""
        profile = _make_profile(followup_sent=True)
        send_mock, parse_mock = self._run_message(profile, "2 Mazoe $1 each")

        profile.company.refresh_from_db()
        self.assertEqual(profile.company.name, "Unnamed Shop")
        parse_mock.assert_called_once()  # fell through, normal (sale) processing continued

    def test_name_containing_a_digit_but_no_currency_is_still_captured(self):
        """The sale-shaped guard must not over-reject — a real shop name
        with a digit and no currency indicator (e.g. "Shop 24") is not
        remotely sale-shaped and should still be captured normally."""
        profile = _make_profile(followup_sent=True)
        send_mock, parse_mock = self._run_message(profile, "Shop 24")

        profile.company.refresh_from_db()
        self.assertEqual(profile.company.name, "Shop 24")
        parse_mock.assert_not_called()
