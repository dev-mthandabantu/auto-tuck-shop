"""Unit tests for waitlist onboarding behavior."""

import asyncio
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
import django

django.setup()

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from apps.whatsapp.services.waitlist_handler import process_new_waitlist_entry_async


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class WaitlistHandlerTest(TestCase):

    def test_new_waitlist_entry_acknowledges_first_message(self):
        entry = SimpleNamespace(id=123)
        with patch("apps.whatsapp.services.waitlist_handler._create_waitlist_entry", new=AsyncMock(return_value=(entry, True))) as create_entry_mock, \
             patch("apps.whatsapp.services.waitlist_handler.t", side_effect=lambda key, **kwargs: key) as t_mock, \
             patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=AsyncMock(return_value="msg_sid")) as send_buttons_mock, \
             patch("apps.whatsapp.services.waitlist_handler._store_waitlist_response_message_sid", new=AsyncMock()) as store_sid_mock:
            _run(process_new_waitlist_entry_async("+263778083288", "Sold 2 packs of noodles $2"))

        create_entry_mock.assert_awaited_once()
        send_buttons_mock.assert_awaited_once()
        assert send_buttons_mock.call_args[0][1] == "language.prompt_with_first_message"
        store_sid_mock.assert_awaited_once_with(entry.id, "msg_sid")

    def test_new_waitlist_entry_uses_default_prompt_for_empty_message(self):
        entry = SimpleNamespace(id=234)
        with patch("apps.whatsapp.services.waitlist_handler._create_waitlist_entry", new=AsyncMock(return_value=(entry, True))), \
             patch("apps.whatsapp.services.waitlist_handler.t", side_effect=lambda key, **kwargs: key) as t_mock, \
             patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=AsyncMock(return_value="msg_sid")) as send_buttons_mock, \
             patch("apps.whatsapp.services.waitlist_handler._store_waitlist_response_message_sid", new=AsyncMock()):
            _run(process_new_waitlist_entry_async("+263778083289", "    "))

        assert send_buttons_mock.call_args[0][1] == "language.prompt"
