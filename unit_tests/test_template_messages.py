"""Tests for template message wiring — send_daily_summary and approval_welcome."""

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase

from apps.core.models import Company, UserProfile


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_client(send_template_mock=None, send_image_mock=None, send_message_mock=None):
    client = MagicMock()
    client.send_template_message = send_template_mock or AsyncMock(return_value=True)
    client.send_image = send_image_mock or AsyncMock(return_value=True)
    client.send_message = send_message_mock or AsyncMock(return_value=True)
    return client


def _make_company(name="Test Shop"):
    return Company.objects.create(
        name=name,
        slug=name.lower().replace(" ", "-"),
        active=True,
        daily_summary_enabled=True,
        first_message_date=date.today() - timedelta(days=5),
    )


def _make_owner(company, phone):
    user = User.objects.create_user(username=phone)
    return UserProfile.objects.create(
        user=user, company=company, phone_number=phone, role=UserProfile.Role.OWNER,
    )


class SendDailySummaryTemplateTest(TransactionTestCase):

    def setUp(self):
        self.company = _make_company()
        self.owner = _make_owner(self.company, "+263771001001")

    def _make_sale(self, product_name="Mazoe", qty=2, price=Decimal("1.00")):
        from apps.sales.models import Sale, SaleItem
        from apps.catalog.models import Product
        product, _ = Product.objects.get_or_create(
            company=self.company, name=product_name, defaults={"active": True}
        )
        sale = Sale.objects.create(
            company=self.company,
            total_amount=price * qty,
            status="confirmed",
        )
        SaleItem.objects.create(
            sale=sale, product=product, quantity=qty, unit_price=price, currency="USD"
        )
        return sale

    def test_uses_template_when_flag_enabled(self):
        self._make_sale()
        send_template_mock = AsyncMock(return_value=True)

        async def _run_test():
            with patch("apps.whatsapp.services.business_reports.settings") as mock_settings, \
                 patch("apps.whatsapp.services.business_reports.get_whatsapp_client", return_value=_mock_client(send_template_mock=send_template_mock)):
                mock_settings.USE_TEMPLATE_MESSAGES = True
                from apps.whatsapp.services.business_reports import send_daily_summary
                return await send_daily_summary(self.company)

        recipients = _run(_run_test())
        self.assertIn("+263771001001", recipients)
        send_template_mock.assert_called_once()
        call_args = send_template_mock.call_args
        self.assertEqual(call_args[0][1], "ats_daily_summary")
        components = call_args[1]["components"]
        self.assertEqual(len(components[0]["parameters"]), 4)

    def test_uses_image_when_flag_disabled(self):
        self._make_sale()
        send_image_mock = AsyncMock(return_value=True)
        send_template_mock = AsyncMock(return_value=True)

        async def _run_test():
            with patch("apps.whatsapp.services.business_reports.settings") as mock_settings, \
                 patch("apps.whatsapp.services.business_reports.get_whatsapp_client", return_value=_mock_client(send_template_mock=send_template_mock, send_image_mock=send_image_mock)), \
                 patch("apps.whatsapp.services.business_reports.upload_report_image", return_value="https://cdn.example.com/report.png"):
                mock_settings.USE_TEMPLATE_MESSAGES = False
                # Patch generate_stat_card at its definition point since it's imported locally
                import apps.whatsapp.services.report_card as rc
                original = rc.generate_stat_card
                rc.generate_stat_card = lambda *a, **kw: b"fakepng"
                try:
                    from apps.whatsapp.services.business_reports import send_daily_summary
                    return await send_daily_summary(self.company)
                finally:
                    rc.generate_stat_card = original

        _run(_run_test())
        send_image_mock.assert_called_once()
        send_template_mock.assert_not_called()

    def test_skips_when_no_sales(self):
        send_template_mock = AsyncMock(return_value=True)

        async def _run_test():
            with patch("apps.whatsapp.services.business_reports.settings") as mock_settings, \
                 patch("apps.whatsapp.services.business_reports.get_whatsapp_client", return_value=_mock_client(send_template_mock=send_template_mock)):
                mock_settings.USE_TEMPLATE_MESSAGES = True
                from apps.whatsapp.services.business_reports import send_daily_summary
                return await send_daily_summary(self.company)

        recipients = _run(_run_test())
        self.assertEqual(recipients, [])
        send_template_mock.assert_not_called()


