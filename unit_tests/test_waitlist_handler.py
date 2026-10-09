"""Tests for waitlist onboarding: first-message acknowledgment (#255) and
greeting-as-company-name rejection (#256)."""

import asyncio

from unit_tests.async_helpers import run_async_test as _run
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import sync_to_async
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.core.models import WaitlistEntry
from apps.whatsapp.services.waitlist_handler import (
    _send_waitlist_admin_notification,
    process_language_button_async,
    process_new_waitlist_entry_async,
    process_shop_name_button_async,
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

    def _run_message(self, text, buttons_sent="wamid.buttons"):
        """Send `text` as the waitlisted owner. Yes/No questions go to `self.buttons` (#423)."""
        send_response_mock = AsyncMock()
        notify_mock = AsyncMock()
        self.buttons = AsyncMock(return_value=buttons_sent)
        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler._send_response", new=send_response_mock), \
                 patch("apps.whatsapp.services.waitlist_handler._send_response_with_buttons", new=self.buttons), \
                 patch("apps.whatsapp.services.waitlist_handler._send_waitlist_admin_notification", new=notify_mock):
                await process_waitlisted_message_async("+263771000600", text, self.entry)
        _run(_run_test())
        self.entry.refresh_from_db()
        return send_response_mock, notify_mock

    def _reset(self):
        self.entry.company_name = ""
        self.entry.pending_name_candidate = ""
        self.entry.save()

    def assertAskedToConfirm(self, text, notify_mock):
        self.assertEqual(self.entry.company_name, "")
        notify_mock.assert_not_called()
        self.buttons.assert_awaited_once()
        self.assertIn(f"*{text}*", self.buttons.await_args.args[1])
        self.assertEqual(self.entry.pending_name_candidate, text)

    def test_greeting_not_captured_as_company_name(self):
        for greeting in ("Hello", "hi", "Hie", "hey!", "HELLO."):
            with self.subTest(greeting=greeting):
                self._reset()
                send_response_mock, notify_mock = self._run_message(greeting)
                self.assertEqual(self.entry.company_name, "")
                notify_mock.assert_not_called()
                send_response_mock.assert_called_once()

    def test_a_plain_name_is_asked_about_too(self):
        """#423: every name is confirmed with one tap; nothing is saved without a Yes."""
        _, notify_mock = self._run_message("Mai Ati Mini Eatery")
        self.assertAskedToConfirm("Mai Ati Mini Eatery", notify_mock)

    def test_sale_not_captured_as_company_name(self):
        """#419: on staging, "2 sugar R45 imwe" became the shop's name. #423: it is asked about
        (Yes/No) instead of turned away, and never saved without a Yes."""
        for sale in ("2 sugar R45 imwe", "5 chingwa R18 chimwe", "2 Mazoe $3 rimwe", "bread 1.50",
                     "Plate Beef $1×16", "3 coke @ 1", "sold 4 bread", "ndatengesa 2 shuga", "bread 2x",
                     "10 airtime 5 zwg", "eggs 30 rand", "2 bread", "5 chingwa", "2 sugar 45", "Ndatengesa chingwa"):
            with self.subTest(sale=sale):
                self._reset()
                _, notify_mock = self._run_message(sale)
                self.assertAskedToConfirm(sale, notify_mock)

    def test_every_name_is_asked_about_first(self):
        for name in ("R&M General Dealer", "Mai Ati Mini Eatery", "Mai Tendai's shop", "Rimbi Store",
                     "Each Day Groceries", "We sell groceries"):
            with self.subTest(name=name):
                self._reset()
                _, notify_mock = self._run_message(name)
                self.assertAskedToConfirm(name, notify_mock)

    def test_the_same_sale_sent_twice_is_never_saved(self):
        """#421 saved a sale sent twice as the name (Omar's review). #423: still only asked."""
        self._run_message("3 cokes and 3 book")
        _, notify_mock = self._run_message("3 cokes and 3 book")
        self.assertAskedToConfirm("3 cokes and 3 book", notify_mock)

    def test_a_different_sale_the_second_time_is_asked_about_too(self):
        self._run_message("2 sugar R45 imwe")
        _, notify_mock = self._run_message("3 bread R18 chimwe")
        self.assertAskedToConfirm("3 bread R18 chimwe", notify_mock)

    def test_the_question_is_in_the_entrys_language(self):
        self.entry.language = "sn"
        self.entry.save()
        self._run_message("2 sugar R45 imwe")
        self.assertIn("ndiro zita reshop yako here?", self.buttons.await_args.args[1])

    def test_still_pending_response_once_company_name_already_set(self):
        self.entry.company_name = "Existing Shop"
        self.entry.save()
        send_response_mock, notify_mock = self._run_message("Hello")
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.company_name, "Existing Shop")
        notify_mock.assert_not_called()
        send_response_mock.assert_called_once()


