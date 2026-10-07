"""Profit and cost prices are owner-only (#409, Brighton's call): what a shop pays
for stock is the owner's private information, so assistants can't set costs or see
profit, and are never asked for a cost."""

import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.models import Company, UserProfile
from apps.sales.models import Sale, SaleItem
from apps.whatsapp.services import business_reports, cost_handler, sale_handler, webhook_handler
from apps.whatsapp.services.business_reports import BusinessSnapshot, format_business_summary, format_period_summary
from apps.whatsapp.services.message_parser import ParsedCostItem
from unit_tests.async_helpers import run_async_test

OWNER_PHONE = "+263771000001"
ASSISTANT_PHONE = "+263771000002"


class _Shop(TransactionTestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Staff Shop", slug="staff-shop", active=True, daily_summary_enabled=True,
            first_message_date=datetime.date.today() - datetime.timedelta(days=5),
        )
        self.owner = self._profile(OWNER_PHONE, UserProfile.Role.OWNER)
        self.assistant = self._profile(ASSISTANT_PHONE, UserProfile.Role.ASSISTANT)
        self.bread = Product.objects.create(company=self.company, name="Bread")
        self.eggs = Product.objects.create(company=self.company, name="Eggs")

    def _profile(self, phone, role):
        user = User.objects.create_user(username=phone)
        return UserProfile.objects.create(
            user=user, company=self.company, phone_number=phone, role=role, language="en")

    def _sale(self, *lines, status=Sale.Status.CONFIRMED):
        sale = Sale.objects.create(company=self.company, status=status)
        for product, qty, price, cost in lines:
            SaleItem.objects.create(
                sale=sale, product=product, quantity=qty, unit_price=Decimal(price), currency="USD",
                unit_cost=None if cost is None else Decimal(cost), cost_currency=None if cost is None else "USD")
        return sale


class DailySummaryPerRecipientTest(_Shop):
    def _send(self):
        client = MagicMock()
        client.send_message = AsyncMock(return_value=True)
        client.send_image = AsyncMock(return_value=True)
        client.send_template_message = AsyncMock(return_value=True)

        async def go():
            with patch.object(business_reports, "get_whatsapp_client", return_value=client), \
                 patch.object(business_reports.settings, "USE_TEMPLATE_MESSAGES", False), \
                 patch("apps.whatsapp.services.report_card.generate_stat_card", side_effect=RuntimeError("no card")):
                return await business_reports.send_daily_summary(self.company)

        run_async_test(go())
        return {c.args[0]: c.args[1] for c in client.send_message.await_args_list}

    def test_owner_gets_profit_and_assistant_does_not(self):
        self._sale((self.bread, 2, "2.00", "1.00"), (self.eggs, 3, "0.50", "0.10"))

        sent = self._send()

        self.assertCountEqual(sent, [OWNER_PHONE, ASSISTANT_PHONE])
        self.assertIn("Profit: $", sent[OWNER_PHONE])
        self.assertIn("Most profitable:", sent[OWNER_PHONE])
        for leak in ("Profit", "profit", "Most profitable", "cost"):
            self.assertNotIn(leak, sent[ASSISTANT_PHONE], leak)
        self.assertIn("$5.50", sent[ASSISTANT_PHONE])  # the sales part is still there

    def test_assistant_does_not_get_the_add_a_cost_prompt_either(self):
        self._sale((self.bread, 2, "2.00", None))

        sent = self._send()

        self.assertIn("e.g. *Bread costs 0.80*", sent[OWNER_PHONE])
        self.assertNotIn("costs 0.80", sent[ASSISTANT_PHONE])
        self.assertNotIn("No cost yet", sent[ASSISTANT_PHONE])

    def test_assistant_summary_equals_the_summary_from_before_profit_existed(self):
        snapshot = BusinessSnapshot(
            company_id=1, report_date=datetime.date(2026, 10, 6), currency="USD", sales_count=2, items_sold=3,
            revenue=Decimal("5.00"), currency_revenues={"USD": Decimal("5.00")}, top_products=[("Bread", 2)],
            low_stock_items=[], out_of_stock_items=[], currency_profits={"USD": Decimal("2.00")},
            uncosted_lines=1, profit_products={"USD": [("Bread", Decimal("2.00")), ("Eggs", Decimal("1.00"))]},
            uncosted_products=["Coke"], mismatched_currency_lines=1)
        bare = BusinessSnapshot(
            company_id=1, report_date=datetime.date(2026, 10, 6), currency="USD", sales_count=2, items_sold=3,
            revenue=Decimal("5.00"), currency_revenues={"USD": Decimal("5.00")}, top_products=[("Bread", 2)],
            low_stock_items=[], out_of_stock_items=[])

        self.assertEqual(format_business_summary(snapshot, lang="en", show_profit=False),
                         format_business_summary(bare, lang="en"))

    def test_a_shop_with_only_an_owner_still_gets_the_profit_message(self):
        UserProfile.objects.filter(pk=self.assistant.pk).delete()
        self._sale((self.bread, 2, "2.00", "1.00"))

        sent = self._send()

        self.assertEqual(list(sent), [OWNER_PHONE])
        self.assertIn("Profit: $2.00", sent[OWNER_PHONE])