class ApprovalWelcomeTemplateTest(TransactionTestCase):

    def setUp(self):
        from apps.core.models import WaitlistEntry
        self.waitlist_entry = WaitlistEntry.objects.create(
            phone_number="+263771002002",
            company_name="New Shop",
            status="pending",
            language="en",
        )
        self.approved_company = Company.objects.create(
            name="New Shop Approved", slug="new-shop-approved", active=True
        )

    def test_sends_template_on_approve_when_flag_enabled(self):
        send_template_mock = AsyncMock(return_value=True)
        approved_company = self.approved_company

        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler.settings") as mock_settings, \
                 patch("apps.whatsapp.services.waitlist_handler._get_and_update_waitlist_entry", new=AsyncMock(return_value=self.waitlist_entry)), \
                 patch("apps.whatsapp.services.waitlist_handler._approve_waitlist_entry", new=AsyncMock(return_value=(approved_company, None))), \
                 patch("apps.whatsapp.services.waitlist_handler._send_response", new=AsyncMock()), \
                 patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_template_mock=send_template_mock)):
                mock_settings.USE_TEMPLATE_MESSAGES = True
                from apps.whatsapp.services.waitlist_handler import process_waitlist_button_action_async
                await process_waitlist_button_action_async("approve", "+263770000001", original_message_sid="sid_abc")

        _run(_run_test())
        send_template_mock.assert_called_once_with("+263771002002", "ats_approval_welcome")

    def test_sends_free_form_on_approve_when_flag_disabled(self):
        send_response_mock = AsyncMock()
        send_template_mock = AsyncMock(return_value=True)
        approved_company = self.approved_company

        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler.settings") as mock_settings, \
                 patch("apps.whatsapp.services.waitlist_handler._get_and_update_waitlist_entry", new=AsyncMock(return_value=self.waitlist_entry)), \
                 patch("apps.whatsapp.services.waitlist_handler._approve_waitlist_entry", new=AsyncMock(return_value=(approved_company, None))), \
                 patch("apps.whatsapp.services.waitlist_handler._send_response", new=send_response_mock), \
                 patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_template_mock=send_template_mock)):
                mock_settings.USE_TEMPLATE_MESSAGES = False
                from apps.whatsapp.services.waitlist_handler import process_waitlist_button_action_async
                await process_waitlist_button_action_async("approve", "+263770000001", original_message_sid="sid_abc")

        _run(_run_test())
        send_template_mock.assert_not_called()
        # _send_response called twice: admin confirmation + shop owner welcome
        self.assertEqual(send_response_mock.call_count, 2)


class AssistantWelcomeTemplateTest(TransactionTestCase):

    def setUp(self):
        self.company = _make_company("Assistant Shop")
        self.owner = _make_owner(self.company, "+263771003003")

    def test_sends_template_to_assistant_when_flag_enabled(self):
        send_response_mock = AsyncMock()
        send_template_mock = AsyncMock(return_value=True)

        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler.settings") as mock_settings, \
                 patch("apps.whatsapp.services.waitlist_handler._get_profile_by_phone", new=AsyncMock(return_value=None)), \
                 patch("apps.whatsapp.services.waitlist_handler._create_assistant", new=AsyncMock()), \
                 patch("apps.whatsapp.services.waitlist_handler._send_response", new=send_response_mock), \
                 patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_template_mock=send_template_mock)):
                mock_settings.USE_TEMPLATE_MESSAGES = True
                from apps.whatsapp.services.waitlist_handler import handle_add_assistant
                parsed_result = SimpleNamespace(phone_number="+263771004004")
                await handle_add_assistant("+263771003003", "add assistant +263771004004", self.owner, parsed_result)

        _run(_run_test())
        send_template_mock.assert_called_once()
        self.assertEqual(send_template_mock.call_args[0][0], "+263771004004")
        self.assertEqual(send_template_mock.call_args[0][1], "ats_assistant_welcome")
        self.assertEqual(
            send_template_mock.call_args[1]["components"],
            [{"type": "body", "parameters": [{"type": "text", "text": "Assistant Shop"}]}],
        )
        self.assertEqual(send_response_mock.call_count, 1)

    def test_sends_localized_fallback_to_assistant_when_templates_disabled(self):
        send_response_mock = AsyncMock()
        send_template_mock = AsyncMock(return_value=True)

        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler.settings") as mock_settings, \
                 patch("apps.whatsapp.services.waitlist_handler._get_profile_by_phone", new=AsyncMock(return_value=None)), \
                 patch("apps.whatsapp.services.waitlist_handler._create_assistant", new=AsyncMock()), \
                 patch("apps.whatsapp.services.waitlist_handler._send_response", new=send_response_mock), \
                 patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_template_mock=send_template_mock)):
                mock_settings.USE_TEMPLATE_MESSAGES = False
                from apps.whatsapp.services.waitlist_handler import handle_add_assistant
                parsed_result = SimpleNamespace(phone_number="+263771005005")
                await handle_add_assistant("+263771003003", "add assistant +263771005005", self.owner, parsed_result)

        _run(_run_test())
        send_template_mock.assert_not_called()
        self.assertEqual(send_response_mock.call_count, 2)
        assistant_call = send_response_mock.call_args_list[1]
        self.assertEqual(assistant_call.args[0], "+263771005005")
        self.assertIn("assistant for Assistant Shop", assistant_call.args[1])

    def test_falls_back_to_localized_message_when_template_send_fails(self):
        send_response_mock = AsyncMock()
        send_template_mock = AsyncMock(return_value=False)

        async def _run_test():
            with patch("apps.whatsapp.services.waitlist_handler.settings") as mock_settings, \
                 patch("apps.whatsapp.services.waitlist_handler._get_profile_by_phone", new=AsyncMock(return_value=None)), \
                 patch("apps.whatsapp.services.waitlist_handler._create_assistant", new=AsyncMock()), \
                 patch("apps.whatsapp.services.waitlist_handler._send_response", new=send_response_mock), \
                 patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_template_mock=send_template_mock)):
                mock_settings.USE_TEMPLATE_MESSAGES = True
                from apps.whatsapp.services.waitlist_handler import handle_add_assistant
                parsed_result = SimpleNamespace(phone_number="+263771006006")
                await handle_add_assistant("+263771003003", "add assistant +263771006006", self.owner, parsed_result)

        _run(_run_test())
        send_template_mock.assert_called_once()
        self.assertEqual(send_response_mock.call_count, 2)
        assistant_call = send_response_mock.call_args_list[1]
        self.assertEqual(assistant_call.args[0], "+263771006006")
        self.assertIn("assistant for Assistant Shop", assistant_call.args[1])