class ShopNameConfirmationTest(TransactionTestCase):
    """#423: "Is *X* your shop's name?" with Yes / No. Invariant: the name is only ever set to
    text the owner sent, and only if it was clearly a name or the owner tapped Yes on the
    question about that exact text; the admin is alerted once, when it is set."""

    PHONE = "+263771000700"

    def setUp(self):
        self.entry = WaitlistEntry.objects.create(
            phone_number=self.PHONE, status=WaitlistEntry.Status.PENDING, language="en")
        self.notify = AsyncMock()
        self.send = AsyncMock()
        self.buttons = AsyncMock(return_value="wamid.buttons")

    def _patches(self):
        base = "apps.whatsapp.services.waitlist_handler."
        return (patch(base + "_send_response", new=self.send),
                patch(base + "_send_response_with_buttons", new=self.buttons),
                patch(base + "_send_waitlist_admin_notification", new=self.notify))

    def _message(self, text):
        a, b, c = self._patches()
        async def go():
            with a, b, c:
                entry = await sync_to_async(WaitlistEntry.objects.get)(id=self.entry.id)
                await process_waitlisted_message_async(self.PHONE, text, entry)
        _run(go())
        self.entry.refresh_from_db()

    def _tap(self, action, button_id=None, phone=None):
        button_id = button_id or self.buttons.await_args.args[2][0 if action == "yes" else 1]["id"]
        _, act, entry_id, tag = button_id.split("_", 3)
        a, b, c = self._patches()
        async def go():
            with a, b, c:
                await process_shop_name_button_async(act, int(entry_id), tag, phone or self.PHONE)
        _run(go())
        self.entry.refresh_from_db()

    def _last_text(self):
        return self.send.await_args.args[1]

    def test_names_with_numbers_and_chat_are_asked_about(self):
        for text in ("Shop 24", "Stand 1043 Tuckshop", "4x4 Auto Spares", "How are you doing?", "how are you",
                     "Ok", "Ndiri kuda kutengesa", "bread 2", "chingwa 1", "coke 2"):
            with self.subTest(text=text):
                WaitlistEntry.objects.filter(id=self.entry.id).update(company_name="", pending_name_candidate="")
                self.buttons.reset_mock()
                self._message(text)
                self.assertEqual(self.entry.company_name, "")
                self.assertEqual(self.entry.pending_name_candidate, text)
                self.buttons.assert_awaited_once()
        self.notify.assert_not_called()

    def test_the_buttons_carry_the_entry_and_a_tag_of_the_text(self):
        self._message("4x4 Auto Spares")
        ids = [b["id"] for b in self.buttons.await_args.args[2]]
        self.assertEqual(ids[0].rsplit("_", 1)[0], f"shopname_yes_{self.entry.id}")
        self.assertEqual(ids[1].rsplit("_", 1)[0], f"shopname_no_{self.entry.id}")
        self.assertTrue(all(len(b["title"]) <= 20 for b in self.buttons.await_args.args[2]))

    def test_yes_saves_that_text_and_alerts_the_admin_once(self):
        self._message("4x4 Auto Spares")
        self._tap("yes")
        self.assertEqual(self.entry.company_name, "4x4 Auto Spares")
        self.assertEqual(self.entry.pending_name_candidate, "")
        self.notify.assert_awaited_once()
        self.assertIn("4x4 Auto Spares", self._last_text())

    def test_a_second_yes_does_nothing_more(self):
        self._message("4x4 Auto Spares")
        yes_id = self.buttons.await_args.args[2][0]["id"]
        self._tap("yes", yes_id)
        self._tap("yes", yes_id)
        self.assertEqual(self.entry.company_name, "4x4 Auto Spares")
        self.notify.assert_awaited_once()

    def test_no_clears_the_question_and_asks_again(self):
        self._message("2 sugar R45 imwe")
        self._tap("no")
        self.assertEqual(self.entry.company_name, "")
        self.assertEqual(self.entry.pending_name_candidate, "")
        self.notify.assert_not_called()
        self.assertIn("What is your shop called?", self._last_text())

    def test_a_tap_from_another_number_does_nothing(self):
        self._message("4x4 Auto Spares")
        self._tap("yes", phone="+263771999999")
        self.assertEqual(self.entry.company_name, "")
        self.assertEqual(self.entry.pending_name_candidate, "4x4 Auto Spares")
        self.notify.assert_not_called()

    def test_yes_on_an_older_question_never_saves_the_newer_text(self):
        self._message("2 sugar R45 imwe")
        old_yes = self.buttons.await_args.args[2][0]["id"]
        self._message("Shop 24")
        self._tap("yes", old_yes)
        self.assertEqual(self.entry.company_name, "")
        self.assertEqual(self.entry.pending_name_candidate, "Shop 24")
        self.notify.assert_not_called()
        self.assertIn("expired", self._last_text())

    def test_typing_a_new_name_replaces_the_question_and_old_buttons_stop_working(self):
        self._message("2 sugar R45 imwe")
        old_yes = self.buttons.await_args.args[2][0]["id"]
        self._message("Mai Tendai's shop")
        self.assertEqual(self.entry.pending_name_candidate, "Mai Tendai's shop")
        self._tap("yes", old_yes)
        self.assertEqual(self.entry.company_name, "")
        self._tap("yes")
        self.assertEqual(self.entry.company_name, "Mai Tendai's shop")
        self.notify.assert_awaited_once()

    def test_the_candidate_changing_between_read_and_save_saves_nothing(self):
        """Another message replaces the candidate after this tap read the entry."""
        self._message("4x4 Auto Spares")
        yes_id = self.buttons.await_args.args[2][0]["id"]
        stale = WaitlistEntry.objects.get(id=self.entry.id)
        WaitlistEntry.objects.filter(id=self.entry.id).update(pending_name_candidate="2 sugar R45 imwe")
        with patch("apps.whatsapp.services.waitlist_handler._get_waitlist_entry", new=AsyncMock(return_value=stale)):
            self._tap("yes", yes_id)
        self.assertEqual(self.entry.company_name, "")
        self.notify.assert_not_called()

    def test_if_the_buttons_fail_the_question_is_dropped_and_asked_plainly(self):
        self.buttons = AsyncMock(return_value=None)
        self._message("4x4 Auto Spares")
        self.assertEqual(self.entry.pending_name_candidate, "")
        self.assertIn("What is your shop called?", self._last_text())

    def test_a_tap_after_approval_changes_nothing_and_says_nothing(self):
        self._message("4x4 Auto Spares")
        yes_id = self.buttons.await_args.args[2][0]["id"]
        self.send.reset_mock()
        WaitlistEntry.objects.filter(id=self.entry.id).update(status=WaitlistEntry.Status.APPROVED)
        self._tap("yes", yes_id)
        self.assertEqual(self.entry.company_name, "")
        self.notify.assert_not_called()
        self.send.assert_not_awaited()  # not "you are still on our waitlist"

    def test_a_tap_after_rejection_gets_the_rejected_reply(self):
        self._message("4x4 Auto Spares")
        yes_id = self.buttons.await_args.args[2][0]["id"]
        WaitlistEntry.objects.filter(id=self.entry.id).update(status=WaitlistEntry.Status.REJECTED)
        self._tap("yes", yes_id)
        self.assertEqual(self.entry.company_name, "")
        from apps.whatsapp.services.webhook_handler import t
        self.assertEqual(self._last_text(), t("waitlist.rejected", lang="en"))

    # --- #424 review ---

    def test_chat_emoji_and_punctuation_are_asked_about_not_saved(self):
        for text in ("Good morning", "Mangwanani", "Tatenda", "Sure", "Hello there", "👍", ".", "!!", "Ok thanks",
                     "Mai Ati shop?", "Mai Ati Mini Eatery and Takeaway Grocery Store"):
            with self.subTest(text=text):
                WaitlistEntry.objects.filter(id=self.entry.id).update(company_name="", pending_name_candidate="")
                self.buttons.reset_mock()
                self._message(text)
                self.assertEqual(self.entry.company_name, "")
                self.buttons.assert_awaited_once()
        self.notify.assert_not_called()

    def test_any_name_is_saved_with_one_yes(self):
        for name in ("Rimbi Store", "R&M General Dealer", "Mai Tendai's shop", "Coca-Cola Depot", "St. Mary's Tuckshop",
                     "Shop 24", "We sell groceries"):
            with self.subTest(name=name):
                WaitlistEntry.objects.filter(id=self.entry.id).update(company_name="", pending_name_candidate="")
                self.buttons.reset_mock()
                self.notify.reset_mock()
                self._message(name)
                self.assertEqual(self.entry.company_name, "")
                self._tap("yes")
                self.assertEqual(self.entry.company_name, name)
                self.notify.assert_awaited_once()

    def test_typing_yes_answers_the_open_question(self):
        for yes in ("Yes", "ehe", "Hongu!"):
            with self.subTest(yes=yes):
                WaitlistEntry.objects.filter(id=self.entry.id).update(company_name="", pending_name_candidate="")
                self._message("4x4 Auto Spares")
                self._message(yes)
                self.assertEqual(self.entry.company_name, "4x4 Auto Spares")

    def test_typing_no_answers_the_open_question(self):
        self._message("2 sugar R45 imwe")
        self._message("kwete")
        self.assertEqual(self.entry.company_name, "")
        self.assertEqual(self.entry.pending_name_candidate, "")
        self.assertIn("What is your shop called?", self._last_text())

    def test_typing_yes_with_no_open_question_is_not_saved_as_a_name(self):
        self._message("Yes")
        self.assertEqual(self.entry.company_name, "")
        self.assertEqual(self.entry.pending_name_candidate, "")
        self.buttons.assert_not_awaited()  # not "Is *Yes* your shop's name?"
        self.notify.assert_not_called()

    def test_two_yes_taps_racing_alert_the_admin_once(self):
        """Both taps read the entry before either saved: the conditional UPDATE lets only one set
        the name, so the admin is alerted once and the other tap gets the still-pending reply."""
        self._message("Rimbi Store")
        yes_id = self.buttons.await_args.args[2][0]["id"]
        stale = WaitlistEntry.objects.get(id=self.entry.id)
        with patch("apps.whatsapp.services.waitlist_handler._get_waitlist_entry", new=AsyncMock(return_value=stale)):
            self._tap("yes", yes_id)
            self._tap("yes", yes_id)
        self.assertEqual(self.entry.company_name, "Rimbi Store")
        self.notify.assert_awaited_once()
        self.assertIn("still on our waitlist", self._last_text())

    def test_the_save_never_overwrites_a_name_or_touches_a_decided_entry(self):
        from apps.whatsapp.services.waitlist_handler import _claim_waitlist_company_name
        WaitlistEntry.objects.filter(id=self.entry.id).update(company_name="Rimbi Store")
        self.assertFalse(_run(_claim_waitlist_company_name(self.entry.id, "Other")))
        WaitlistEntry.objects.filter(id=self.entry.id).update(company_name="", status=WaitlistEntry.Status.APPROVED)
        self.assertFalse(_run(_claim_waitlist_company_name(self.entry.id, "Other")))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.company_name, "")

    # --- #424 Codex review ---

    def _language_tap(self):
        a, b, c = self._patches()
        async def go():
            with a, b, c:
                await process_language_button_async("en", self.entry.id, self.PHONE)
        _run(go())

    def test_a_language_tap_after_the_admin_was_alerted_does_not_alert_again(self):
        self._message("Rimbi Store")
        self._tap("yes")  # saved and alerted
        WaitlistEntry.objects.filter(id=self.entry.id).update(admin_alerted_at=timezone.now())
        self._language_tap()
        self._language_tap()
        self.notify.assert_awaited_once()

    def test_a_language_tap_still_alerts_if_the_admin_was_never_told(self):
        WaitlistEntry.objects.filter(id=self.entry.id).update(company_name="Rimbi Store")
        self._language_tap()
        self.notify.assert_awaited_once()

    def test_buttons_that_raise_leave_nothing_to_confirm(self):
        """Meta answering 200 with no message id raises inside the client."""
        self.buttons = AsyncMock(side_effect=ValueError("no messages in response"))
        self._message("bread 2")
        self.assertEqual(self.entry.pending_name_candidate, "")
        self.assertIn("What is your shop called?", self._last_text())
        self.buttons = AsyncMock(return_value="wamid.buttons")
        self._message("yes")  # a question the owner never saw can't be confirmed
        self.assertEqual(self.entry.company_name, "")
        self.notify.assert_not_called()

    def test_no_question_is_sent_once_another_message_saved_the_name(self):
        stale = WaitlistEntry.objects.get(id=self.entry.id)
        WaitlistEntry.objects.filter(id=self.entry.id).update(company_name="Rimbi Store")
        a, b, c = self._patches()
        async def go():
            with a, b, c:
                await process_waitlisted_message_async(self.PHONE, "bread 2", stale)
        _run(go())
        self.entry.refresh_from_db()
        self.buttons.assert_not_awaited()
        self.assertEqual(self.entry.pending_name_candidate, "")
        self.assertIn("still on our waitlist", self._last_text())

    def test_shona_question_and_buttons(self):
        WaitlistEntry.objects.filter(id=self.entry.id).update(language="sn")
        self._message("Shop 24")
        self.assertIn("ndiro zita reshop yako here?", self.buttons.await_args.args[1])
        self.assertEqual([b["title"] for b in self.buttons.await_args.args[2]], ["Ehe, ndizvo", "Kwete"])


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
