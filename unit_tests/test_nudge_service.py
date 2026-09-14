"""Unit tests for the GTM drip nudge service."""

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.core.models import Company, UserProfile
from apps.whatsapp.services.nudge_service import (
    _compute_streak,
    _eligible_ctas,
    _in_send_window,
    _mark_unhandled_tap_followup_sent,
    _template_name_for_lang,
    build_shop_context,
    maybe_send_nudges,
    maybe_send_onboarding_followup,
    maybe_send_reengagement,
    maybe_send_unhandled_tap_followup,
    maybe_send_unnamed_shop_followup,
    record_unhandled_button_tap,
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

    def test_approval_and_assistant_welcome_map_correctly(self):
        """Regression for #235's second gap: waitlist approval and assistant-add
        welcomes were computing lang but never passing it to the template call."""
        expected = {
            "ats_approval_welcome": "ats_approval_welcome_shona_version",
            "ats_assistant_welcome": "ats_assistant_welcome_shona_version",
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
        # onboarding_shona_tip/onboarding_reports are time-boxed to the first
        # _ONBOARDING_DAYS. onboarding_first_sale is NOT — it stays eligible
        # for as long as the shop has zero sales, regardless of age (#257).
        context = {"days_since_onboarding": 20, "streak": 0, "last_active_days_ago": 1, "features_used": [], "nudge_stage": 0}
        keys = [c["key"] for c in _eligible_ctas(context)]
        self.assertNotIn("onboarding_first_sale", keys)
        self.assertNotIn("onboarding_shona_tip", keys)

    def test_onboarding_first_sale_still_eligible_after_window_if_never_activated(self):
        # Regression: a shop with zero sales ever was falling into the
        # discovery/retention bucket once days_since_onboarding > 14, receiving
        # nudges about features of a workflow it had never entered (#257).
        context = {"days_since_onboarding": 40, "streak": 0, "last_active_days_ago": None, "features_used": [], "nudge_stage": 0}
        keys = [c["key"] for c in _eligible_ctas(context)]
        self.assertIn("onboarding_first_sale", keys)
        self.assertNotIn("discovery_weekly_report", keys)
        self.assertNotIn("discovery_multi_item", keys)
        self.assertNotIn("discovery_photo_upload", keys)
        self.assertNotIn("retention_no_sales_today", keys)

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

    def test_discovery_photo_upload_always_eligible_for_activated_shop(self):
        # #258: surface the existing photo -> batch-sale parsing capability as a
        # discovery nudge, same shape as discovery_multi_item — not gated on a
        # features_used flag, since there's no reliable way to detect "owner
        # keeps a written record" from message history the way "used reports"
        # or "sent a multi-item message" can be detected. Requires prior sales
        # history though — see test_retention_excluded_for_never_sold_shop (#257).
        for context in [
            {"days_since_onboarding": 3, "streak": 0, "last_active_days_ago": 0, "features_used": [], "nudge_stage": 0},
            {"days_since_onboarding": 20, "streak": 0, "last_active_days_ago": 5, "features_used": ["multi_item", "reports"], "nudge_stage": 0},
        ]:
            keys = [c["key"] for c in _eligible_ctas(context)]
            self.assertIn("discovery_photo_upload", keys)


class MaybeSendNudgesTest(TransactionTestCase):

    def setUp(self):
        # day 1 deliberately avoids _ONBOARDING_FOLLOWUP_DAYS (3, 7, 14) —
        # this class tests the generic nudge path in isolation; a shop on
        # its actual followup cadence day is covered by
        # test_yields_slot_to_onboarding_followup_candidate below (#257).
        self.company = _make_company(first_message_date=date.today() - timedelta(days=1))
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
                 patch("apps.whatsapp.services.nudge_service.pick_nudge_cta", new=AsyncMock(return_value={"cta_key": "retention_no_sales_today", "message": "What did you sell today?", "cta_type": "text"})), \
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

    @patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=True)
    def test_skips_shop_already_messaged_today_by_another_mechanism(self, _mock_window):
        """Regression for #262 — a shop already messaged today via a different
        automated nudge mechanism (e.g. reengagement) should not also get the
        daily nudge, even though its own last_nudge_date is unset."""
        self.company.last_automated_message_date = date.today()
        self.company.save()
        with patch("apps.whatsapp.services.nudge_service._recently_active", new=AsyncMock(return_value=False)):
            result = _run(maybe_send_nudges())
        self.assertNotIn(self.company.id, result["sent"])
        self.assertIn(self.company.id, result["skipped"])

    @patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=True)
    def test_yields_slot_to_onboarding_followup_candidate(self, _mock_window):
        """Regression for #257 — a never-activated shop on its exact day-3/7/14
        onboarding followup cadence day must not be claimed by the generic nudge
        first. Both mechanisms share the one-message-per-day gate (#262); if the
        generic nudge fires here, maybe_send_onboarding_followup never runs and
        onboarding_followup_count never advances. The yield only applies when
        USE_TEMPLATE_MESSAGES is on (see test_sends_nudge_on_cadence_day_when_
        templates_disabled for the off case) — pinned explicitly here rather
        than relying on the environment's default."""
        self.company.first_message_date = date.today() - timedelta(days=3)
        self.company.save()
        with patch("apps.whatsapp.services.nudge_service._recently_active", new=AsyncMock(return_value=False)), \
             patch.object(settings, "USE_TEMPLATE_MESSAGES", True):
            result = _run(maybe_send_nudges())
        self.assertNotIn(self.company.id, result["sent"])
        self.assertIn(self.company.id, result["skipped"])
        self.company.refresh_from_db()
        self.assertIsNone(self.company.last_automated_message_date)

    @patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=True)
    def test_sends_nudge_on_cadence_day_when_templates_disabled(self, _mock_window):
        """When USE_TEMPLATE_MESSAGES is off, maybe_send_onboarding_followup
        never runs (send_nudges.py gates it on that flag) — yielding the slot
        in that case would mean the shop gets no nudge at all that day, a
        regression from before this PR. Must still send the generic nudge (#257)."""
        self.company.first_message_date = date.today() - timedelta(days=3)
        self.company.save()
        async def _run_with_mocks():
            with patch("apps.whatsapp.services.nudge_service._recently_active", new=AsyncMock(return_value=False)), \
                 patch("apps.whatsapp.services.nudge_service.pick_nudge_cta", new=AsyncMock(return_value={"cta_key": "onboarding_first_sale", "message": "Record your first sale!", "cta_type": "text"})), \
                 patch("apps.whatsapp.services.nudge_service._send_nudge", new=AsyncMock(return_value=True)), \
                 patch.object(settings, "USE_TEMPLATE_MESSAGES", False):
                return await maybe_send_nudges()
        result = _run(_run_with_mocks())
        self.assertIn(self.company.id, result["sent"])


