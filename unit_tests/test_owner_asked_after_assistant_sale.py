"""When an assistant records a sale of a product with no cost, the owner is asked (#409, Brighton's
review): the owner is the one who knows what products cost. The assistant is never asked, and if the
owner is outside WhatsApp's 24 hour window nothing is sent."""

import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from django.db.models import F
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.models import Company
from apps.sales.models import Sale, SaleItem
from apps.whatsapp.models import WhatsAppMessage
from apps.whatsapp.services import cost_handler, sale_handler
from unit_tests.async_helpers import run_async_test
from unit_tests.test_owner_only_profit import ASSISTANT_PHONE, OWNER_PHONE, _Shop


class OwnerAskedAfterAssistantSaleTest(_Shop):
    def _inbound(self, phone, hours_ago):
        msg = WhatsAppMessage.objects.create(
            phone_number=phone, direction=WhatsAppMessage.Direction.INBOUND,
            message_type=WhatsAppMessage.MessageType.TEXT, company=self.company)
        WhatsAppMessage.objects.filter(pk=msg.pk).update(timestamp=timezone.now() - datetime.timedelta(hours=hours_ago))

    def _sale_by_assistant(self, *products, sid="sid_a"):
        sale = Sale.objects.create(company=self.company, confirmation_message_sid=sid)
        for p in products:
            SaleItem.objects.create(sale=sale, product=p, quantity=1, unit_price=Decimal("2.00"), currency="USD")
        return sale

    def _confirm(self, profile, sid="sid_a", template_delivered=True, text_delivered=True):
        """Confirm the sale as `profile`. Templates go to `self.template` (#416)."""
        to_assistant, to_owner = AsyncMock(), AsyncMock(return_value=text_delivered)
        client = MagicMock()
        if isinstance(template_delivered, list):  # one result per owner, in order
            client.send_template_message = AsyncMock(side_effect=template_delivered)
        else:
            client.send_template_message = AsyncMock(return_value=template_delivered)
        self.template = client.send_template_message
        with patch.object(sale_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)), \
             patch.object(sale_handler, "_send_response", new=to_assistant), \
             patch.object(sale_handler, "_send_response_with_buttons", new=AsyncMock()), \
             patch.object(sale_handler, "_company_needs_closing_time", new=AsyncMock(return_value=False)), \
             patch.object(cost_handler, "_send_response", new=to_owner), \
             patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=client):
            run_async_test(sale_handler.process_sale_button_action_async("confirm", profile.phone_number, sid))
        return to_assistant, to_owner

    def _pending(self):
        return Company.objects.get(id=self.company.id).pending_cost_product_ids

    def test_owner_in_the_window_gets_the_question(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)

        _, to_owner = self._confirm(self.assistant)

        to_owner.assert_awaited_once()
        phone, text = to_owner.await_args.args
        self.assertEqual(phone, OWNER_PHONE)
        self.assertIn("Your assistant recorded a sale", text)
        self.assertIn("one *Bread*", text)
        self.assertEqual(self._pending(), [self.bread.id])
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost_ask_count, 1)

    def test_the_assistant_is_never_asked_and_gets_the_plain_confirmation(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)

        to_assistant, to_owner = self._confirm(self.assistant)

        self.assertEqual(to_assistant.await_args.args[1], "✓ Great, sale recorded!")
        self.assertNotIn(ASSISTANT_PHONE, [c.args[0] for c in to_owner.await_args_list])

    def test_the_sale_is_still_confirmed(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        sale = self._sale_by_assistant(self.bread)

        self._confirm(self.assistant)

        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.CONFIRMED)

    def test_owner_outside_the_window_gets_no_free_form_message(self):
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)

        _, to_owner = self._confirm(self.assistant)

        to_owner.assert_not_awaited()  # outside the window only a template can reach them (#416)

    def test_owner_who_never_messaged_gets_no_free_form_message(self):
        self._sale_by_assistant(self.bread)

        _, to_owner = self._confirm(self.assistant)

        to_owner.assert_not_awaited()

    def test_a_second_assistant_sale_does_not_pile_on_while_a_question_is_open(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread, sid="sid_1")
        self._confirm(self.assistant, sid="sid_1")
        self._sale_by_assistant(self.eggs, sid="sid_2")

        _, to_owner = self._confirm(self.assistant, sid="sid_2")

        to_owner.assert_not_awaited()
        self.assertEqual(self._pending(), [self.bread.id])

    def test_a_product_that_already_has_a_cost_is_not_asked_about(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        Product.objects.filter(id=self.bread.id).update(cost=Decimal("0.80"), cost_currency="USD")
        self._sale_by_assistant(self.bread)

        _, to_owner = self._confirm(self.assistant)

        to_owner.assert_not_awaited()

    def test_a_product_already_asked_twice_is_left_alone(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        Product.objects.filter(id=self.bread.id).update(cost_ask_count=2)
        self._sale_by_assistant(self.bread)

        _, to_owner = self._confirm(self.assistant)

        to_owner.assert_not_awaited()

    def test_several_products_are_asked_in_one_message(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread, self.eggs)

        _, to_owner = self._confirm(self.assistant)

        to_owner.assert_awaited_once()
        text = to_owner.await_args.args[1]
        self.assertIn("• Bread", text)
        self.assertIn("• Eggs", text)
        self.assertCountEqual(self._pending(), [self.bread.id, self.eggs.id])

    def test_the_question_is_in_the_owners_language(self):
        UserProfile = type(self.owner)
        UserProfile.objects.filter(pk=self.owner.pk).update(language="sn")
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)

        _, to_owner = self._confirm(self.assistant)

        self.assertIn("Mutengesi wako arekodha sale", to_owner.await_args.args[1])

    def test_the_owner_can_answer_it(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)
        self._confirm(self.assistant)
        company = Company.objects.get(id=self.company.id)
        send = AsyncMock()

        with patch.object(cost_handler, "_send_response", new=send):
            handled = run_async_test(cost_handler.handle_pending_cost_reply(
                OWNER_PHONE, company, "bread 0.80", "en", user_profile=self.owner))

        self.assertTrue(handled)
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.80"))

    def test_an_assistant_cannot_answer_it(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)
        self._confirm(self.assistant)
        company = Company.objects.get(id=self.company.id)

        handled = run_async_test(cost_handler.handle_pending_cost_reply(
            ASSISTANT_PHONE, company, "bread 0.80", "en", user_profile=self.assistant))

        self.assertFalse(handled)
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)

    def test_a_failure_asking_the_owner_never_blocks_the_sale(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        sale = self._sale_by_assistant(self.bread)

        with patch.object(cost_handler, "ask_owner_after_assistant_sale", new=AsyncMock(side_effect=RuntimeError("boom"))):
            to_assistant, _ = self._confirm(self.assistant)

        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.CONFIRMED)
        self.assertEqual(to_assistant.await_args.args[1], "✓ Great, sale recorded!")

    # --- Outside the 24 hour window: the approved template (#416) ---

    def test_owner_outside_the_window_gets_the_template(self):
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)

        self._confirm(self.assistant)

        self.template.assert_awaited_once()
        phone, name = self.template.await_args.args
        self.assertEqual(phone, OWNER_PHONE)
        self.assertEqual(name, "ats_cost_question")
        params = self.template.await_args.kwargs["components"][0]["parameters"]
        self.assertEqual([p["text"] for p in params], ["Bread", "Bread 1.00"])
        self.assertEqual(self._pending(), [self.bread.id])
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost_ask_count, 1)

    def test_owner_who_never_messaged_gets_the_template(self):
        self._sale_by_assistant(self.bread)

        self._confirm(self.assistant)

        self.template.assert_awaited_once()
        self.assertEqual(self._pending(), [self.bread.id])

    def test_the_template_lists_every_product(self):
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread, self.eggs)

        self._confirm(self.assistant)

        params = self.template.await_args.kwargs["components"][0]["parameters"]
        self.assertEqual(params[0]["text"], "Bread, Eggs")
        self.assertEqual(params[1]["text"], "Bread 1.00")
        self.assertCountEqual(self._pending(), [self.bread.id, self.eggs.id])

    def test_a_shona_owner_gets_the_shona_template(self):
        type(self.owner).objects.filter(pk=self.owner.pk).update(language="sn")
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)

        self._confirm(self.assistant)

        self.assertEqual(self.template.await_args.args[1], "ats_cost_question_shona_version")

    def test_an_owner_in_the_window_gets_no_template(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)

        self._confirm(self.assistant)

        self.template.assert_not_awaited()

    def test_a_failed_template_send_opens_no_question_and_uses_no_ask(self):
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)

        self._confirm(self.assistant, template_delivered=False)

        self.template.assert_awaited_once()
        self.assertEqual(self._pending(), [])
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost_ask_count, 0)  # not used up, so a later sale can ask again

    def test_the_owner_can_answer_the_template(self):
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)
        self._confirm(self.assistant)
        company = Company.objects.get(id=self.company.id)

        with patch.object(cost_handler, "_send_response", new=AsyncMock()):
            handled = run_async_test(cost_handler.handle_pending_cost_reply(
                OWNER_PHONE, company, "bread 1.00", "en", user_profile=self.owner))

        self.assertTrue(handled)
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("1.00"))

    def test_the_template_does_not_pile_on_an_open_question(self):
        self._sale_by_assistant(self.bread, sid="sid_1")
        self._confirm(self.assistant, sid="sid_1")
        self._sale_by_assistant(self.eggs, sid="sid_2")

        self._confirm(self.assistant, sid="sid_2")

        self.template.assert_not_awaited()
        self.assertEqual(self._pending(), [self.bread.id])

    def _reply_after(self, hours, text="Bread 1.00"):
        """The owner answers `hours` after the question was opened."""
        Company.objects.filter(id=self.company.id).update(
            pending_cost_asked_at=F("pending_cost_asked_at") - datetime.timedelta(hours=hours),
            pending_cost_expires_at=F("pending_cost_expires_at") - datetime.timedelta(hours=hours),
        )
        company = Company.objects.get(id=self.company.id)
        with patch.object(cost_handler, "_send_response", new=AsyncMock()):
            return run_async_test(cost_handler.handle_pending_cost_reply(
                OWNER_PHONE, company, text, "en", user_profile=self.owner))

    def test_a_late_answer_to_the_template_is_still_a_cost(self):
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)
        self._confirm(self.assistant)

        handled = self._reply_after(hours=6)  # past the 4 hours an in-chat question lasts

        self.assertTrue(handled)
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("1.00"))

    def test_a_late_reply_with_only_a_currency_is_a_sale_not_a_cost(self):
        """#417 review: after the usual 4 hours the owner has probably moved on, and
        "bread $1" is more likely a sale. A sale draft they can fix beats a silent cost."""
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)
        self._confirm(self.assistant)

        handled = self._reply_after(hours=6, text="bread $1")

        self.assertFalse(handled)
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(self._pending(), [])  # closed, and the message goes on to sale parsing

    def test_a_late_reply_with_a_decimal_and_a_currency_is_still_a_cost(self):
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)
        self._confirm(self.assistant)

        handled = self._reply_after(hours=6, text="bread $1.00")

        self.assertTrue(handled)
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("1.00"))

    def test_an_early_reply_with_only_a_currency_is_still_a_cost(self):
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)
        self._confirm(self.assistant)

        handled = self._reply_after(hours=1, text="bread $1")

        self.assertTrue(handled)
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("1"))

    def test_the_template_question_expires_after_a_day(self):
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)
        self._confirm(self.assistant)

        handled = self._reply_after(hours=25)

        self.assertFalse(handled)
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)

    def test_an_in_window_question_keeps_the_short_expiry(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)
        self._confirm(self.assistant)

        self.assertIsNone(Company.objects.get(id=self.company.id).pending_cost_expires_at)

    def test_the_question_is_open_while_the_template_is_being_sent(self):
        """A second sale confirmed during the send must see the open question, not ask again."""
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)
        seen = []

        async def check_pending(*args, **kwargs):
            seen.append(await cost_handler._get_pending_products(self.company.id))
            return True

        client = MagicMock()
        client.send_template_message = AsyncMock(side_effect=check_pending)
        with patch.object(sale_handler, "_get_profile_by_phone", new=AsyncMock(return_value=self.assistant)), \
             patch.object(sale_handler, "_send_response", new=AsyncMock()), \
             patch.object(sale_handler, "_send_response_with_buttons", new=AsyncMock()), \
             patch.object(sale_handler, "_company_needs_closing_time", new=AsyncMock(return_value=False)), \
             patch.object(cost_handler, "_send_response", new=AsyncMock()), \
             patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=client):
            run_async_test(sale_handler.process_sale_button_action_async("confirm", ASSISTANT_PHONE, "sid_a"))

        self.assertEqual(seen, [{self.bread.id: "Bread"}])

    def _second_owner(self, phone="+263771000003"):
        from django.contrib.auth.models import User
        UserProfile = type(self.owner)
        return UserProfile.objects.create(
            user=User.objects.create_user(username=phone), company=self.company,
            phone_number=phone, role=UserProfile.Role.OWNER, language="en")

    def test_two_owners_one_in_the_window_and_one_out(self):
        second = self._second_owner()
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)

        _, to_owner = self._confirm(self.assistant, template_delivered=False)

        self.assertEqual(to_owner.await_args.args[0], OWNER_PHONE)
        self.assertEqual(self.template.await_args.args[0], second.phone_number)
        # The in-window owner was reached, so the question stays open and the ask counts.
        self.assertEqual(self._pending(), [self.bread.id])
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost_ask_count, 1)
        # No template got through, so it keeps the usual expiry, not 24 hours.
        self.assertIsNone(Company.objects.get(id=self.company.id).pending_cost_expires_at)

    def test_two_owners_text_and_template_both_accepted_get_the_long_expiry(self):
        self._second_owner()
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)

        self._confirm(self.assistant)

        self.assertIsNotNone(Company.objects.get(id=self.company.id).pending_cost_expires_at)

    def test_a_rejected_normal_message_opens_no_question_and_uses_no_ask(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)

        _, to_owner = self._confirm(self.assistant, text_delivered=False)

        to_owner.assert_awaited_once()
        self.assertEqual(self._pending(), [])
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost_ask_count, 0)

    def test_a_question_opened_by_another_sale_in_the_meantime_wins(self):
        """Two assistants confirm at once: both pass the 'nothing open' check, only one claims it."""
        self._sale_by_assistant(self.bread)
        Company.objects.filter(id=self.company.id).update(
            pending_cost_product_ids=[self.eggs.id], pending_cost_asked_at=timezone.now())

        with patch.object(cost_handler, "_get_pending_products", new=AsyncMock(return_value={})):
            self._confirm(self.assistant)

        self.template.assert_not_awaited()
        self.assertEqual(self._pending(), [self.eggs.id])
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost_ask_count, 0)

    def test_taking_back_a_failed_question_never_clears_another_sales_question(self):
        self._sale_by_assistant(self.bread)
        other_asked_at = timezone.now() + datetime.timedelta(seconds=1)

        async def another_sale_claims_it_then_ours_fails(*args, **kwargs):
            # Another sale's question replaced ours while our template was being sent.
            from asgiref.sync import sync_to_async
            await sync_to_async(Company.objects.filter(id=self.company.id).update)(
                pending_cost_product_ids=[self.eggs.id], pending_cost_asked_at=other_asked_at)
            return False

        client = MagicMock()
        client.send_template_message = AsyncMock(side_effect=another_sale_claims_it_then_ours_fails)
        with patch.object(sale_handler, "_get_profile_by_phone", new=AsyncMock(return_value=self.assistant)), \
             patch.object(sale_handler, "_send_response", new=AsyncMock()), \
             patch.object(sale_handler, "_send_response_with_buttons", new=AsyncMock()), \
             patch.object(sale_handler, "_company_needs_closing_time", new=AsyncMock(return_value=False)), \
             patch.object(cost_handler, "_send_response", new=AsyncMock()), \
             patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=client):
            run_async_test(sale_handler.process_sale_button_action_async("confirm", ASSISTANT_PHONE, "sid_a"))

        self.assertEqual(self._pending(), [self.eggs.id])

    def test_two_owners_outside_the_window_one_template_accepted(self):
        self._second_owner()
        self._sale_by_assistant(self.bread)

        self._confirm(self.assistant, template_delivered=[False, True])

        self.assertEqual(self.template.await_count, 2)
        self.assertEqual(self._pending(), [self.bread.id])
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost_ask_count, 1)

    def test_two_owners_outside_the_window_neither_template_accepted(self):
        self._second_owner()
        self._sale_by_assistant(self.bread)

        self._confirm(self.assistant, template_delivered=[False, False])

        self.assertEqual(self.template.await_count, 2)
        self.assertEqual(self._pending(), [])
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost_ask_count, 0)

    def test_the_owner_confirming_their_own_sale_is_unchanged(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)

        to_owner_reply, other = self._confirm(self.owner)

        self.assertIn("how much do you pay", to_owner_reply.await_args.args[1])  # merged message, as before
        other.assert_not_awaited()
