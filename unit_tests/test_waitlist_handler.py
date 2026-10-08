"""Tests for waitlist onboarding: first-message acknowledgment (#255) and
greeting-as-company-name rejection (#256)."""

import asyncio

from unit_tests.async_helpers import run_async_test as _run
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.core.models import WaitlistEntry
from apps.whatsapp.services.waitlist_handler import (
    _send_waitlist_admin_notification,
    process_language_button_async,
    process_new_waitlist_entry_async,
    process_waitlisted_message_async,
)




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

    def test_sale_not_captured_as_company_name(self):
        """#419: on staging, "2 sugar R45 imwe" became the shop's name."""
        for sale in ("2 sugar R45 imwe", "5 chingwa R18 chimwe", "2 Mazoe $3 rimwe", "bread 1.50",
                     "Plate Beef $1×16", "3 coke @ 1", "sold 4 bread", "ndatengesa 2 shuga", "bread 2x",
                     "10 airtime 5 zwg", "eggs 30 rand",
                     # #421 review: a number plus a product with no price, as the issue asked
                     "2 bread", "5 chingwa", "2 sugar 45", "Ndatengesa chingwa"):
            with self.subTest(sale=sale):
                self.entry.company_name = ""
                self.entry.save()
                send_response_mock, notify_mock = self._run_message(sale)
                self.entry.refresh_from_db()
                self.assertEqual(self.entry.company_name, "")
                notify_mock.assert_not_called()
                self.assertIn("looks like a sale", send_response_mock.call_args.args[1])

    def test_names_with_numbers_or_r_are_still_names(self):
        for name in ("Shop 24", "R&M General Dealer", "Stand 1043 Tuckshop", "Mai Ati Mini Eatery",
                     "Mai Tendai's shop", "Rimbi Store", "Each Day Groceries"):
            with self.subTest(name=name):
                self.entry.company_name = ""
                self.entry.save()
                _, notify_mock = self._run_message(name)
                self.entry.refresh_from_db()
                self.assertEqual(self.entry.company_name, name)

    def test_a_name_that_looks_like_a_sale_is_saved_when_sent_again(self):
        """#421 review: "4x4 Auto Spares" must not be refused forever. Sending the same text
        again (any case, any surrounding spaces) saves it."""
        for name, again in (("4x4 Auto Spares", "4x4 auto spares"), ("R2 Supermarket", " R2 Supermarket "),
                            ("7 Eleven", "7 Eleven"), ("Shop @ 5th Street", "Shop @ 5th Street")):
            with self.subTest(name=name):
                self.entry.company_name = ""
                self.entry.rejected_name_candidate = ""
                self.entry.save()
                first, first_notify = self._run_message(name)
                self.entry.refresh_from_db()
                self.assertEqual(self.entry.company_name, "")
                first_notify.assert_not_called()
                self.assertIn("send it again", first.call_args.args[1])

                _, notify_mock = self._run_message(again)
                self.entry.refresh_from_db()
                self.assertEqual(self.entry.company_name, again.strip())
                notify_mock.assert_called_once()

    def test_a_different_sale_the_second_time_is_still_turned_away(self):
        self._run_message("2 sugar R45 imwe")
        self.entry.refresh_from_db()

        send_response_mock, notify_mock = self._run_message("3 bread R18 chimwe")

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.company_name, "")
        notify_mock.assert_not_called()
        self.assertIn("looks like a sale", send_response_mock.call_args.args[1])

    def test_sale_prompt_is_in_the_entrys_language(self):
        self.entry.language = "sn"
        self.entry.save()
        send_response_mock, _ = self._run_message("2 sugar R45 imwe")
        self.assertIn("Izvi zvinoita se sale", send_response_mock.call_args.args[1])

    def test_still_pending_response_once_company_name_already_set(self):
        self.entry.company_name = "Existing Shop"
        self.entry.save()
        send_response_mock, notify_mock = self._run_message("Hello")
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.company_name, "Existing Shop")
        notify_mock.assert_not_called()
        send_response_mock.assert_called_once()