def _mock_client(send_template_mock):
    """Return a mock WhatsApp client with send_template_message replaced."""
    from unittest.mock import MagicMock
    client = MagicMock()
    client.send_template_message = send_template_mock
    return client


class OnboardingFollowupTest(TransactionTestCase):
    """Shops that sent a message and onboarded but never recorded a confirmed sale — regression for #257."""

    def setUp(self):
        # day 3 is the first date in _ONBOARDING_FOLLOWUP_DAYS (#257)
        self.company = _make_company(
            name="New Shop",
            first_message_date=date.today() - timedelta(days=3),
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
        self.assertEqual(self.company.onboarding_followup_count, 1)

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

    def test_skips_shop_not_on_cadence_day(self):
        self.company.first_message_date = date.today() - timedelta(days=2)
        self.company.save()
        # 2 days isn't in _ONBOARDING_FOLLOWUP_DAYS (3, 7, 14) — no candidates found
        result = _run(maybe_send_onboarding_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_stops_after_cap_reached(self):
        # Regression: the old BooleanField sent exactly once then let the shop
        # fall back to the generic daily nudge cycle indefinitely. Now capped
        # at len(_ONBOARDING_FOLLOWUP_DAYS) attempts, then handed to human
        # outreach per #149 (#257).
        self.company.onboarding_followup_count = 3
        self.company.save()
        result = _run(maybe_send_onboarding_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_skips_shop_with_confirmed_sale(self):
        from apps.sales.models import Sale
        Sale.objects.create(company=self.company, total_amount=10, status="confirmed")
        result = _run(maybe_send_onboarding_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_skips_shop_already_sent_today(self):
        self.company.last_onboarding_followup_date = date.today()
        self.company.save()
        result = _run(maybe_send_onboarding_followup())
        self.assertNotIn(self.company.id, result["sent"])


class UnnamedShopFollowupTest(TransactionTestCase):
    """#316: ask once for the real name if a shop is still stuck on the
    "Unnamed Shop" placeholder set at approval time."""

    def setUp(self):
        self.company = _make_company(name="Unnamed Shop", first_message_date=date.today() - timedelta(days=2))
        self.profile = _make_user_profile(self.company, "+263771000300")

    def test_sends_shona_template_to_shona_speaking_owner(self):
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_mock)):
                return await maybe_send_unnamed_shop_followup()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])
        send_mock.assert_called_once_with("+263771000300", "ats_unnamed_shop_followup_shona_version")
        self.company.refresh_from_db()
        self.assertTrue(self.company.unnamed_shop_followup_sent)

    def test_sends_english_template_to_english_speaking_owner(self):
        self.profile.language = "en"
        self.profile.save()
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_mock)):
                return await maybe_send_unnamed_shop_followup()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])
        send_mock.assert_called_once_with("+263771000300", "ats_unnamed_shop_followup")

    def test_skips_shop_that_already_has_a_real_name(self):
        self.company.name = "Mai Ati Mini Eatery"
        self.company.save()
        result = _run(maybe_send_unnamed_shop_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_skips_shop_already_sent(self):
        self.company.unnamed_shop_followup_sent = True
        self.company.save()
        result = _run(maybe_send_unnamed_shop_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_skips_inactive_shop(self):
        self.company.active = False
        self.company.save()
        result = _run(maybe_send_unnamed_shop_followup())
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

    def test_skips_shop_already_messaged_today_by_another_mechanism(self):
        """Regression for #262 — otherwise-eligible for reengagement, but
        already messaged today via a different automated nudge mechanism."""
        self._make_sale(days_ago=3)
        self.company.last_automated_message_date = date.today()
        self.company.save()
        result = _run(maybe_send_reengagement())
        self.assertNotIn(self.company.id, result["sent"])

    def test_cross_mechanism_dedup_daily_nudge_then_reengagement_same_day(self):
        """Regression for #262 — the exact scenario confirmed in production:
        a shop eligible for both the daily nudge and reengagement on the same
        day should only receive one message, not both."""
        self._make_sale(days_ago=3)
        with patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=True), \
             patch("apps.whatsapp.services.nudge_service._recently_active", new=AsyncMock(return_value=False)), \
             patch("apps.whatsapp.services.nudge_service.pick_nudge_cta", new=AsyncMock(return_value={"cta_key": "retention_no_sales_today", "message": "What did you sell today?", "cta_type": "text"})), \
             patch("apps.whatsapp.services.nudge_service._send_nudge", new=AsyncMock(return_value=True)):
            nudge_result = _run(maybe_send_nudges())
        self.assertIn(self.company.id, nudge_result["sent"])

        send_mock = AsyncMock(return_value=True)
        async def _run_reengagement():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_client(send_mock)):
                return await maybe_send_reengagement()
        reengagement_result = _run(_run_reengagement())
        self.assertNotIn(self.company.id, reengagement_result["sent"])
        send_mock.assert_not_called()


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
            self.company.last_automated_message_date = None
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


