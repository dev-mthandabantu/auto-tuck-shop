"""Tests for waitlist onboarding: first-message acknowledgment (#255) and
greeting-as-company-name rejection (#256)."""

import asyncio
from unittest.mock import AsyncMock, patch

from django.test import TransactionTestCase

from apps.core.models import WaitlistEntry
from apps.whatsapp.services.waitlist_handler import (
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
