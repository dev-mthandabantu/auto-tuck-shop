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

from unit_tests.async_helpers import run_async_test as _run
from unittest.mock import AsyncMock, Mock, patch

import django.db.utils
from django.contrib.auth.models import User
from django.test import TransactionTestCase

from apps.core.models import Company, UserProfile
from apps.whatsapp.services.message_parser import UnifiedMessageResult
from apps.whatsapp.services.webhook_handler import _process_message_async, handle_incoming_message




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
        # not a coroutine — must mock it with Mock, not AsyncMock. First call raises
        # OperationalError (triggers the retry path), second call (the retry) raises a
        # plain Exception.
        # coro.close() matters: the call site builds _process_message_async(...) eagerly,
        # so a mock that raises instead of running it leaves an un-awaited coroutine and a
        # "coroutine was never awaited" RuntimeWarning in the test output. The body never
        # ran, so no DB connection was opened — this silences noise, it does not fix a
        # leak. Same reason test_concurrent_message_safety.py closes it.
        raises = iter([django.db.utils.OperationalError("connection lost"), Exception("retry also failed")])

        def fake_run_async(coro):
            coro.close()
            raise next(raises)

        run_async_mock = Mock(side_effect=fake_run_async)
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


class HelpMenuTest(TransactionTestCase):
    """#131: help/options/menu (EN + SN) and free-form questions must return the help menu.

    Two distinct paths, both landing on the same t("help.menu", ...) reply:
    1. Literal fast-path — exact trigger words, matched before the LLM is ever called
       (mirrors the stop/unsubscribe pattern already in _process_message_async).
    2. LLM-classified path — other_sub_intent == "question" now routes to help.menu
       instead of the old generic other.question fallback, since help-seeking phrasing
       is too varied for a fixed word list to cover on its own.
    """

    def setUp(self):
        self.profile = _make_profile(phone="+263771000399")
        self.sender = self.profile.phone_number

    def test_fast_path_trigger_words_send_help_menu_without_calling_llm(self):
        """Exact trigger words must short-circuit before parse_message_unified is ever
        invoked — this is what makes it a genuine fast-path, not just a fast reply."""
        parse_mock = AsyncMock()
        send_mock = AsyncMock()
        for trigger in ("help", "options", "menu", "rubatsiro", "HELP", "Menu"):
            with self.subTest(trigger=trigger):
                parse_mock.reset_mock()
                send_mock.reset_mock()
                with (
                    patch("apps.whatsapp.services.webhook_handler.parse_message_unified", new=parse_mock),
                    patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
                ):
                    _run(_process_message_async("msg", self.sender, trigger, self.profile))
                parse_mock.assert_not_called()
                send_mock.assert_called_once()
                self.assertIn("I can help you with", send_mock.call_args[0][1])

    def test_sentence_containing_trigger_word_does_not_fast_path(self):
        """Matching is on the whole trimmed message, not a substring — a real sentence
        that happens to contain "help" must still go to the LLM like any other message,
        not get intercepted as if it were the literal word."""
        result = UnifiedMessageResult(intent="other", confidence=0.6, other_sub_intent="deferral")
        parse_mock = AsyncMock(return_value=result)
        send_mock = AsyncMock()
        with (
            patch("apps.whatsapp.services.webhook_handler.parse_message_unified", new=parse_mock),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
        ):
            _run(_process_message_async("msg", self.sender, "I need help with prices", self.profile))
        parse_mock.assert_called_once()

    def test_llm_question_sub_intent_sends_help_menu(self):
        """Free-form help-seeking phrasing the fixed word list can't cover (e.g. "how do I
        delete a sale?") is classified by the LLM as other_sub_intent == "question" and
        must now route to help.menu instead of the old other.question fallback."""
        result = UnifiedMessageResult(intent="other", confidence=0.8, other_sub_intent="question")
        send_mock = AsyncMock()
        with (
            patch("apps.whatsapp.services.webhook_handler.parse_message_unified", new=AsyncMock(return_value=result)),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
        ):
            _run(_process_message_async("msg", self.sender, "how do I delete a sale?", self.profile))
        send_mock.assert_called_once()
        self.assertIn("I can help you with", send_mock.call_args[0][1])

    def test_shona_language_profile_still_receives_localised_help_menu(self):
        """t() resolves the localised string internally — this just confirms the call
        site passes the user's actual language through, not a hardcoded "en"."""
        self.profile.language = "sn"
        self.profile.save(update_fields=["language"])
        send_mock = AsyncMock()
        with (
            patch("apps.whatsapp.services.webhook_handler.parse_message_unified", new=AsyncMock()),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
        ):
            _run(_process_message_async("msg", self.sender, "rubatsiro", self.profile))
        send_mock.assert_called_once()
        sent_text = send_mock.call_args[0][1]
        self.assertIn("Ndinogona kukubatsira", sent_text)
