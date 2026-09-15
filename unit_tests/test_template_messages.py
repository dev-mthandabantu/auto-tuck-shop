"""Tests for template message wiring — send_daily_summary and approval_welcome."""

import asyncio

from unit_tests.async_helpers import run_async_test as _run
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.utils import timezone

from apps.core.models import Company, UserProfile
from apps.whatsapp.models import WhatsAppMessage




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

    def test_uses_shona_template_when_flag_enabled_and_outside_window(self):
        """No inbound message in the last 24h → outside service window → template path.
        UserProfile.language defaults to 'sn' — should use the Shona-named
        template (submitted to Meta as a distinct name — see #235)."""
        msg = WhatsAppMessage.objects.create(
            phone_number="+263771001001",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type=WhatsAppMessage.MessageType.TEXT,
            company=self.company,
        )
        WhatsAppMessage.objects.filter(pk=msg.pk).update(timestamp=timezone.now() - timedelta(hours=30))

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
        self.assertEqual(call_args[0][1], "ats_daily_summary_shona_version")
        components = call_args[1]["components"]
        self.assertEqual(len(components[0]["parameters"]), 4)

    def test_template_revenue_uses_actual_sale_currency_not_company_default(self):
        """Regression test for #246 — the template's revenue parameter must
        reflect the sale's actual currency (ZAR), not the company's
        configured default (USD)."""
        msg = WhatsAppMessage.objects.create(
            phone_number="+263771001001",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type=WhatsAppMessage.MessageType.TEXT,
            company=self.company,
        )
        WhatsAppMessage.objects.filter(pk=msg.pk).update(timestamp=timezone.now() - timedelta(hours=30))

        self._make_sale(product_name="Bread", qty=3, price=Decimal("18.00"))
        from apps.sales.models import SaleItem
        SaleItem.objects.filter(sale__company=self.company).update(currency="ZAR")

        send_template_mock = AsyncMock(return_value=True)

        async def _run_test():
            with patch("apps.whatsapp.services.business_reports.settings") as mock_settings, \
                 patch("apps.whatsapp.services.business_reports.get_whatsapp_client", return_value=_mock_client(send_template_mock=send_template_mock)):
                mock_settings.USE_TEMPLATE_MESSAGES = True
                from apps.whatsapp.services.business_reports import send_daily_summary
                return await send_daily_summary(self.company)

        _run(_run_test())
        send_template_mock.assert_called_once()
        components = send_template_mock.call_args[1]["components"]
        revenue_param = components[0]["parameters"][1]["text"]
        self.assertIn("R", revenue_param)
        self.assertNotIn("$", revenue_param)

    def test_uses_english_template_for_english_speaking_owner(self):
        self.owner.language = "en"
        self.owner.save()
        msg = WhatsAppMessage.objects.create(
            phone_number="+263771001001",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type=WhatsAppMessage.MessageType.TEXT,
            company=self.company,
        )
        WhatsAppMessage.objects.filter(pk=msg.pk).update(timestamp=timezone.now() - timedelta(hours=30))

        self._make_sale()
        send_template_mock = AsyncMock(return_value=True)

        async def _run_test():
            with patch("apps.whatsapp.services.business_reports.settings") as mock_settings, \
                 patch("apps.whatsapp.services.business_reports.get_whatsapp_client", return_value=_mock_client(send_template_mock=send_template_mock)):
                mock_settings.USE_TEMPLATE_MESSAGES = True
                from apps.whatsapp.services.business_reports import send_daily_summary
                return await send_daily_summary(self.company)

        _run(_run_test())
        call_args = send_template_mock.call_args
        self.assertEqual(call_args[0][1], "ats_daily_summary")

    def test_uses_image_when_flag_enabled_but_within_window(self):
        """Recent inbound message (<24h) + flag enabled → still image/text, NOT template.
        Regression test for #237.
        """
        msg = WhatsAppMessage.objects.create(
            phone_number="+263771001001",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type=WhatsAppMessage.MessageType.TEXT,
            company=self.company,
        )
        WhatsAppMessage.objects.filter(pk=msg.pk).update(timestamp=timezone.now() - timedelta(hours=2))

        self._make_sale()
        send_image_mock = AsyncMock(return_value=True)
        send_template_mock = AsyncMock(return_value=True)

        async def _run_test():
            with patch("apps.whatsapp.services.business_reports.settings") as mock_settings, \
                 patch("apps.whatsapp.services.business_reports.get_whatsapp_client", return_value=_mock_client(send_template_mock=send_template_mock, send_image_mock=send_image_mock)), \
                 patch("apps.whatsapp.services.business_reports.upload_report_image", return_value="https://cdn.example.com/report.png"):
                mock_settings.USE_TEMPLATE_MESSAGES = True
                import apps.whatsapp.services.report_card as rc
                original = rc.generate_stat_card
                rc.generate_stat_card = lambda *a, **kw: b"fakepng"
                try:
                    from apps.whatsapp.services.business_reports import send_daily_summary
                    return await send_daily_summary(self.company)
                finally:
                    rc.generate_stat_card = original

        recipients = _run(_run_test())
        self.assertIn("+263771001001", recipients)
        send_image_mock.assert_called_once()
        send_template_mock.assert_not_called()

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
        send_template_mock.assert_called_once_with(
            "+263771002002",
            "ats_approval_welcome",
            components=[{"type": "body", "parameters": [{"type": "text", "text": "New Shop Approved"}]}],
        )

    def test_sends_shona_named_template_for_shona_owner(self):
        """Regression for #235: lang was computed but never passed to the
        template call, so Shona owners always got the English template name."""
        self.waitlist_entry.language = "sn"
        self.waitlist_entry.save(update_fields=["language"])
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
        send_template_mock.assert_called_once_with(
            "+263771002002",
            "ats_approval_welcome_shona_version",
            components=[{"type": "body", "parameters": [{"type": "text", "text": "New Shop Approved"}]}],
        )

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
        # _make_owner leaves language unset, which defaults to Shona (see
        # UserProfile.language). These tests assert English fallback copy,
        # so make the owner's language explicit rather than relying on the
        # UserProfile model default.
        self.owner.language = "en"
        self.owner.save(update_fields=["language"])

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

    def test_sends_shona_named_template_when_owner_language_is_shona(self):
        """Regression for #235: lang (the owner's language) was computed but
        never passed to the template call."""
        self.owner.language = "sn"
        self.owner.save(update_fields=["language"])
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
        self.assertEqual(send_template_mock.call_args[0][1], "ats_assistant_welcome_shona_version")

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
