"""Regression tests for issue #284: the bot going fully silent on some inputs.

Two distinct bugs, two distinct fixes, both in webhook_handler.py:

1. `handle_incoming_message` / `handle_incoming_media_message` / `handle_incoming_audio_message`
   only logged unhandled downstream exceptions — the shop owner got no WhatsApp reply at all.
   Fixed by `_send_fallback_error` in the outer except blocks. A second, narrower bug in the
   same area: the retry-on-OperationalError branch could itself raise, and since that retry
   lived inside the `except OperationalError:` block, a failure there was NOT caught by the
   sibling `except Exception:` clause — it propagated fully uncaught, skipping the fallback
   entirely. Fixed by wrapping the retry call in its own try/except.
2. This branch's primary fix for #284 is prompt-level — services/openrouter/prompts.py now
   tells the LLM to classify reactive emoji/fillers as "greeting" and product-like emoji as
   "sale". `_is_silent_greeting()` is a code-level backstop: even if the LLM mis-classifies
   something as "greeting", the bot only actually stays silent when the raw text matches a
   recognised filler-word/emoji pattern — never on the LLM label alone. That pattern must
   cover every example the prompt trains the LLM on ("hi", "okay", "thanks", "good morning",
   "👍", "🙏", "❤️", "✅"), or a real greeting the LLM correctly classifies could still get an
   unwanted reply.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import django.db.utils
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
    import uuid
    company = Company.objects.create(name="Test Shop", slug=f"test-shop-fallback-{uuid.uuid4().hex[:8]}", active=True)
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

    def test_exception_on_retry_after_operational_error_still_replies(self):
        """The retry branch lives inside `except OperationalError:` — a sibling to
        `except Exception:`, not covered by it. Without the fix, a failure during the
        retry itself propagates fully uncaught and skips the fallback reply entirely,
        reproducing #284's exact failure mode on a DB blip that outlasts one retry."""
        send_mock = Mock()
        # run_async is a plain sync function (it manages the event loop internally),
        # not a coroutine — must mock it with Mock, not AsyncMock.
        # First call raises OperationalError (triggers the retry path), second call
        # (the retry itself) raises a plain Exception.
        run_async_mock = Mock(
            side_effect=[django.db.utils.OperationalError("connection lost"), Exception("retry also failed")]
        )
        with (
            patch("apps.whatsapp.services.webhook_handler.run_async", new=run_async_mock),
            patch("apps.whatsapp.services.webhook_handler._send_fallback_error", new=send_mock),
        ):
            handle_incoming_message("msg_284b", self.sender, "5 bread", self.profile)

        send_mock.assert_called_once()


class GreetingSilenceGateTest(TransactionTestCase):
    """Only recognised filler words/emoji may stay silent — not every LLM 'greeting' label."""

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

    def test_recognised_filler_word_stays_silent(self):
        """Real filler acknowledgements ('hi', 'thanks', 'ok') keep the original no-reply behaviour."""
        send_mock = self._run_other_greeting("hi")
        send_mock.assert_not_called()

    def test_llm_prompt_greeting_examples_stay_silent(self):
        """services/openrouter/prompts.py explicitly trains the LLM's "greeting" sub-intent
        on "hi", "okay", "thanks", "good morning", "👍", "🙏", "❤️", and "✅" — every one of
        those must still produce no reply, or the LLM correctly classifying a real greeting
        still results in an unwanted reply."""
        for example in ("hi", "okay", "thanks", "good morning", "\U0001F44D", "\U0001F64F", "❤️", "✅"):
            with self.subTest(example=example):
                send_mock = self._run_other_greeting(example)
                send_mock.assert_not_called()

    def test_multi_word_and_repeated_emoji_greetings_stay_silent(self):
        """Not just the LLM prompt's exact examples — nearby real-world phrasing a shop
        owner would plausibly send."""
        for example in ("thanks so much!", "thank you very much", "mangwanani", "\U0001F44D\U0001F44D"):
            with self.subTest(example=example):
                send_mock = self._run_other_greeting(example)
                send_mock.assert_not_called()

    def test_unrecognised_greeting_falls_through_to_reply(self):
        """If the LLM classifies something as "greeting" that ISN'T a recognised filler
        word/emoji, the code-level backstop must still produce a reply rather than trust
        the label blindly — this is the actual #284 regression this gate protects against."""
        send_mock = self._run_other_greeting("switch to Shona")
        send_mock.assert_called_once()

    def test_non_greeting_emoji_combination_still_replies(self):
        """An emoji combination that isn't a plausible plain greeting/reaction should still
        fall through to a reply — the emoji-only silence gate must not swallow everything
        containing an emoji, only messages that are ENTIRELY emoji/whitespace."""
        send_mock = self._run_other_greeting("\U0001F44D see attached")
        send_mock.assert_called_once()
