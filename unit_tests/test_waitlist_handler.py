"""Tests for waitlist onboarding: first-message acknowledgment (#255) and
greeting-as-company-name rejection (#256)."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.test import TransactionTestCase
from django.utils import timezone

from apps.core.models import WaitlistEntry
from apps.whatsapp.services.waitlist_handler import (
    _send_waitlist_admin_notification,
    process_language_button_async,
    process_new_waitlist_entry_async,
    process_waitlisted_message_async,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class ProcessNewWaitlistEntryAcknowledgmentTest(TransactionTestCase):
    """Regression for #255 — a real first message (e.g. a sale attempt)
    should be acknowledged, not silently discarded."""

    def test_acknowledges_non_empty_first_message(self):
        send_response_mock = AsyncMock()
        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=AsyncMock(return_value="sid_1")), \
                 patch("apps.whatsapp.services.waitlist_handler._send_response", new=send_response_mock):
                await process_new_waitlist_entry_async("+263771000500", "Sold 2 packs of noodles $2")

        _run(_run_test())
        send_response_mock.assert_called_once()
        args, _ = send_response_mock.call_args
        self.assertEqual(args[0], "+263771000500")

    def test_does_not_acknowledge_empty_first_message(self):
        send_response_mock = AsyncMock()
        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=AsyncMock(return_value="sid_1")), \
                 patch("apps.whatsapp.services.waitlist_handler._send_response", new=send_response_mock):
                await process_new_waitlist_entry_async("+263771000501", "   ")

        _run(_run_test())
        send_response_mock.assert_not_called()


class ProcessLanguageButtonConsentTest(TransactionTestCase):
    """Regression for #338 — waitlist.welcome (sent right after language
    selection, the earliest point in the flow, before any proactive message
    could ever go out) must include an active opt-in confirmation, not just
    the operational "we'll review your request" copy. Per Brighton's #338
    review: Meta's opt-in requirement isn't "outside WhatsApp only" — an
    active in-conversation confirmation is a valid pattern, our gap was
    having no active confirmation step at all."""

    def test_waitlist_welcome_includes_opt_in_language(self):
        entry = WaitlistEntry.objects.create(phone_number="+263771000600", language="en")
        send_response_mock = AsyncMock()
        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler._send_response", new=send_response_mock):
                await process_language_button_async("en", entry.id, "+263771000600")

        _run(_run_test())
        # language.confirmed, then waitlist.welcome
        self.assertEqual(send_response_mock.call_count, 2)
        welcome_text = send_response_mock.call_args_list[1][0][1]
        self.assertIn("agree to receive messages", welcome_text)
        self.assertIn("stop", welcome_text.lower())

    def test_does_not_double_prompt_duplicate_message(self):
        WaitlistEntry.objects.create(phone_number="+263771000502", first_message="hello")
        send_response_mock = AsyncMock()
        send_buttons_mock = AsyncMock(return_value="sid_1")
        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=send_buttons_mock), \
                 patch("apps.whatsapp.services.waitlist_handler._send_response", new=send_response_mock):
                await process_new_waitlist_entry_async("+263771000502", "another message")

        _run(_run_test())
        send_buttons_mock.assert_not_called()
        send_response_mock.assert_not_called()


class ProcessWaitlistedMessageGreetingTest(TransactionTestCase):
    """Regression for #256 — a greeting should not be captured verbatim as
    the shop's company_name."""

    def setUp(self):
        self.entry = WaitlistEntry.objects.create(
            phone_number="+263771000600",
            status=WaitlistEntry.Status.PENDING,
            language="en",
        )

    def _run_message(self, text):
        send_response_mock = AsyncMock()
        notify_mock = AsyncMock()
        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler._send_response", new=send_response_mock), \
                 patch("apps.whatsapp.services.waitlist_handler._send_waitlist_admin_notification", new=notify_mock):
                await process_waitlisted_message_async("+263771000600", text, self.entry)
        _run(_run_test())
        return send_response_mock, notify_mock

    def test_greeting_not_captured_as_company_name(self):
        for greeting in ("Hello", "hi", "Hie", "hey!", "HELLO."):
            with self.subTest(greeting=greeting):
                self.entry.company_name = ""
                self.entry.save()
                send_response_mock, notify_mock = self._run_message(greeting)
                self.entry.refresh_from_db()
                self.assertEqual(self.entry.company_name, "")
                notify_mock.assert_not_called()
                send_response_mock.assert_called_once()

    def test_non_greeting_still_captured_as_company_name(self):
        send_response_mock, notify_mock = self._run_message("Mai Ati Mini Eatery")
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.company_name, "Mai Ati Mini Eatery")
        notify_mock.assert_called_once()
        send_response_mock.assert_called_once()

    def test_still_pending_response_once_company_name_already_set(self):
        self.entry.company_name = "Existing Shop"
        self.entry.save()
        send_response_mock, notify_mock = self._run_message("Hello")
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.company_name, "Existing Shop")
        notify_mock.assert_not_called()
        send_response_mock.assert_called_once()


