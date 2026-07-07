"""Unit tests for the GTM drip nudge service."""

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.core.models import Company, UserProfile
from apps.whatsapp.services.nudge_service import (
    _compute_streak,
    _eligible_ctas,
    _in_send_window,
    _template_name_for_lang,
    build_shop_context,
    maybe_send_nudges,
    maybe_send_onboarding_followup,
    maybe_send_reengagement,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_company(name="Test Shop", first_message_date=None, last_nudge_date=None, nudge_stage=0):
    company = Company.objects.create(
        name=name,
        slug=name.lower().replace(" ", "-"),
        active=True,
        first_message_date=first_message_date,
        last_nudge_date=last_nudge_date,
        nudge_stage=nudge_stage,
    )
    return company


def _make_user_profile(company, phone, opted_out=False):
    user = User.objects.create_user(username=phone, password="x")
    profile = UserProfile.objects.create(
        user=user, company=company, phone_number=phone,
        role=UserProfile.Role.OWNER, nudge_opt_out=opted_out,
    )
    return profile


class TemplateNameForLangTest(TestCase):
    """Direct tests for _template_name_for_lang — no DB needed. Regression
    for #235: these are distinct template NAMES submitted to Meta, not
    language variants of one name."""

    def test_shona_returns_shona_named_template(self):
        self.assertEqual(
            _template_name_for_lang("ats_daily_nudge", "sn"),
            "_ats_daily_nudge_shona_version",
        )

    def test_english_returns_base_name_unchanged(self):
        self.assertEqual(_template_name_for_lang("ats_daily_nudge", "en"), "ats_daily_nudge")

    def test_shona_falls_back_to_base_name_if_no_shona_variant_submitted(self):
        self.assertEqual(_template_name_for_lang("ats_some_future_template", "sn"), "ats_some_future_template")

    def test_all_four_known_templates_map_correctly(self):
        expected = {
            "ats_daily_nudge": "_ats_daily_nudge_shona_version",
            "ats_onboarding_followup": "ats_onboarding_followup_shona_version",
            "ats_reengagement": "ats_reengagement_shona_version",
            "ats_daily_summary": "ats_daily_summary_shona_version",
        }
        for base_name, shona_name in expected.items():
            self.assertEqual(_template_name_for_lang(base_name, "sn"), shona_name)


class StreakCalculationTest(TransactionTestCase):

    def setUp(self):
        self.company = _make_company()

    def test_no_sales_streak_is_zero(self):
        self.assertEqual(_compute_streak(self.company), 0)

    def test_streak_with_consecutive_days(self):
        from apps.sales.models import Sale
        today = timezone.localdate()
        for i in range(3):
            Sale.objects.create(
                company=self.company,
                sale_timestamp=timezone.now() - timedelta(days=i),
                total_amount=10,
                status="confirmed",
            )
        self.assertEqual(_compute_streak(self.company), 3)

    def test_streak_resets_on_gap(self):
        from apps.sales.models import Sale
        today = timezone.localdate()
        # Sale today and 2 days ago — gap on yesterday
        Sale.objects.create(company=self.company, sale_timestamp=timezone.now(), total_amount=5, status="confirmed")
        Sale.objects.create(company=self.company, sale_timestamp=timezone.now() - timedelta(days=2), total_amount=5, status="confirmed")
        self.assertEqual(_compute_streak(self.company), 1)


class EligibleCtasTest(TransactionTestCase):

    def test_onboarding_ctas_within_window(self):
        context = {"days_since_onboarding": 3, "streak": 0, "features_used": [], "nudge_stage": 0}
        keys = [c["key"] for c in _eligible_ctas(context)]
        self.assertIn("onboarding_first_sale", keys)
        self.assertIn("onboarding_shona_tip", keys)

    def test_onboarding_first_sale_excluded_if_shop_has_sales(self):
        # Regression: Sweetwaters received onboarding_first_sale despite having confirmed sales (#153)
        context = {"days_since_onboarding": 0, "streak": 0, "last_active_days_ago": 0, "features_used": [], "nudge_stage": 0}
        keys = [c["key"] for c in _eligible_ctas(context)]
        self.assertNotIn("onboarding_first_sale", keys)

    def test_onboarding_first_sale_included_if_no_sales(self):
        context = {"days_since_onboarding": 0, "streak": 0, "last_active_days_ago": None, "features_used": [], "nudge_stage": 0}
        keys = [c["key"] for c in _eligible_ctas(context)]
        self.assertIn("onboarding_first_sale", keys)

    def test_onboarding_ctas_excluded_after_window(self):
        context = {"days_since_onboarding": 20, "streak": 0, "features_used": [], "nudge_stage": 0}
        keys = [c["key"] for c in _eligible_ctas(context)]
        self.assertNotIn("onboarding_first_sale", keys)

    def test_streak_cta_only_when_streak_gte_2(self):
        ctx_no_streak = {"days_since_onboarding": 5, "streak": 1, "features_used": [], "nudge_stage": 0}
        ctx_with_streak = {"days_since_onboarding": 5, "streak": 3, "features_used": [], "nudge_stage": 0}
        self.assertNotIn("retention_streak", [c["key"] for c in _eligible_ctas(ctx_no_streak)])
        self.assertIn("retention_streak", [c["key"] for c in _eligible_ctas(ctx_with_streak)])

    def test_reports_cta_excluded_if_feature_used(self):
        context = {"days_since_onboarding": 5, "streak": 0, "features_used": ["reports"], "nudge_stage": 0}
        keys = [c["key"] for c in _eligible_ctas(context)]
        self.assertNotIn("discovery_weekly_report", keys)

    def test_onboarding_reports_cta_is_button_type(self):
        context = {"days_since_onboarding": 3, "streak": 0, "features_used": [], "nudge_stage": 0}
        ctas = _eligible_ctas(context)
        reports_cta = next((c for c in ctas if c["key"] == "onboarding_reports"), None)
        self.assertIsNotNone(reports_cta)
        self.assertEqual(reports_cta["type"], "button")

    def test_all_other_ctas_are_text_type(self):
        context = {"days_since_onboarding": 3, "streak": 3, "features_used": [], "nudge_stage": 0}
        ctas = _eligible_ctas(context)
        for cta in ctas:
            if cta["key"] != "onboarding_reports":
                self.assertEqual(cta["type"], "text", f"Expected text type for {cta['key']}")

    def test_insight_ctas_never_appear(self):
        for context in [
            {"days_since_onboarding": 3, "streak": 5, "last_active_days_ago": 0, "features_used": [], "nudge_stage": 0},
            {"days_since_onboarding": 20, "streak": 0, "last_active_days_ago": None, "features_used": [], "nudge_stage": 0},
        ]:
            keys = [c["key"] for c in _eligible_ctas(context)]
            self.assertNotIn("insight_best_day", keys)
            self.assertNotIn("insight_daily_average", keys)

    def test_retention_excluded_for_never_sold_shop(self):
        context = {"days_since_onboarding": 5, "streak": 0, "last_active_days_ago": None, "features_used": [], "nudge_stage": 0}
        keys = [c["key"] for c in _eligible_ctas(context)]
        self.assertNotIn("retention_no_sales_today", keys)

    def test_retention_included_for_shop_with_history(self):
        context = {"days_since_onboarding": 5, "streak": 0, "last_active_days_ago": 1, "features_used": [], "nudge_stage": 0}
        keys = [c["key"] for c in _eligible_ctas(context)]
        self.assertIn("retention_no_sales_today", keys)


class MaybeSendNudgesTest(TransactionTestCase):

    def setUp(self):
        self.company = _make_company(first_message_date=date.today() - timedelta(days=3))
        self.profile = _make_user_profile(self.company, "+263771000099")

    @patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=False)
    def test_skips_outside_window(self, _mock):
        result = _run(maybe_send_nudges())
        self.assertEqual(result["reason"], "outside_window")
        self.assertEqual(result["sent"], [])

    @patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=True)
    @patch("apps.whatsapp.services.nudge_service._recently_active", return_value=AsyncMock(return_value=True))
    def test_skips_recently_active_shop(self, _mock_active, _mock_window):
        _mock_active.return_value = True
        _mock_active.side_effect = AsyncMock(return_value=True)
        with patch("apps.whatsapp.services.nudge_service._recently_active", new=AsyncMock(return_value=True)):
            result = _run(maybe_send_nudges())
        self.assertIn(self.company.id, result["skipped"])
        self.assertNotIn(self.company.id, result["sent"])

    @patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=True)
    def test_sends_nudge_to_eligible_shop(self, _mock_window):
        async def _run_with_mocks():
            with patch("apps.whatsapp.services.nudge_service._recently_active", new=AsyncMock(return_value=False)), \
                 patch("apps.whatsapp.services.nudge_service.pick_nudge_cta", new=AsyncMock(return_value={"message": "What did you sell today?", "cta_type": "text"})), \
                 patch("apps.whatsapp.services.nudge_service._send_nudge", new=AsyncMock(return_value=True)):
                return await maybe_send_nudges()
        result = _run(_run_with_mocks())
        self.assertIn(self.company.id, result["sent"])
        self.company.refresh_from_db()
        self.assertEqual(self.company.last_nudge_date, date.today())
        self.assertEqual(self.company.nudge_stage, 1)

    @patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=True)
    def test_skips_shop_nudged_today(self, _mock_window):
        self.company.last_nudge_date = date.today()
        self.company.save()
        with patch("apps.whatsapp.services.nudge_service._recently_active", new=AsyncMock(return_value=False)):
            result = _run(maybe_send_nudges())
        self.assertNotIn(self.company.id, result["sent"])

    @patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=True)
    def test_skips_shop_with_all_users_opted_out(self, _mock_window):
        self.profile.nudge_opt_out = True
        self.profile.save()
        with patch("apps.whatsapp.services.nudge_service._recently_active", new=AsyncMock(return_value=False)):
            result = _run(maybe_send_nudges())
        self.assertNotIn(self.company.id, result["sent"])

    @patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=True)
    def test_skips_company_without_first_message_date(self, _mock_window):
        self.company.first_message_date = None
        self.company.save()
        with patch("apps.whatsapp.services.nudge_service._recently_active", new=AsyncMock(return_value=False)):
            result = _run(maybe_send_nudges())
        self.assertNotIn(self.company.id, result["sent"])


