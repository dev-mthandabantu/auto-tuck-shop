"""Regression tests for #239: report card generation/upload failures were only
logged, with no distinct/alertable Sentry signal — a shop's visual reports
could silently degrade to text-only indefinitely with nobody noticing.

Both call sites (send_daily_summary in business_reports.py, and the on-demand
sales_query path in webhook_handler.py) must, on failure, call
sentry_sdk.capture_exception with a `failure_type` tag distinct from a
generic exception, and a `company_id` tag so it's filterable per-shop.
"""

import asyncio

from unit_tests.async_helpers import run_async_test as _run
import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import django.db.utils
from django.contrib.auth.models import User
from django.test import TransactionTestCase

from apps.core.models import Company, UserProfile
from apps.whatsapp.services.business_reports import BusinessSnapshot
from apps.whatsapp.services.message_parser import UnifiedMessageResult




def _make_profile(phone="+263771000299"):
    import uuid
    company = Company.objects.create(
        name="Test QA", slug=f"test-shop-239-{uuid.uuid4().hex[:8]}", active=True
    )
    user = User.objects.create_user(username=phone, password="x")
    return UserProfile.objects.create(
        user=user,
        company=company,
        phone_number=phone,
        role=UserProfile.Role.OWNER,
        language="en",
    )


def _make_snapshot(company_id, sales_count=1, report_date=None):
    return BusinessSnapshot(
        company_id=company_id,
        report_date=report_date or datetime.date(2026, 8, 19),
        currency="USD",
        sales_count=sales_count,
        items_sold=2,
        revenue=Decimal("20.00"),
        currency_revenues={"USD": Decimal("20.00")},
        cost=Decimal("0.00"),
        gross_profit=Decimal("20.00"),
        top_products=[("books", 2)],
        low_stock_items=[],
        out_of_stock_items=[],
    )


class DailySummaryReportCardFailureAlertingTest(TransactionTestCase):
    """send_daily_summary must emit a distinct, tagged Sentry event when the
    stat card fails to generate or upload — not just a log line."""

    def setUp(self):
        self.profile = _make_profile()
        self.company = self.profile.company

    def test_capture_exception_called_with_distinct_tags_on_generation_failure(self):
        from apps.whatsapp.services.business_reports import send_daily_summary

        snapshot = _make_snapshot(self.company.id)
        scope_mock = MagicMock()
        scope_mock.__enter__ = Mock(return_value=scope_mock)
        scope_mock.__exit__ = Mock(return_value=False)

        with (
            patch(
                "apps.whatsapp.services.business_reports.build_business_snapshot",
                return_value=snapshot,
            ),
            patch(
                "apps.whatsapp.services.business_reports.build_comparison_context",
                return_value={},
            ),
            patch(
                "apps.whatsapp.services.report_card.generate_stat_card",
                side_effect=RuntimeError("boom"),
            ),
            patch("apps.whatsapp.services.business_reports._company_recipients", return_value=[]),
            patch("apps.whatsapp.services.business_reports.mark_summary_sent", new=AsyncMock()),
            patch("sentry_sdk.new_scope", return_value=scope_mock) as new_scope_mock,
            patch("sentry_sdk.capture_exception") as capture_mock,
        ):
            _run(send_daily_summary(self.company, report_date=snapshot.report_date))

        new_scope_mock.assert_called_once()
        capture_mock.assert_called_once()
        self.assertIsInstance(capture_mock.call_args[0][0], RuntimeError)

        tag_calls = {call.args[0]: call.args[1] for call in scope_mock.set_tag.call_args_list}
        self.assertEqual(tag_calls.get("failure_type"), "report_card_generation")
        self.assertEqual(tag_calls.get("company_id"), self.company.id)
        self.assertEqual(tag_calls.get("source"), "send_daily_summary")

        context_calls = {call.args[0]: call.args[1] for call in scope_mock.set_context.call_args_list}
        self.assertIn("report_card", context_calls)
        self.assertEqual(context_calls["report_card"]["company_id"], self.company.id)


class OnDemandReportCardFailureAlertingTest(TransactionTestCase):
    """_handle_sales_query (the "send daily summary" chat command) must emit
    the same distinct, tagged Sentry event on report card failure."""

    def setUp(self):
        self.profile = _make_profile()
        self.company = self.profile.company

    def test_capture_exception_called_with_distinct_tags_on_generation_failure(self):
        from apps.whatsapp.services.webhook_handler import _handle_sales_query

        snapshot = _make_snapshot(self.company.id)
        result = UnifiedMessageResult(intent="sales_query", confidence=1.0, timeframe="today")
        scope_mock = MagicMock()
        scope_mock.__enter__ = Mock(return_value=scope_mock)
        scope_mock.__exit__ = Mock(return_value=False)

        with (
            patch(
                "apps.whatsapp.services.business_reports.build_business_snapshot",
                return_value=snapshot,
            ),
            patch(
                "apps.whatsapp.services.business_reports.build_comparison_context",
                return_value={},
            ),
            patch(
                "apps.whatsapp.services.report_card.generate_stat_card",
                side_effect=RuntimeError("boom"),
            ),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=AsyncMock()),
            patch("apps.whatsapp.services.webhook_handler.get_whatsapp_client"),
            patch("sentry_sdk.new_scope", return_value=scope_mock) as new_scope_mock,
            patch("sentry_sdk.capture_exception") as capture_mock,
        ):
            _run(_handle_sales_query(self.profile.phone_number, self.profile, result, lang="en"))

        new_scope_mock.assert_called_once()
        capture_mock.assert_called_once()
        self.assertIsInstance(capture_mock.call_args[0][0], RuntimeError)

        tag_calls = {call.args[0]: call.args[1] for call in scope_mock.set_tag.call_args_list}
        self.assertEqual(tag_calls.get("failure_type"), "report_card_generation")
        self.assertEqual(tag_calls.get("company_id"), self.company.id)
        self.assertEqual(tag_calls.get("source"), "sales_query_on_demand")

        context_calls = {call.args[0]: call.args[1] for call in scope_mock.set_context.call_args_list}
        self.assertIn("report_card", context_calls)
        self.assertEqual(context_calls["report_card"]["source"], "sales_query_on_demand")