class SalesQueryRoleTest(_Shop):
    def setUp(self):
        super().setUp()
        self._sale((self.bread, 2, "2.00", "1.00"), (self.eggs, 3, "0.50", "0.10"))

    def _ask(self, profile, **fields):
        result = SimpleNamespace(**{"timeframe": "week", "period_count": None, "metric": "revenue", **fields})
        send = AsyncMock()
        with patch.object(webhook_handler, "_send_response", new=send):
            run_async_test(webhook_handler._handle_sales_query("+263770000000", profile, result, lang="en"))
        return send

    def test_assistant_asking_for_profit_gets_an_owner_only_reply(self):
        send = self._ask(self.assistant, metric="profit")

        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[1], "Only the shop owner can see profit.")

    def test_assistant_asking_about_sales_gets_revenue_without_any_profit(self):
        send = self._ask(self.assistant, metric="revenue")

        text = send.await_args.args[1]
        self.assertIn("$5.50", text)
        for leak in ("Profit", "profit", "Most profitable", "No cost"):
            self.assertNotIn(leak, text, leak)

    def test_assistant_asking_about_today_gets_no_profit_either(self):
        send = self._ask(self.assistant, timeframe="today", metric="revenue")

        for call in send.await_args_list:
            self.assertNotIn("rofit", str(call.args[1]))

    def test_owner_still_gets_profit(self):
        send = self._ask(self.owner, metric="profit")

        self.assertIn("Profit: $", send.await_args.args[1])

    def test_assistant_cannot_get_profit_with_a_rolling_window_either(self):
        send = self._ask(self.assistant, timeframe="days", period_count=2, metric="profit")

        self.assertEqual(send.await_args.args[1], "Only the shop owner can see profit.")


class SetCostRoleTest(_Shop):
    def _set(self, profile):
        company = Company.objects.get(id=self.company.id)
        result = SimpleNamespace(cost_items=[ParsedCostItem("bread", Decimal("0.80"), "USD")])
        send, send_buttons = AsyncMock(), AsyncMock()
        with patch.object(cost_handler, "_send_response", new=send), \
             patch.object(cost_handler, "_send_response_with_buttons", new=send_buttons):
            run_async_test(cost_handler.handle_set_cost_intent("+263770000000", company, result, "en", user_profile=profile))
        return send

    def test_assistant_cannot_set_a_cost(self):
        send = self._set(self.assistant)

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(send.await_args.args[1], "Only the shop owner can set product costs.")

    def test_owner_can(self):
        self._set(self.owner)

        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.80"))

    def test_a_missing_profile_is_treated_as_not_the_owner(self):
        self._set(None)

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)

    def test_assistant_cannot_use_the_confirm_button(self):
        send = AsyncMock()
        with patch.object(cost_handler, "_get_profile_by_phone", new=AsyncMock(return_value=self.assistant)), \
             patch.object(cost_handler, "_send_response", new=send):
            run_async_test(cost_handler._process_cost_confirm_async(
                ASSISTANT_PHONE, f"costyes_{self.bread.id}_USD_8_none"))

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(send.await_args.args[1], "Only the shop owner can set product costs.")

    def test_assistant_message_is_not_read_as_the_answer_and_the_question_stays_open(self):
        Company.objects.filter(id=self.company.id).update(
            pending_cost_product_ids=[self.bread.id], pending_cost_asked_at=timezone.now())
        company = Company.objects.get(id=self.company.id)

        handled = run_async_test(cost_handler.handle_pending_cost_reply(
            ASSISTANT_PHONE, company, "bread 0.80", "en", user_profile=self.assistant))

        self.assertFalse(handled)
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [self.bread.id])


