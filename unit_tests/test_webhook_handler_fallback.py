"""Regression tests for issue #284: the bot going fully silent on some inputs.

Two distinct bugs, two distinct fixes, both in webhook_handler.py:

1. `handle_incoming_message` / `handle_incoming_media_message` / `handle_incoming_audio_message`
   only logged unhandled downstream exceptions — the shop owner got no WhatsApp reply at all.
   Fixed by `_send_fallback_error` in the outer except blocks.
2. The LLM's "other/greeting" bucket also catches things it can't otherwise classify (a bare
   emoji, "switch to Shona") and the bot stayed silent for ALL of "greeting", not just real
   filler words. Fixed by gating the silence on `_SILENT_PATTERN` matching the raw text, not
   on the LLM label alone.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase

from apps.core.models import Company, UserProfile
from apps.whatsapp.services.message_parser import UnifiedMessageResult
from apps.whatsapp.services.webhook_handler import _process_message_async, handle_incoming_message


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_profile(phone="+263771000199"):
    company = Company.objects.create(name="Test Shop", slug="test-shop-fallback", active=True)
    user = User.objects.create_user(username=phone, password="x")
    return UserProfile.objects.create(
        user=user,
        company=company,
        phone_number=phone,
        role=UserProfile.Role.OWNER,
        language="en",
    )


class FallbackErrorOnDownstreamExceptionTest(TransactionTestCase):
    """An exception anywhere downstream of the LLM parse must still produce a reply."""

    def setUp(self):
        self.profile = _make_profile()
        self.sender = self.profile.phone_number

    def test_exception_in_sale_processing_still_replies(self):
        sale_result = UnifiedMessageResult(intent="sale", confidence=0.9, items=[], currency=None)
        send_mock = AsyncMock()
        with (
            patch("apps.whatsapp.services.webhook_handler.parse_message_unified", new=AsyncMock(return_value=sale_result)),
            patch("apps.whatsapp.services.sale_handler.process_sale_message_unified", new=AsyncMock(side_effect=Exception("boom"))),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
        ):
            handle_incoming_message("msg_284a", self.sender, "5 bread", self.profile)

        send_mock.assert_called_once()
        msg = send_mock.call_args[0][1]
        self.assertIn("something went wrong", msg.lower())


class GreetingSilenceGateTest(TransactionTestCase):
    """Only recognised filler words may stay silent — not every LLM 'greeting' label."""

    def setUp(self):
        self.profile = _make_profile(phone="+263771000299")
        self.sender = self.profile.phone_number

    def _run_other_greeting(self, text):
        result = UnifiedMessageResult(intent="other", confidence=0.7, other_sub_intent="greeting")
        send_mock = AsyncMock()
        with (
            patch("apps.whatsapp.services.webhook_handler.parse_message_unified", new=AsyncMock(return_value=result)),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
        ):
            _run(_process_message_async("msg", self.sender, text, self.profile))
        return send_mock

    def test_emoji_only_classified_as_greeting_still_replies(self):
        """A bare emoji is trained as a 'greeting' example in the LLM prompt — must not go silent."""
        send_mock = self._run_other_greeting("\U0001F44D")
        send_mock.assert_called_once()

    def test_language_switch_request_classified_as_greeting_still_replies(self):
        """No dedicated intent exists for language-switch requests; if the LLM buckets it as
        'greeting' for lack of a better fit, the shop owner must still get some reply."""
        send_mock = self._run_other_greeting("switch to Shona")
        send_mock.assert_called_once()

    def test_recognised_filler_word_stays_silent(self):
        """Real filler acknowledgements ('hi', 'thanks', 'ok') keep the original no-reply behaviour."""
        send_mock = self._run_other_greeting("hi")
        send_mock.assert_not_called()
