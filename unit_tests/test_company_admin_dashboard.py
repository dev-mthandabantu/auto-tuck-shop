"""Tests for the Company admin dashboard (#274): last-activity, nudge status,
and the "needs attention" flag/filter surfaced in apps/core/admin.py.

Run with:
    python manage.py test unit_tests.test_company_admin_dashboard
"""

import datetime

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from apps.core.admin import CompanyAdmin, NeedsAttentionFilter, UnnamedShopFilter, NEEDS_ATTENTION_DAYS
from apps.core.models import Company, UserProfile
from apps.core.pilot_test_phones import PILOT_TEST_PHONES
from apps.sales.models import Sale
from apps.whatsapp.models import WhatsAppMessage
from django.contrib import admin as django_admin
from django.utils import timezone


def _make_company(**kwargs):
    defaults = {"name": "Test Shop", "slug": "test-shop-admin-dash", "active": True}
    defaults.update(kwargs)
    return Company.objects.create(**defaults)


def _make_owner(company, phone="+263771111111"):
    user = User.objects.create_user(username=phone, password="x")
    return UserProfile.objects.create(
        user=user, company=company, phone_number=phone, role=UserProfile.Role.OWNER
    )


def _make_second_owner(company, phone):
    """UserProfile has no DB constraint limiting a company to one OWNER-role profile."""
    return _make_owner(company, phone=phone)


def _make_sale(company, days_ago):
    ts = timezone.now() - datetime.timedelta(days=days_ago)
    return Sale.objects.create(company=company, sale_timestamp=ts, status=Sale.Status.CONFIRMED)


def _make_inbound_message(company, days_ago):
    msg = WhatsAppMessage.objects.create(
        direction=WhatsAppMessage.Direction.INBOUND,
        message_type=WhatsAppMessage.MessageType.TEXT,
        phone_number="+263771111111",
        content="hi",
        company=company,
    )
    # timestamp is auto_now_add — backdate it directly.
    WhatsAppMessage.objects.filter(pk=msg.pk).update(
        timestamp=timezone.now() - datetime.timedelta(days=days_ago)
    )
    return msg


class CompanyAdminAnnotationTest(TestCase):
    def setUp(self):
        self.admin = CompanyAdmin(Company, django_admin.site)
        self.request = RequestFactory().get("/admin/core/company/")

    def _annotated(self, company):
        return self.admin.get_queryset(self.request).get(pk=company.pk)

    def test_last_confirmed_sale_and_inbound_message_annotated(self):
        company = _make_company()
        _make_sale(company, days_ago=3)
        _make_inbound_message(company, days_ago=1)

        obj = self._annotated(company)
        self.assertIsNotNone(obj._last_confirmed_sale)
        self.assertIsNotNone(obj._last_inbound_message)
        self.assertEqual(self.admin.last_confirmed_sale(obj), obj._last_confirmed_sale)
        self.assertEqual(self.admin.last_inbound_message(obj), obj._last_inbound_message)

    def test_never_active_shows_never(self):
        company = _make_company(slug="never-active")
        obj = self._annotated(company)
        self.assertEqual(self.admin.last_confirmed_sale(obj), "never")
        self.assertEqual(self.admin.last_inbound_message(obj), "never")

    def test_only_pending_sale_does_not_count_as_confirmed(self):
        """Sales start PENDING and only count once confirmed — a pending-only shop is still 'never'."""
        company = _make_company(slug="pending-only")
        Sale.objects.create(company=company, status=Sale.Status.PENDING)
        obj = self._annotated(company)
        self.assertEqual(self.admin.last_confirmed_sale(obj), "never")