class CostQuestionRoleTest(_Shop):
    def _tap_confirm(self, profile):
        sale = Sale.objects.create(company=self.company, confirmation_message_sid="sid_role")
        SaleItem.objects.create(sale=sale, product=self.bread, quantity=1, unit_price=Decimal("2.00"), currency="USD")
        send = AsyncMock()
        with patch.object(sale_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)), \
             patch.object(sale_handler, "_send_response", new=send), \
             patch.object(sale_handler, "_send_response_with_buttons", new=AsyncMock()), \
             patch.object(sale_handler, "_company_needs_closing_time", new=AsyncMock(return_value=False)):
            run_async_test(sale_handler.process_sale_button_action_async("confirm", "+263770000000", "sid_role"))
        return send

    def test_assistant_confirming_a_sale_is_not_asked_for_a_cost(self):
        send = self._tap_confirm(self.assistant)

        self.assertEqual(send.await_args.args[1], "✓ Great, sale recorded!")
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [])
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost_ask_count, 0)

    def test_the_sale_itself_is_still_confirmed_for_an_assistant(self):
        self._tap_confirm(self.assistant)

        self.assertEqual(Sale.objects.get(confirmation_message_sid="sid_role").status, Sale.Status.CONFIRMED)

    def test_owner_confirming_a_sale_is_asked(self):
        send = self._tap_confirm(self.owner)

        self.assertIn("how much do you pay", send.await_args.args[1])


class IncomingMessageWiringTest(_Shop):
    """The handlers above are unit-tested directly; these go through the real incoming-message
    entry point, so a dropped argument (the role, the closing-prompt flag) can't hide."""

    def _incoming(self, profile, text, parse_result=None):
        parse = AsyncMock(return_value=parse_result or SimpleNamespace(
            intent="other", confidence=1.0, notes=None, other_sub_intent="unknown"))
        send = AsyncMock()
        fresh = UserProfile.objects.select_related("company").get(pk=profile.pk)
        with patch.object(webhook_handler, "parse_message_unified", new=parse), \
             patch.object(webhook_handler, "_send_response", new=send), \
             patch.object(cost_handler, "_send_response", new=send), \
             patch.object(webhook_handler.settings, "ENABLE_WHATSAPP_TYPING", False):
            run_async_test(webhook_handler._process_message_async("wamid.w1", profile.phone_number, text, fresh))
        return parse, send

    def _open_question(self):
        Company.objects.filter(id=self.company.id).update(
            pending_cost_product_ids=[self.bread.id], pending_cost_asked_at=timezone.now())

    def test_owner_reply_is_saved_as_a_cost_without_calling_the_llm(self):
        self._open_question()

        parse, send = self._incoming(self.owner, "bread 0.80")

        parse.assert_not_awaited()
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.80"))
        self.assertIn("Cost saved", send.await_args.args[1])

    def test_assistant_message_goes_to_normal_parsing_and_saves_no_cost(self):
        self._open_question()

        parse, _ = self._incoming(self.assistant, "bread 0.80")

        parse.assert_awaited_once()
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [self.bread.id])

    def test_a_bare_number_answers_the_closing_prompt_not_the_cost_question(self):
        self._open_question()
        Company.objects.filter(id=self.company.id).update(last_closing_prompt_date=timezone.localdate())
        closing = AsyncMock(return_value=datetime.time(6, 0))
        set_closing = AsyncMock()

        with patch.object(business_reports, "parse_closing_time_llm", new=closing), \
             patch.object(business_reports, "set_company_daily_closing_time", new=set_closing):
            parse, _ = self._incoming(self.owner, "6")

        set_closing.assert_awaited_once()
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)

    def test_set_cost_intent_from_an_assistant_is_refused_through_the_entry_point(self):
        result = SimpleNamespace(intent="set_cost", confidence=1.0, notes=None,
                                 cost_items=[ParsedCostItem("bread", Decimal("0.80"), "USD")])

        _, send = self._incoming(self.assistant, "bread costs me 0.80", parse_result=result)

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(send.await_args.args[1], "Only the shop owner can set product costs.")

    def test_set_cost_intent_from_the_owner_is_saved_through_the_entry_point(self):
        result = SimpleNamespace(intent="set_cost", confidence=1.0, notes=None,
                                 cost_items=[ParsedCostItem("bread", Decimal("0.80"), "USD")])

        self._incoming(self.owner, "bread costs me 0.80", parse_result=result)

        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.80"))