class SendWaitlistAdminNotificationTest(TransactionTestCase):
    """Regression for #281 — outside the 24h window, the admin notification
    must fall back to the approved ats_admin_new_signup template, not
    ats_reengagement (which is written for a dormant shop owner, not an
    admin alert — see #199)."""

    def setUp(self):
        self.entry = WaitlistEntry.objects.create(
            phone_number="+263771000700",
            first_message="Sold 2 Mazoe $1 each",
            status=WaitlistEntry.Status.PENDING,
        )

    def test_falls_back_to_admin_new_signup_template_when_buttons_fail(self):
        client_mock = AsyncMock()
        send_template_mock = AsyncMock()
        client_mock.send_template_message = send_template_mock

        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=AsyncMock(return_value=None)), \
                 patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=client_mock):
                await _send_waitlist_admin_notification(self.entry)

        _run(_run_test())
        send_template_mock.assert_called_once()
        args, kwargs = send_template_mock.call_args
        self.assertEqual(args[1], "ats_admin_new_signup")
        components = kwargs["components"]
        params = components[0]["parameters"]
        self.assertEqual(params[0]["text"], "+263771000700")
        self.assertEqual(params[1]["text"], "Sold 2 Mazoe $1 each")
        self.assertIn("fly.dev/admin", params[2]["text"])

    def test_does_not_use_template_fallback_when_buttons_succeed(self):
        send_template_mock = AsyncMock()

        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=AsyncMock(return_value="sid_1")), \
                 patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client") as get_client_mock:
                get_client_mock.return_value.send_template_message = send_template_mock
                await _send_waitlist_admin_notification(self.entry)

        _run(_run_test())
        send_template_mock.assert_not_called()

    def test_unhandled_send_exception_does_not_propagate(self):
        """Regression for #339 — entry #30 sat pending for 29 days with zero
        trace of any notification attempt. Root cause: an exception raised by
        the WhatsApp client (not just a graceful None return) had no handling
        anywhere in the call chain below the outer catch-all in
        handle_language_button_action/handle_waitlisted_message — by the time
        it's caught there, the shop owner already has their own confirmation
        message, so nothing looks wrong to them and the only trace is a
        single unmonitored Sentry log line. _send_waitlist_admin_notification
        must swallow this itself so callers never need to guess whether it's
        safe to call unguarded."""

        async def _run_test():
            with patch(
                "apps.whatsapp.services.waitlist_handler._send_response_with_buttons",
                new=AsyncMock(side_effect=ConnectionError("simulated network failure")),
            ):
                await _send_waitlist_admin_notification(self.entry)

        _run(_run_test())  # must not raise

    def test_stale_pending_flag_reflects_send_failure(self):
        from apps.core.admin import WaitlistEntryAdmin
        from django.contrib import admin as django_admin

        self.entry.created_at = timezone.now() - timedelta(days=4)
        self.entry.save(update_fields=["created_at"])
        admin_instance = WaitlistEntryAdmin(WaitlistEntry, django_admin.site)
        self.assertTrue(admin_instance.stale_pending_flag(self.entry))

    def test_stale_pending_flag_false_for_recent_entry(self):
        from apps.core.admin import WaitlistEntryAdmin
        from django.contrib import admin as django_admin

        admin_instance = WaitlistEntryAdmin(WaitlistEntry, django_admin.site)
        self.assertFalse(admin_instance.stale_pending_flag(self.entry))