def _mock_message_client(send_mock):
    """Return a mock WhatsApp client with send_message replaced (plain text, not templates)."""
    client = MagicMock()
    client.send_message = send_mock
    return client


class RecordUnhandledButtonTapTest(TransactionTestCase):
    """#209 — capturing a tap on a button with no dedicated handler."""

    def setUp(self):
        self.company = _make_company(first_message_date=date.today() - timedelta(days=20))

    def test_records_tap_timestamp(self):
        self.assertIsNone(self.company.last_unhandled_button_tap_at)
        _run(record_unhandled_button_tap(self.company))
        self.company.refresh_from_db()
        self.assertIsNotNone(self.company.last_unhandled_button_tap_at)

    def test_resets_followup_sent_flag_on_new_tap(self):
        """A shop that already got the tap-followup taps again — the window
        should reopen rather than staying permanently suppressed."""
        self.company.unhandled_tap_followup_sent = True
        self.company.save()
        _run(record_unhandled_button_tap(self.company))
        self.company.refresh_from_db()
        self.assertFalse(self.company.unhandled_tap_followup_sent)


class UnhandledTapFollowupTest(TransactionTestCase):
    """#209 — soft text follow-up when a shop taps a nudge button but records
    no sale within the follow-up window."""

    def setUp(self):
        self.company = _make_company(first_message_date=date.today() - timedelta(days=20))
        self.profile = _make_user_profile(self.company, "+263771000400")

    def _tap(self, hours_ago):
        self.company.last_unhandled_button_tap_at = timezone.now() - timedelta(hours=hours_ago)
        self.company.unhandled_tap_followup_sent = False
        self.company.save()

    def _make_sale(self, hours_ago):
        from apps.sales.models import Sale
        Sale.objects.create(
            company=self.company,
            sale_timestamp=timezone.now() - timedelta(hours=hours_ago),
            total_amount=10,
            status="confirmed",
        )

    def test_sends_followup_to_shop_that_tapped_and_recorded_no_sale(self):
        self._tap(hours_ago=3)
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_message_client(send_mock)):
                return await maybe_send_unhandled_tap_followup()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])
        self.company.refresh_from_db()
        self.assertTrue(self.company.unhandled_tap_followup_sent)
        send_mock.assert_called_once()

    def test_mark_sent_is_a_noop_if_the_tap_changed_since_loading(self):
        """Regression for the re-tap/in-flight-send race: if the tap
        timestamp on the row no longer matches what was loaded (a fresh tap
        landed in between), marking sent must not overwrite that reset —
        otherwise the new tap silently loses its own follow-up window."""
        self._tap(hours_ago=3)
        loaded_company = Company.objects.get(pk=self.company.pk)
        # Simulate a re-tap landing after the candidate was loaded but
        # before the "mark sent" write for the old tap runs.
        self.company.last_unhandled_button_tap_at = timezone.now()
        self.company.unhandled_tap_followup_sent = False
        self.company.save()

        _run(_mark_unhandled_tap_followup_sent(loaded_company))

        self.company.refresh_from_db()
        self.assertFalse(self.company.unhandled_tap_followup_sent)

    def test_mark_sent_applies_when_tap_is_unchanged(self):
        self._tap(hours_ago=3)
        loaded_company = Company.objects.get(pk=self.company.pk)

        _run(_mark_unhandled_tap_followup_sent(loaded_company))

        self.company.refresh_from_db()
        self.assertTrue(self.company.unhandled_tap_followup_sent)

    def test_does_not_mark_sent_on_send_failure(self):
        self._tap(hours_ago=3)
        send_mock = AsyncMock(return_value=False)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_message_client(send_mock)):
                return await maybe_send_unhandled_tap_followup()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["skipped"])
        self.company.refresh_from_db()
        self.assertFalse(self.company.unhandled_tap_followup_sent)

    def test_partial_multi_recipient_success_is_marked_sent_not_retried(self):
        """A shop with two recipients where only one send succeeds must be
        marked sent — not retried — so the successful recipient never gets
        a duplicate. This mechanism uses any-succeeded semantics, unlike the
        all-or-nothing shape of the other four mechanisms in this file (see
        maybe_send_unhandled_tap_followup's docstring for why)."""
        assistant_phone = "+263771000499"
        _make_user_profile(self.company, assistant_phone)
        self._tap(hours_ago=3)

        async def _send_side_effect(phone, message):
            return phone != assistant_phone  # owner succeeds, assistant fails

        send_mock = AsyncMock(side_effect=_send_side_effect)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_message_client(send_mock)):
                return await maybe_send_unhandled_tap_followup()
        result = _run(_run_test())

        self.assertIn(self.company.id, result["sent"])
        self.company.refresh_from_db()
        self.assertTrue(self.company.unhandled_tap_followup_sent)
        self.assertEqual(send_mock.call_count, 2)

    def test_all_recipients_failing_is_not_marked_sent(self):
        assistant_phone = "+263771000498"
        _make_user_profile(self.company, assistant_phone)
        self._tap(hours_ago=3)
        send_mock = AsyncMock(return_value=False)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_message_client(send_mock)):
                return await maybe_send_unhandled_tap_followup()
        result = _run(_run_test())

        self.assertIn(self.company.id, result["skipped"])
        self.company.refresh_from_db()
        self.assertFalse(self.company.unhandled_tap_followup_sent)

    def test_skips_shop_within_the_2_hour_window(self):
        self._tap(hours_ago=1)
        result = _run(maybe_send_unhandled_tap_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_shop_exactly_at_the_2_hour_boundary_is_eligible(self):
        # Pins `<=` vs `<` at the exact boundary, same discipline as #210's
        # CHURNED boundary test. _load_unhandled_tap_followup_candidates and
        # this test each call timezone.now() independently, so an exact-age
        # fixture drifts past the cutoff by the time the function actually
        # runs unless both clocks are anchored to the same frozen instant.
        frozen = timezone.now()
        exact_ts = frozen - timedelta(hours=2)
        self.company.last_unhandled_button_tap_at = exact_ts
        self.company.unhandled_tap_followup_sent = False
        self.company.save()
        send_mock = AsyncMock(return_value=True)

        async def _run_test():
            with patch("apps.whatsapp.services.nudge_service.timezone.now", return_value=frozen), \
                 patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_message_client(send_mock)):
                return await maybe_send_unhandled_tap_followup()

        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])

    def test_shop_just_inside_the_2_hour_boundary_is_not_yet_eligible(self):
        frozen = timezone.now()
        just_inside = frozen - timedelta(hours=2) + timedelta(minutes=1)
        self.company.last_unhandled_button_tap_at = just_inside
        self.company.unhandled_tap_followup_sent = False
        self.company.save()

        async def _run_test():
            with patch("apps.whatsapp.services.nudge_service.timezone.now", return_value=frozen):
                return await maybe_send_unhandled_tap_followup()

        result = _run(_run_test())
        self.assertNotIn(self.company.id, result["sent"])

    def test_skips_shop_with_a_stale_tap_past_24h(self):
        """A tap old enough that a plain-text follow-up would land outside
        Meta's 24h customer-service window must not be retried forever —
        without an upper bound, a failed send never sets
        unhandled_tap_followup_sent, so this shop would be re-attempted
        every 15 min indefinitely."""
        self._tap(hours_ago=30)
        result = _run(maybe_send_unhandled_tap_followup())
        self.assertNotIn(self.company.id, result["sent"])
        self.assertNotIn(self.company.id, result["skipped"])

    def test_sends_to_shop_with_tap_just_inside_the_24h_bound(self):
        self._tap(hours_ago=23)
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_message_client(send_mock)):
                return await maybe_send_unhandled_tap_followup()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])

    def test_skips_shop_that_never_tapped(self):
        result = _run(maybe_send_unhandled_tap_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_skips_shop_that_sold_after_the_tap(self):
        self._tap(hours_ago=3)
        self._make_sale(hours_ago=1)
        result = _run(maybe_send_unhandled_tap_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_sends_to_shop_that_sold_before_the_tap(self):
        """A sale that happened before the tap doesn't count — the tap itself
        proves the shop is still silent afterward."""
        self._make_sale(hours_ago=5)
        self._tap(hours_ago=3)
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_message_client(send_mock)):
                return await maybe_send_unhandled_tap_followup()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])

    def test_skips_shop_already_sent_the_followup(self):
        self._tap(hours_ago=3)
        self.company.unhandled_tap_followup_sent = True
        self.company.save()
        result = _run(maybe_send_unhandled_tap_followup())
        self.assertNotIn(self.company.id, result["sent"])

    def test_sends_even_if_already_messaged_today_by_another_mechanism(self):
        """The tap is a user-initiated inbound interaction, not the two-
        automated-mechanisms-stacking problem #262's gate exists to prevent —
        deliberately NOT gated on _already_messaged_today. The most common
        real trigger is a tap on the daily nudge sent that same morning,
        which already sets last_automated_message_date; if this gate
        applied, the follow-up would be blocked in exactly that case."""
        self._tap(hours_ago=3)
        self.company.last_automated_message_date = date.today()
        self.company.save()
        send_mock = AsyncMock(return_value=True)
        async def _run_test():
            with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_message_client(send_mock)):
                return await maybe_send_unhandled_tap_followup()
        result = _run(_run_test())
        self.assertIn(self.company.id, result["sent"])

    def test_skips_shop_with_all_users_opted_out(self):
        self._tap(hours_ago=3)
        self.profile.nudge_opt_out = True
        self.profile.save()
        result = _run(maybe_send_unhandled_tap_followup())
        self.assertNotIn(self.company.id, result["sent"])