class VoiceRoleTest(_Shop):
    """Voice goes through media_handler, a separate path from text. The fresh review of the
    owner-only commit found nothing exercised an assistant here."""

    def _speak(self, profile, transcript, parse_result=None):
        from django.test import override_settings
        from apps.whatsapp.services import media_handler

        fresh = UserProfile.objects.select_related("company").get(pk=profile.pk)
        client = SimpleNamespace(download_media=AsyncMock(return_value=(b"audio", "audio/ogg")))
        parse = AsyncMock(return_value=parse_result or SimpleNamespace(
            intent="other", confidence=1.0, notes=None, other_sub_intent="unknown"))
        send = AsyncMock()
        sale = AsyncMock()
        with override_settings(ELEVENLABS_API_KEY="test"), \
             patch.object(media_handler, "get_whatsapp_client", return_value=client), \
             patch.object(media_handler, "_upload_to_r2", new=AsyncMock(return_value=None)), \
             patch("services.elevenlabs.ElevenLabsClient") as eleven, \
             patch("apps.whatsapp.services.message_parser._get_active_products", new=AsyncMock(return_value=[])), \
             patch("apps.whatsapp.services.message_parser.parse_message_unified", new=parse), \
             patch("apps.whatsapp.services.sale_handler.process_sale_message_unified", new=sale), \
             patch.object(cost_handler, "_send_response", new=send), \
             patch.object(media_handler, "_send_response", new=send):
            eleven.return_value.transcribe_audio = AsyncMock(return_value=transcript)
            run_async_test(media_handler.process_audio_message_async("wamid.v2", profile.phone_number, "m1", fresh))
        return parse, send

    def _open_question(self):
        Company.objects.filter(id=self.company.id).update(
            pending_cost_product_ids=[self.bread.id], pending_cost_asked_at=timezone.now())

    def _set_cost_result(self):
        return SimpleNamespace(intent="set_cost", confidence=1.0, notes=None,
                               cost_items=[ParsedCostItem("bread", Decimal("0.80"), "USD")])

    def test_assistant_speaking_a_cost_reply_saves_nothing_and_leaves_the_question_open(self):
        self._open_question()

        parse, _ = self._speak(self.assistant, "bread 0.80")

        parse.assert_awaited_once()  # carried on to normal parsing, never read as the answer
        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(Company.objects.get(id=self.company.id).pending_cost_product_ids, [self.bread.id])

    def test_owner_speaking_a_cost_reply_is_saved(self):
        self._open_question()

        parse, _ = self._speak(self.owner, "bread 0.80")

        parse.assert_not_awaited()
        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.80"))

    def test_assistant_voice_cost_statement_is_refused(self):
        _, send = self._speak(self.assistant, "bread costs me 0.80", parse_result=self._set_cost_result())

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertEqual(send.await_args.args[1], "Only the shop owner can set product costs.")

    def test_owner_voice_cost_statement_is_saved(self):
        self._speak(self.owner, "bread costs me 0.80", parse_result=self._set_cost_result())

        self.bread.refresh_from_db()
        self.assertEqual(self.bread.cost, Decimal("0.80"))


class ButtonRoleTest(_Shop):
    def _tap(self, profile, button_id):
        send = AsyncMock()
        with patch.object(cost_handler, "_get_profile_by_phone", new=AsyncMock(return_value=profile)), \
             patch.object(cost_handler, "_send_response", new=send):
            run_async_test(cost_handler._process_cost_confirm_async(profile.phone_number, button_id))
        return send

    def test_assistant_tapping_no_gets_the_owner_only_reply_too(self):
        send = self._tap(self.assistant, "costno")

        self.assertEqual(send.await_args.args[1], "Only the shop owner can set product costs.")

    def test_owner_tapping_no_saves_nothing(self):
        send = self._tap(self.owner, "costno")

        self.bread.refresh_from_db()
        self.assertIsNone(self.bread.cost)
        self.assertIn("not saved", send.await_args.args[1])


class ShopWithNoOwnerTest(_Shop):
    def test_a_shop_with_only_assistants_sends_nobody_profit(self):
        UserProfile.objects.filter(pk=self.owner.pk).delete()
        self._sale((self.bread, 2, "2.00", "1.00"))
        client = MagicMock()
        client.send_message = AsyncMock(return_value=True)
        client.send_image = AsyncMock(return_value=True)

        async def go():
            with patch.object(business_reports, "get_whatsapp_client", return_value=client), \
                 patch.object(business_reports.settings, "USE_TEMPLATE_MESSAGES", False), \
                 patch("apps.whatsapp.services.report_card.generate_stat_card", side_effect=RuntimeError("no card")):
                return await business_reports.send_daily_summary(self.company)

        run_async_test(go())

        texts = [c.args[1] for c in client.send_message.await_args_list]
        self.assertEqual(len(texts), 1)
        self.assertNotIn("rofit", texts[0])
