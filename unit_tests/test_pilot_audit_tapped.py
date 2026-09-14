"""Unit tests for the TAPPED, NO SALE flag in the pilot_audit management
command — #209.

Surfaces shops that tapped a button with no dedicated handler (most
commonly a quick-reply on a Meta-approved template) but have not recorded a
confirmed sale since — the issue's "at minimum, flag it" fallback for a
human to follow up while the shop is still warm.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Company, UserProfile
from apps.sales.models import Sale


def _make_company(name="Test Shop", owner_phone="+263700000001", active=True):
    company = Company.objects.create(name=name, slug=name.lower().replace(" ", "-"), active=active)
    user = User.objects.create_user(username=owner_phone.replace("+", ""))
    UserProfile.objects.create(
        user=user, company=company, role=UserProfile.Role.OWNER, phone_number=owner_phone
    )
    return company


def _make_sale(company, status, age: timedelta):
    sale = Sale.objects.create(company=company, status=status)
    Sale.objects.filter(pk=sale.pk).update(sale_timestamp=timezone.now() - age)
    return sale


def _run_audit():
    out = StringIO()
    call_command("pilot_audit", stdout=out)
    return out.getvalue()


class PilotAuditTappedTest(TestCase):

    def test_flags_shop_that_tapped_and_has_not_sold(self):
        company = _make_company()
        company.last_unhandled_button_tap_at = timezone.now() - timedelta(hours=3)
        company.unhandled_tap_followup_sent = False
        company.save()

        output = _run_audit()

        self.assertIn(f"TAPPED, NO SALE: {company.name}", output)

    def test_does_not_flag_shop_that_never_tapped(self):
        company = _make_company()

        output = _run_audit()

        self.assertNotIn(f"TAPPED, NO SALE: {company.name}", output)

    def test_does_not_flag_shop_that_sold_after_the_tap(self):
        company = _make_company()
        company.last_unhandled_button_tap_at = timezone.now() - timedelta(hours=3)
        company.unhandled_tap_followup_sent = False
        company.save()
        _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(hours=1))

        output = _run_audit()

        self.assertNotIn(f"TAPPED, NO SALE: {company.name}", output)

    def test_flags_shop_that_sold_before_the_tap(self):
        """A sale that happened before the tap doesn't count — the tap
        itself proves the shop went quiet again afterward."""
        company = _make_company()
        _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(hours=5))
        company.last_unhandled_button_tap_at = timezone.now() - timedelta(hours=3)
        company.unhandled_tap_followup_sent = False
        company.save()

        output = _run_audit()

        self.assertIn(f"TAPPED, NO SALE: {company.name}", output)

    def test_does_not_flag_shop_once_followup_already_sent(self):
        """Once the automated follow-up has fired (or a fresh tap hasn't
        reset the flag), the audit stops surfacing it — pilot_audit and the
        automated mechanism share the same unhandled_tap_followup_sent
        field by design."""
        company = _make_company()
        company.last_unhandled_button_tap_at = timezone.now() - timedelta(hours=3)
        company.unhandled_tap_followup_sent = True
        company.save()

        output = _run_audit()

        self.assertNotIn(f"TAPPED, NO SALE: {company.name}", output)

    def test_does_not_flag_inactive_company(self):
        company = _make_company(active=False)
        company.last_unhandled_button_tap_at = timezone.now() - timedelta(hours=3)
        company.unhandled_tap_followup_sent = False
        company.save()

        output = _run_audit()

        self.assertNotIn(f"TAPPED, NO SALE: {company.name}", output)

    def test_flag_has_no_time_threshold_unlike_the_automated_followup(self):
        """Deliberate asymmetry (see docs/Architecture.md): the automated
        text follow-up waits 2h before firing, but the audit is a human-read
        report, so a tap from a few minutes ago is still worth surfacing —
        no minimum age is applied here."""
        company = _make_company()
        company.last_unhandled_button_tap_at = timezone.now() - timedelta(minutes=10)
        company.unhandled_tap_followup_sent = False
        company.save()

        output = _run_audit()

        self.assertIn(f"TAPPED, NO SALE: {company.name}", output)

    def test_shown_together_with_churned_when_both_apply(self):
        """TAPPED, NO SALE and CHURNED read independent fields
        (last_unhandled_button_tap_at vs last inbound message / sale) and
        aren't mutually exclusive the way SILENT and CHURNED are — a shop
        can trip both in the same audit run."""
        from apps.core.services import CHURN_MIN_SALES, CHURN_SILENCE_DAYS
        company = _make_company()
        for _ in range(CHURN_MIN_SALES):
            _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=CHURN_SILENCE_DAYS + 1))
        company.last_unhandled_button_tap_at = timezone.now() - timedelta(hours=3)
        company.unhandled_tap_followup_sent = False
        company.save()

        output = _run_audit()

        self.assertIn(f"TAPPED, NO SALE: {company.name}", output)
        self.assertIn(f"CHURNED: {company.name}", output)