class NudgeStatusTest(TestCase):
    def setUp(self):
        self.admin = CompanyAdmin(Company, django_admin.site)

    def test_never_nudged(self):
        company = _make_company(slug="never-nudged")
        self.assertIn("never nudged", self.admin.nudge_status(company))
        self.assertIn("stage 0", self.admin.nudge_status(company))

    def test_nudged_shows_stage_and_date(self):
        company = _make_company(
            slug="nudged",
            nudge_stage=2,
            last_nudge_date=timezone.localdate(),
        )
        status = self.admin.nudge_status(company)
        self.assertIn("stage 2", status)
        self.assertIn(str(timezone.localdate()), status)


class NeedsAttentionFlagTest(TestCase):
    def setUp(self):
        self.admin = CompanyAdmin(Company, django_admin.site)
        self.request = RequestFactory().get("/admin/core/company/")

    def _annotated(self, company):
        return self.admin.get_queryset(self.request).get(pk=company.pk)

    def test_silent_onboarded_shop_flagged(self):
        company = _make_company(
            slug="silent-shop",
            first_message_date=timezone.localdate() - datetime.timedelta(days=NEEDS_ATTENTION_DAYS + 5),
        )
        _make_owner(company)
        _make_sale(company, days_ago=NEEDS_ATTENTION_DAYS + 3)
        obj = self._annotated(company)
        self.assertTrue(self.admin.needs_attention_flag(obj))

    def test_recently_active_shop_not_flagged(self):
        company = _make_company(
            slug="active-shop",
            first_message_date=timezone.localdate() - datetime.timedelta(days=30),
        )
        _make_owner(company)
        _make_sale(company, days_ago=1)
        obj = self._annotated(company)
        self.assertFalse(self.admin.needs_attention_flag(obj))

    def test_never_messaged_shop_not_flagged(self):
        """No first_message_date → not yet onboarded; that's the zero-contact-followup case, not this flag."""
        company = _make_company(slug="never-messaged", first_message_date=None)
        _make_owner(company)
        obj = self._annotated(company)
        self.assertFalse(self.admin.needs_attention_flag(obj))

    def test_inactive_shop_not_flagged(self):
        company = _make_company(
            slug="inactive-shop",
            active=False,
            first_message_date=timezone.localdate() - datetime.timedelta(days=30),
        )
        _make_owner(company)
        obj = self._annotated(company)
        self.assertFalse(self.admin.needs_attention_flag(obj))

    def test_test_account_excluded(self):
        test_phone = next(iter(PILOT_TEST_PHONES))
        company = _make_company(
            slug="test-account-shop",
            first_message_date=timezone.localdate() - datetime.timedelta(days=30),
        )
        _make_owner(company, phone=test_phone)
        obj = self._annotated(company)
        self.assertFalse(self.admin.needs_attention_flag(obj))

    def test_multi_owner_excluded_if_any_owner_is_test_phone(self):
        """A company can have more than one OWNER-role profile (no DB constraint prevents it).
        If ANY owner matches a known test number, the shop must be excluded — regardless of
        which owner .first() would have picked under the old per-row-query implementation."""
        test_phone = next(iter(PILOT_TEST_PHONES))
        company = _make_company(
            slug="multi-owner-shop",
            first_message_date=timezone.localdate() - datetime.timedelta(days=30),
        )
        # Real owner sorts first alphabetically by username/phone ("+1..." < "+9...")
        _make_owner(company, phone="+15550000000")
        _make_second_owner(company, phone=test_phone if test_phone > "+15550000000" else "+99999999999")
        _make_sale(company, days_ago=NEEDS_ATTENTION_DAYS + 3)
        obj = self._annotated(company)
        self.assertFalse(self.admin.needs_attention_flag(obj))

    def test_needs_attention_flag_does_not_query_per_row(self):
        """Regression for the N+1 in the old .first()-based implementation (#274 review)."""
        for i in range(3):
            company = _make_company(
                slug=f"no-n-plus-one-{i}",
                first_message_date=timezone.localdate() - datetime.timedelta(days=NEEDS_ATTENTION_DAYS + 5),
            )
            _make_owner(company, phone=f"+2637711111{i:02d}")

        objs = list(self.admin.get_queryset(self.request).filter(slug__startswith="no-n-plus-one-"))
        with self.assertNumQueries(0):
            for obj in objs:
                self.admin.needs_attention_flag(obj)


