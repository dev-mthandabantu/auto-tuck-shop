"""Tests for multi-recipient admin notifications — #388.

A single ADMIN_PHONE_NUMBER was a point of failure: if that person missed the
message, nobody else found out. The setting now accepts a comma-separated list
and `send_to_admins` fans a send out across it.
"""

from unittest.mock import AsyncMock, patch

from django.conf import settings
from django.test import TestCase, TransactionTestCase

from apps.core.models import WaitlistEntry
from apps.whatsapp.services.waitlist_handler import (
    _secondary_admins,
    _send_waitlist_admin_notification,
)
from apps.whatsapp.services.webhook_handler import send_to_admins
from config.settings.base import parse_admin_phone_numbers
from unit_tests.async_helpers import run_async_test as _run


class SettingsParsingTest(TestCase):
    """The comma-separated setting is split into a list at settings load.

    Exercises the real parser from config.settings.base rather than a copy of
    it, so a change to the parsing rules cannot pass here while breaking the
    shipped behaviour.
    """

    def _parse(self, raw):
        return parse_admin_phone_numbers(raw)

    def test_single_number_parses_to_one_entry(self):
        self.assertEqual(self._parse("+263771000001"), ["+263771000001"])

    def test_multiple_numbers_split_on_comma(self):
        self.assertEqual(
            self._parse("+263771000001,+263771000002"),
            ["+263771000001", "+263771000002"],
        )

    def test_whitespace_around_numbers_is_stripped(self):
        self.assertEqual(
            self._parse(" +263771000001 , +263771000002 "),
            ["+263771000001", "+263771000002"],
        )

    def test_empty_and_trailing_comma_entries_are_dropped(self):
        self.assertEqual(self._parse("+263771000001,,"), ["+263771000001"])

    def test_unset_parses_to_empty_list(self):
        self.assertEqual(self._parse(None), [])

    def test_duplicate_numbers_are_collapsed(self):
        # A copy-paste slip must not WhatsApp the same admin twice.
        self.assertEqual(
            self._parse("+263771000001,+263771000001"),
            ["+263771000001"],
        )

    def test_duplicate_differing_only_by_whitespace_is_collapsed(self):
        self.assertEqual(
            self._parse("+263771000001, +263771000001 "),
            ["+263771000001"],
        )

    def test_duplicate_does_not_displace_the_primary(self):
        # First occurrence wins, so the primary admin stays the primary.
        self.assertEqual(
            self._parse("+263771000001,+263771000002,+263771000001"),
            ["+263771000001", "+263771000002"],
        )


class UnsetSettingFallbackTest(TestCase):
    """With ADMIN_PHONE_NUMBER unset, both admin settings fall back together.

    The pre-#388 code fell back to a hardcoded number when the setting was
    missing. The list must fall back to the same value, or the fan-out helper
    would reach nobody in exactly the case the single-value path still worked.
    """

    def _module_values(self, setting_number, setting_numbers):
        # Re-import webhook_handler with the settings patched, so the
        # module-level fallback is evaluated under those settings rather than
        # whatever was in place when the suite first imported it.
        import importlib

        from apps.whatsapp.services import webhook_handler

        with patch.object(settings, "ADMIN_PHONE_NUMBER", setting_number), \
             patch.object(settings, "ADMIN_PHONE_NUMBERS", setting_numbers):
            reloaded = importlib.reload(webhook_handler)
            values = (reloaded.ADMIN_PHONE_NUMBER, reloaded.ADMIN_PHONE_NUMBERS)

        # Restore the module to its real-settings state for other tests.
        importlib.reload(webhook_handler)
        return values

    def test_unset_falls_back_to_the_same_number_in_both(self):
        number, numbers = self._module_values(None, [])

        self.assertEqual(numbers, [number])
        self.assertTrue(number)

    def test_fanout_still_reaches_the_fallback_when_unset(self):
        # The regression guarded against: an empty list means send_to_admins
        # silently reaches nobody while the old single-value path still sent.
        number, numbers = self._module_values(None, [])
        send = AsyncMock(return_value=True)

        result = _run(send_to_admins(send, recipients=numbers))

        self.assertTrue(result)
        self.assertEqual([c.args[0] for c in send.call_args_list], [number])

    def test_configured_numbers_are_used_as_is(self):
        number, numbers = self._module_values("+111", ["+111", "+222"])

        self.assertEqual(number, "+111")
        self.assertEqual(numbers, ["+111", "+222"])