class SendWaitlistAdminNotificationTest(TransactionTestCase):
    """#387 — template (ats_admin_new_signup) is the primary admin alert,
    since interactive buttons only deliver within Meta's 24h window. Buttons
    are still attempted as a bonus (#281 — not ats_reengagement, see #199)."""

    def setUp(self):
        self.entry = WaitlistEntry.objects.create(
            phone_number="+263771000700",
            first_message="Sold 2 Mazoe $1 each",
            status=WaitlistEntry.Status.PENDING,
        )

    def test_sends_template_regardless_of_button_outcome(self):
        client_mock = AsyncMock()
        send_template_mock = AsyncMock()
        client_mock.send_template_message = send_template_mock

        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=AsyncMock(return_value="sid_1")), \
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
        self.entry.refresh_from_db()
        self.assertIsNotNone(self.entry.admin_alerted_at)
        self.assertEqual(self.entry.confirmation_message_sid, "sid_1")

    @override_settings(
        USE_MOCK_WHATSAPP=False,
        META_WHATSAPP_ACCESS_TOKEN="test-token",
        META_WHATSAPP_PHONE_NUMBER_ID="test-phone-id",
    )
    def test_template_send_posts_expected_payload_to_meta(self):
        """#387 — exercises the real send_template_message body (payload,
        credentials check, response handling), not just that it was called."""
        http_response = MagicMock()
        http_response.raise_for_status = MagicMock()
        http_response.json.return_value = {"messages": [{"id": "wamid.123"}]}
        post_mock = AsyncMock(return_value=http_response)

        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=AsyncMock(return_value=None)), \
                 patch("httpx.AsyncClient.post", new=post_mock):
                await _send_waitlist_admin_notification(self.entry)

        _run(_run_test())
        post_mock.assert_called_once()
        _, kwargs = post_mock.call_args
        payload = kwargs["json"]
        self.assertEqual(payload["template"]["name"], "ats_admin_new_signup")
        self.assertEqual(payload["template"]["language"]["code"], "en")
        params = payload["template"]["components"][0]["parameters"]
        self.assertEqual(params[0]["text"], "+263771000700")
        self.entry.refresh_from_db()
        self.assertIsNotNone(self.entry.admin_alerted_at)

    def test_button_failure_does_not_block_template(self):
        """Regression for #339 review (Brighton) — a raised exception from
        _send_response_with_buttons must not stop the template send."""
        client_mock = AsyncMock()
        send_template_mock = AsyncMock()
        client_mock.send_template_message = send_template_mock

        async def _run_test():
            with patch(
                "apps.whatsapp.services.waitlist_handler._send_response_with_buttons",
                new=AsyncMock(side_effect=ConnectionError("simulated network failure")),
            ), patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=client_mock):
                await _send_waitlist_admin_notification(self.entry)

        _run(_run_test())
        send_template_mock.assert_called_once()
        args, _ = send_template_mock.call_args
        self.assertEqual(args[1], "ats_admin_new_signup")
        self.entry.refresh_from_db()
        self.assertIsNotNone(self.entry.admin_alerted_at)

    def test_unhandled_send_exception_does_not_propagate(self):
        """Regression for #339 — entry #30 sat pending for 29 days with zero
        trace of any notification attempt. Even if the template AND the
        button send both fail (e.g. the WhatsApp client is completely
        unreachable), _send_waitlist_admin_notification must swallow the
        failure itself so callers never need to guess whether it's safe to
        call unguarded — the stale-pending admin flag is the backstop for
        exactly this case."""

        async def _run_test():
            with patch(
                "apps.whatsapp.services.waitlist_handler._send_response_with_buttons",
                new=AsyncMock(side_effect=ConnectionError("simulated network failure")),
            ), patch(
                "apps.whatsapp.services.whatsapp_client.get_whatsapp_client",
                side_effect=ConnectionError("client unreachable too"),
            ):
                await _send_waitlist_admin_notification(self.entry)

        _run(_run_test())  # must not raise
        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.admin_alerted_at)

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


class GetNeverAlertedWaitlistEntriesTest(TransactionTestCase):
    """#387 — entries the admin was never successfully notified about,
    either because no shop name has arrived yet or because both the button
    send and the template fallback failed."""

    def _make_entry(self, **kwargs):
        defaults = {
            "phone_number": "+263771000800",
            "status": WaitlistEntry.Status.PENDING,
            "created_at": timezone.now() - timedelta(minutes=31),
        }
        defaults.update(kwargs)
        entry = WaitlistEntry.objects.create(**{k: v for k, v in defaults.items() if k != "created_at"})
        WaitlistEntry.objects.filter(id=entry.id).update(created_at=defaults["created_at"])
        return WaitlistEntry.objects.get(id=entry.id)

    def test_includes_entry_never_alerted_past_the_window(self):
        from apps.core.services import get_never_alerted_waitlist_entries

        entry = self._make_entry(phone_number="+263771000801")
        self.assertIn(entry, list(get_never_alerted_waitlist_entries()))

    def test_excludes_entry_already_alerted(self):
        from apps.core.services import get_never_alerted_waitlist_entries

        entry = self._make_entry(
            phone_number="+263771000802",
            admin_alerted_at=timezone.now() - timedelta(minutes=31),
        )
        self.assertNotIn(entry, list(get_never_alerted_waitlist_entries()))

    def test_excludes_approved_entry(self):
        from apps.core.services import get_never_alerted_waitlist_entries

        entry = self._make_entry(
            phone_number="+263771000803",
            status=WaitlistEntry.Status.APPROVED,
        )
        self.assertNotIn(entry, list(get_never_alerted_waitlist_entries()))