class NeedsAttentionFilterTest(TestCase):
    def setUp(self):
        self.admin = CompanyAdmin(Company, django_admin.site)
        self.request = RequestFactory().get("/admin/core/company/", {"needs_attention": "yes"})

    def test_filter_returns_only_silent_shops(self):
        silent = _make_company(
            slug="filter-silent",
            first_message_date=timezone.localdate() - datetime.timedelta(days=NEEDS_ATTENTION_DAYS + 5),
        )
        _make_owner(silent, phone="+263771111112")

        active = _make_company(
            slug="filter-active",
            first_message_date=timezone.localdate() - datetime.timedelta(days=30),
        )
        _make_owner(active, phone="+263771111113")
        _make_sale(active, days_ago=1)

        base_qs = self.admin.get_queryset(self.request)
        # SimpleListFilter expects a QueryDict (request.GET.copy()) — it pops the
        # parameter as a list and takes the last element, unlike a plain dict.
        filtered = NeedsAttentionFilter(self.request, self.request.GET.copy(), Company, self.admin)
        results = list(filtered.queryset(self.request, base_qs))

        self.assertIn(silent.pk, [c.pk for c in results])
        self.assertNotIn(active.pk, [c.pk for c in results])

    def test_column_and_filter_agree_on_multi_owner_test_shop(self):
        """The column (needs_attention_flag) and the filter (NeedsAttentionFilter) must reach
        the same verdict for a company with two OWNER-role profiles, one a test number — they
        previously used different exclusion logic (.first() vs. an any-match .exclude() join)
        and could disagree depending on alphabetical phone sort order (#274 review)."""
        test_phone = next(iter(PILOT_TEST_PHONES))
        company = _make_company(
            slug="multi-owner-filter-shop",
            first_message_date=timezone.localdate() - datetime.timedelta(days=NEEDS_ATTENTION_DAYS + 5),
        )
        _make_owner(company, phone="+15550000001")
        _make_second_owner(company, phone=test_phone)

        obj = self.admin.get_queryset(self.request).get(pk=company.pk)
        flag_result = self.admin.needs_attention_flag(obj)

        base_qs = self.admin.get_queryset(self.request)
        filtered = NeedsAttentionFilter(self.request, self.request.GET.copy(), Company, self.admin)
        filter_includes_it = company.pk in [c.pk for c in filtered.queryset(self.request, base_qs)]

        self.assertFalse(flag_result)
        self.assertFalse(filter_includes_it)
        self.assertEqual(flag_result, filter_includes_it)


class NeedsNameFlagTest(TestCase):
    """#316: flag/filter for shops still stuck on the "Unnamed Shop" placeholder."""

    def setUp(self):
        self.admin = CompanyAdmin(Company, django_admin.site)

    def test_unnamed_shop_flagged(self):
        company = _make_company(slug="still-unnamed", name="Unnamed Shop")
        self.assertTrue(self.admin.needs_name_flag(company))

    def test_named_shop_not_flagged(self):
        company = _make_company(slug="has-a-name", name="Mai Ati Mini Eatery")
        self.assertFalse(self.admin.needs_name_flag(company))

    def test_filter_returns_only_unnamed_shops(self):
        unnamed = _make_company(slug="filter-unnamed", name="Unnamed Shop")
        named = _make_company(slug="filter-named", name="Mai Ati Mini Eatery")

        request = RequestFactory().get("/admin/core/company/", {"needs_name": "yes"})
        base_qs = self.admin.get_queryset(request)
        filtered = UnnamedShopFilter(request, request.GET.copy(), Company, self.admin)
        results = [c.pk for c in filtered.queryset(request, base_qs)]

        self.assertIn(unnamed.pk, results)
        self.assertNotIn(named.pk, results)