class SendToAdminsTest(TestCase):
    """Fan-out semantics: every recipient attempted, success if any got it."""

    def test_sends_to_every_recipient(self):
        send = AsyncMock(return_value=True)

        result = _run(send_to_admins(send, recipients=["+111", "+222"]))

        self.assertTrue(result)
        self.assertEqual([c.args[0] for c in send.call_args_list], ["+111", "+222"])

    def test_returns_true_when_only_one_recipient_succeeds(self):
        # A stale or bad second number must not make the whole notification
        # look failed — that would feed a false signal to #387's stale flag.
        send = AsyncMock(side_effect=[True, False])

        result = _run(send_to_admins(send, recipients=["+111", "+222"]))

        self.assertTrue(result)

    def test_returns_false_when_every_recipient_fails(self):
        send = AsyncMock(return_value=False)

        result = _run(send_to_admins(send, recipients=["+111", "+222"]))

        self.assertFalse(result)

    def test_one_recipient_raising_does_not_stop_the_rest(self):
        send = AsyncMock(side_effect=[Exception("network"), True])

        result = _run(send_to_admins(send, recipients=["+111", "+222"]))

        self.assertTrue(result)
        self.assertEqual(send.await_count, 2)

    def test_returns_false_and_sends_nothing_when_no_recipients(self):
        send = AsyncMock(return_value=True)

        result = _run(send_to_admins(send, recipients=[]))

        self.assertFalse(result)
        send.assert_not_awaited()

    def test_falsy_sid_counts_as_undelivered(self):
        # send_message_with_buttons returns a sid or None, not a bool.
        send = AsyncMock(return_value=None)

        self.assertFalse(_run(send_to_admins(send, recipients=["+111"])))


class SecondaryAdminsTest(TestCase):
    """The primary admin is excluded from the secondary list."""

    def test_excludes_primary(self):
        with patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBER", "+111"), \
             patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBERS", ["+111", "+222"]):
            self.assertEqual(_secondary_admins(), ["+222"])

    def test_empty_when_only_primary_configured(self):
        with patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBER", "+111"), \
             patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBERS", ["+111"]):
            self.assertEqual(_secondary_admins(), [])


class WaitlistAdminNotificationFanoutTest(TransactionTestCase):
    """Secondary admins are told about a signup, but without buttons.

    WaitlistEntry stores a single confirmation_message_sid and the approve/
    reject lookup matches on it, so only the primary can carry live buttons.
    """

    def setUp(self):
        self.entry = WaitlistEntry.objects.create(
            phone_number="+263771000500", first_message="Sold 2 breads"
        )

    def _notify(self, buttons_sid="sid_primary"):
        client = AsyncMock()
        client.send_message = AsyncMock(return_value=True)
        buttons = AsyncMock(return_value=buttons_sid)

        with patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBER", "+111"), \
             patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBERS", ["+111", "+222"]), \
             patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=buttons), \
             patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=client):
            _run(_send_waitlist_admin_notification(self.entry))

        return client, buttons

    def test_buttons_go_only_to_the_primary_admin(self):
        _, buttons = self._notify()

        self.assertEqual(buttons.await_count, 1)
        self.assertEqual(buttons.call_args.args[0], "+111")

    def test_secondary_admin_gets_a_plain_message(self):
        client, _ = self._notify()

        recipients = [c.args[0] for c in client.send_message.call_args_list]
        self.assertEqual(recipients, ["+222"])

    def test_secondary_message_carries_the_admin_panel_link(self):
        # Without buttons, the link is the secondary admin's only way to act.
        client, _ = self._notify()

        body = client.send_message.call_args.args[1]
        self.assertIn("/admin/core/waitlistentry/", body)
        self.assertIn(self.entry.phone_number, body)

    def test_secondary_still_notified_when_primary_send_fails(self):
        # The whole point of #388: one unreachable admin must not mean nobody
        # else hears about the signup.
        client = AsyncMock()
        client.send_message = AsyncMock(return_value=True)
        client.send_template_message = AsyncMock(return_value=True)

        with patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBER", "+111"), \
             patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBERS", ["+111", "+222"]), \
             patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons",
                   new=AsyncMock(side_effect=Exception("primary unreachable"))), \
             patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=client):
            _run(_send_waitlist_admin_notification(self.entry))

        recipients = [c.args[0] for c in client.send_message.call_args_list]
        self.assertIn("+222", recipients)

    def test_secondary_failure_does_not_block_the_primary(self):
        client = AsyncMock()
        client.send_message = AsyncMock(side_effect=Exception("secondary unreachable"))
        buttons = AsyncMock(return_value="sid_primary")

        with patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBER", "+111"), \
             patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBERS", ["+111", "+222"]), \
             patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=buttons), \
             patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=client):
            _run(_send_waitlist_admin_notification(self.entry))

        buttons.assert_awaited_once()
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.confirmation_message_sid, "sid_primary")

    def test_no_secondary_sends_when_only_primary_configured(self):
        client = AsyncMock()
        client.send_message = AsyncMock(return_value=True)

        with patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBER", "+111"), \
             patch("apps.whatsapp.services.waitlist_handler.ADMIN_PHONE_NUMBERS", ["+111"]), \
             patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons",
                   new=AsyncMock(return_value="sid_primary")), \
             patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=client):
            _run(_send_waitlist_admin_notification(self.entry))

        client.send_message.assert_not_awaited()
