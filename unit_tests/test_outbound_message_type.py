"""Regression tests for #334: outbound WhatsApp sends must record message_type
using the WhatsAppMessage.MessageType enum values (lowercase), not the
uppercase string literals the code used to write.

These call the real WhatsAppClient methods (not mocked) with credentials
forced unset, so each hits its early-return "not configured" branch — no
real network call, but the exact same _record_outbound_message call that
would run on the real send path.

Run with:
    python manage.py test unit_tests.test_outbound_message_type
"""

import asyncio

from unit_tests.async_helpers import run_async_test as _run

from django.test import TransactionTestCase

from apps.whatsapp.models import WhatsAppMessage
from apps.whatsapp.services.whatsapp_client import WhatsAppClient




class OutboundMessageTypeTest(TransactionTestCase):
    # _record_outbound_message is sync_to_async(thread_sensitive=True) — its
    # DB write runs on a separate thread with its own connection. A plain
    # TestCase's per-test transaction isn't committed, so it's invisible to
    # that thread (same reason documented in test_backfill_assistant_add.py).

    def setUp(self):
        self.client = WhatsAppClient()
        self.client.access_token = None  # forces the early-return path, no real HTTP call

    def _last_message_for(self, phone):
        return WhatsAppMessage.objects.filter(phone_number=phone).order_by("-timestamp").first()

    def test_send_message_records_lowercase_text(self):
        _run(self.client.send_message("+263771119901", "hello"))
        msg = self._last_message_for("+263771119901")
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, WhatsAppMessage.MessageType.TEXT)
        self.assertEqual(msg.message_type, "text")

    def test_send_message_with_buttons_records_lowercase_interactive_button(self):
        _run(self.client.send_message_with_buttons(
            "+263771119902", "pick one", [{"id": "a", "title": "A"}]
        ))
        msg = self._last_message_for("+263771119902")
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, WhatsAppMessage.MessageType.INTERACTIVE_BUTTON)
        self.assertEqual(msg.message_type, "interactive_button")

    def test_send_image_records_lowercase_image(self):
        _run(self.client.send_image("+263771119903", "https://example.com/x.png"))
        msg = self._last_message_for("+263771119903")
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, WhatsAppMessage.MessageType.IMAGE)
        self.assertEqual(msg.message_type, "image")