def _mock_client(send_template_mock):
    """Return a mock WhatsApp client with send_template_message replaced."""
    from unittest.mock import MagicMock
    client = MagicMock()
    client.send_template_message = send_template_mock
    return client


class OnboardingFollowupTest(TransactionTestCase):

    def setUp(self):
        self.company = _make_company(
            name="New Shop",
            first_message_date=date.today() - timedelta(days=2),
        )
        self.profile = _make_user_profile(self.company, "+263771000200")

    def test_sends_shona_template_to_shona_speaking_owner(self):
        """UserProfile.language defaults to 'sn' — should use the Shona-named
        template (submitted to Meta as a distinct name, not a language
        variant of the English template — see #235)."""
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_mock)):
                return await maybe_send_onboarding_followup()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])
        send_mock.assert_called_once_with("+263771000200", "ats_onboarding_followup_shona_version")
        self.company.refresh_from_db()
        self.assertTrue(self.company.onboarding_followup_sent)

    def test_sends_english_template_to_english_speaking_owner(self):
        self.profile.language = "en"
        self.profile.save()
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_mock)):
                return await maybe_send_onboarding_followup()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])
        send_mock.assert_called_once_with("+263771000200", "ats_onboarding_followup")

    def test_skips_shop_approved_today(self):
        self.company.first_message_date = date.today()
        self.company.save()
        # No candidates found — no send call made, just check result
        result = _run(maybe_send_onboarding_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_skips_shop_with_confirmed_sale(self):
        from apps.sales.models import Sale
        Sale.objects.create(company=self.company, total_amount=10, status="confirmed")
        result = _run(maybe_send_onboarding_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_skips_shop_already_sent(self):
        self.company.onboarding_followup_sent = True
        self.company.save()
        result = _run(maybe_send_onboarding_followup())
        self.assertNotIn(self.company.id, result["sent"])


class ReengagementTest(TransactionTestCase):

    def setUp(self):
        self.company = _make_company(
            name="Silent Shop",
            first_message_date=date.today() - timedelta(days=10),
        )
        self.profile = _make_user_profile(self.company, "+263771000300")

    def _make_sale(self, days_ago):
        from apps.sales.models import Sale
        Sale.objects.create(
            company=self.company,
            sale_timestamp=timezone.now() - timedelta(days=days_ago),
            total_amount=10,
            status="confirmed",
        )

    def test_sends_shona_template_to_shop_silent_2_plus_days(self):
        """UserProfile.language defaults to 'sn' — should use the Shona-named
        template (submitted to Meta as a distinct name — see #235)."""
        self._make_sale(days_ago=3)
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_mock)):
                return await maybe_send_reengagement()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])
        self.company.refresh_from_db()
        self.assertEqual(self.company.last_reengagement_date, date.today())
        call_kwargs = send_mock.call_args
        self.assertEqual(call_kwargs[0][1], "ats_reengagement_shona_version")
        self.assertIn("components", call_kwargs[1])

    def test_sends_english_template_to_english_speaking_owner(self):
        self.profile.language = "en"
        self.profile.save()
        self._make_sale(days_ago=3)
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_mock)):
                return await maybe_send_reengagement()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])
        call_kwargs = send_mock.call_args
        self.assertEqual(call_kwargs[0][1], "ats_reengagement")

    def test_skips_shop_active_yesterday(self):
        self._make_sale(days_ago=1)
        result = _run(maybe_send_reengagement())
        self.assertNotIn(self.company.id, result["sent"])

    def test_skips_shop_with_no_sales_history(self):
        result = _run(maybe_send_reengagement())
        self.assertNotIn(self.company.id, result["sent"])

    def test_skips_shop_reengaged_within_7_days(self):
        self._make_sale(days_ago=3)
        self.company.last_reengagement_date = date.today() - timedelta(days=3)
        self.company.save()
        result = _run(maybe_send_reengagement())
        self.assertNotIn(self.company.id, result["sent"])

    def test_sends_to_shop_reengaged_over_7_days_ago(self):
        self._make_sale(days_ago=3)
        self.company.last_reengagement_date = date.today() - timedelta(days=8)
        self.company.save()
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_mock)):
                return await maybe_send_reengagement()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])


