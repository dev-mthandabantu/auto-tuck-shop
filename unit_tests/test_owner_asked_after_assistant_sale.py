"""When an assistant records a sale of a product with no cost, the owner is asked (#409, Brighton's
review): the owner is the one who knows what products cost. The assistant is never asked, and if the
owner is outside WhatsApp's 24 hour window nothing is sent."""

import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

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

    def _confirm(self, profile, sid="sid_a"):
        to_assistant, to_owner = AsyncMock(), AsyncMock()
        with patch.object(sale_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)), \
             patch.object(sale_handler, "_send_response", new=to_assistant), \
             patch.object(sale_handler, "_send_response_with_buttons", new=AsyncMock()), \
             patch.object(sale_handler, "_company_needs_closing_time", new=AsyncMock(return_value=False)), \
             patch.object(cost_handler, "_send_response", new=to_owner):
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

    def test_owner_outside_the_window_is_not_messaged_and_no_question_is_opened(self):
        self._inbound(OWNER_PHONE, hours_ago=30)
        self._sale_by_assistant(self.bread)

        _, to_owner = self._confirm(self.assistant)

        to_owner.assert_not_awaited()
        self.assertEqual(self._pending(), [])
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost_ask_count, 0)  # not used up, so a later sale can ask again

    def test_owner_who_never_messaged_is_not_messaged(self):
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

    def test_the_owner_confirming_their_own_sale_is_unchanged(self):
        self._inbound(OWNER_PHONE, hours_ago=2)
        self._sale_by_assistant(self.bread)

        to_owner_reply, other = self._confirm(self.owner)

        self.assertIn("how much do you pay", to_owner_reply.await_args.args[1])  # merged message, as before
        other.assert_not_awaited()