class UnhandledTapFollowupCommandTest(TransactionTestCase):
    """#209 regression — the tap followup must run even outside the daily
    nudge send window (07:00-10:00 CAT), since it's event-triggered and
    time-sensitive, not part of the daily drip the window exists to pace."""

    def setUp(self):
        self.company = _make_company(first_message_date=date.today() - timedelta(days=20))
        self.profile = _make_user_profile(self.company, "+263771000401")
        self.company.last_unhandled_button_tap_at = timezone.now() - timedelta(hours=3)
        self.company.save()

    def test_tap_followup_sends_outside_the_daily_nudge_send_window(self):
        from io import StringIO
        from django.core.management import call_command
        send_mock = AsyncMock(return_value=True)
        with patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=False), \
             patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_message_client(send_mock)):
            out = StringIO()
            call_command("send_nudges", stdout=out)
        output = out.getvalue()
        self.assertIn("Outside nudge send window", output)
        self.assertIn("Tap followups sent:", output)
        self.company.refresh_from_db()
        self.assertTrue(self.company.unhandled_tap_followup_sent)

    def test_daily_drip_mechanisms_stay_suppressed_outside_the_send_window(self):
        """Regression for the exact bug this PR introduced and then fixed:
        moving the tap-followup call above the outside_window return must
        not also free the four daily-drip mechanisms from that window. This
        is the assertion the earlier version of this test was missing —
        without it, a shop eligible for onboarding_followup at 2am would
        have gotten its template fired immediately instead of waiting for
        the 07:00-10:00 window."""
        from io import StringIO
        from django.core.management import call_command
        onboarding_company = _make_company(
            name="Onboarding Followup Candidate",
            first_message_date=date.today() - timedelta(days=3),
        )
        _make_user_profile(onboarding_company, "+263771000402")
        send_mock = AsyncMock(return_value=True)
        with patch("apps.whatsapp.services.nudge_service._in_send_window", return_value=False), \
             patch("django.conf.settings.USE_TEMPLATE_MESSAGES", True), \
             patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=_mock_message_client(send_mock)):
            out = StringIO()
            call_command("send_nudges", stdout=out)
        output = out.getvalue()
        self.assertIn("Outside nudge send window", output)
        self.assertNotIn("Onboarding followups sent:", output)
        self.assertNotIn("Reengagements sent:", output)
        self.assertNotIn("Zero-contact followups sent:", output)
        self.assertNotIn("Unnamed-shop followups sent:", output)
        onboarding_company.refresh_from_db()
        self.assertEqual(onboarding_company.onboarding_followup_count, 0)


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