class ZeroContactFollowupTest(TransactionTestCase):
    """Shops approved but that have never sent a single message — regression for #234."""

    def setUp(self):
        self.company = _make_company(name="Never Started Shop")
        self.profile = _make_user_profile(self.company, "+263771000400")

    def _set_approved_days_ago(self, days):
        approved_at = timezone.now() - timedelta(days=days, minutes=30)
        Company.objects.filter(pk=self.company.pk).update(created_at=approved_at)
        self.company.refresh_from_db()

    def test_sends_on_day_2(self):
        self._set_approved_days_ago(2)
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_mock)):
                from apps.whatsapp.services.nudge_service import maybe_send_zero_contact_followup
                return await maybe_send_zero_contact_followup()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])
        send_mock.assert_called_once_with(
            "+263771000400", "ats_zero_contact_followup", language_code="sn"
        )
        self.company.refresh_from_db()
        self.assertEqual(self.company.zero_contact_followup_count, 1)
        self.assertEqual(self.company.last_zero_contact_followup_date, date.today())

    def test_sends_on_day_5_and_7(self):
        for days in (5, 7):
            self._set_approved_days_ago(days)
            self.company.last_zero_contact_followup_date = None
            self.company.save()
            send_mock = AsyncMock(return_value=True)
            async def _run_test():
                with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_mock)):
                    from apps.whatsapp.services.nudge_service import maybe_send_zero_contact_followup
                    return await maybe_send_zero_contact_followup()
            result = _run(_run_test())
            self.assertIn(self.company.id, result["sent"])
            self.company.refresh_from_db()

    def test_skips_shop_that_has_sent_a_message(self):
        self._set_approved_days_ago(2)
        self.company.first_message_date = date.today()
        self.company.save()
        from apps.whatsapp.services.nudge_service import maybe_send_zero_contact_followup
        result = _run(maybe_send_zero_contact_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_skips_shop_not_on_a_scheduled_day(self):
        self._set_approved_days_ago(3)  # day 3 — not in (2, 5, 7)
        from apps.whatsapp.services.nudge_service import maybe_send_zero_contact_followup
        result = _run(maybe_send_zero_contact_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_stops_after_3_attempts(self):
        self._set_approved_days_ago(7)
        self.company.zero_contact_followup_count = 3
        self.company.save()
        from apps.whatsapp.services.nudge_service import maybe_send_zero_contact_followup
        result = _run(maybe_send_zero_contact_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_does_not_double_send_same_day(self):
        self._set_approved_days_ago(2)
        self.company.last_zero_contact_followup_date = date.today()
        self.company.save()
        from apps.whatsapp.services.nudge_service import maybe_send_zero_contact_followup
        result = _run(maybe_send_zero_contact_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_does_not_advance_count_on_send_failure(self):
        self._set_approved_days_ago(2)
        send_mock = AsyncMock(return_value=False)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_mock)):
                from apps.whatsapp.services.nudge_service import maybe_send_zero_contact_followup
                return await maybe_send_zero_contact_followup()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["skipped"])
        self.company.refresh_from_db()
        self.assertEqual(self.company.zero_contact_followup_count, 0)


class OptOutHandlerTest(TransactionTestCase):
    """Test that 'stop' sets nudge_opt_out on the UserProfile."""

    def setUp(self):
        self.company = _make_company(first_message_date=date.today())
        self.profile = _make_user_profile(self.company, "+263771000088")

    def test_opt_out_flag_set_via_webhook(self):
        from apps.whatsapp.services.webhook_handler import _process_message_async
        with patch("apps.whatsapp.services.webhook_handler.parse_message_unified") as _mock_llm, \
             patch("apps.whatsapp.services.webhook_handler._send_response") as _mock_send:
            _mock_send.return_value = None
            _run(_process_message_async(
                message_id="wamid.test_optout_001",
                sender="+263771000088",
                text="stop",
                user_profile=self.profile,
            ))
            self.profile.refresh_from_db()
            self.assertTrue(self.profile.nudge_opt_out)
            _mock_llm.assert_not_called()
