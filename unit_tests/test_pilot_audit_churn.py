"""Unit tests for the CHURNED flag in the pilot_audit management command — #210.

Detects shops that had real traction (2+ confirmed sales) and then went
quiet, distinct from SILENT (never messaged, zero sales).
"""

from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Company, UserProfile
from apps.core.services import CHURN_MIN_SALES, CHURN_SILENCE_DAYS
from apps.sales.models import Sale
from apps.whatsapp.models import WhatsAppMessage


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


def _make_message(company, age: timedelta, direction=WhatsAppMessage.Direction.INBOUND):
    message = WhatsAppMessage.objects.create(
        company=company,
        direction=direction,
        message_type=WhatsAppMessage.MessageType.TEXT,
        phone_number=company.members.first().phone_number,
        content="hi",
    )
    WhatsAppMessage.objects.filter(pk=message.pk).update(timestamp=timezone.now() - age)
    return message


def _run_audit():
    out = StringIO()
    call_command("pilot_audit", stdout=out)
    return out.getvalue()


class PilotAuditChurnTest(TestCase):

    def test_flags_shop_with_enough_sales_gone_quiet(self):
        company = _make_company()
        for _ in range(CHURN_MIN_SALES):
            _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=CHURN_SILENCE_DAYS + 1))
        _make_message(company, age=timedelta(days=CHURN_SILENCE_DAYS + 1))

        output = _run_audit()

        self.assertIn(f"CHURNED: {company.name}", output)

    def test_does_not_flag_shop_below_min_sales(self):
        company = _make_company()
        _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=CHURN_SILENCE_DAYS + 1))
        _make_message(company, age=timedelta(days=CHURN_SILENCE_DAYS + 1))

        output = _run_audit()

        self.assertNotIn(f"CHURNED: {company.name}", output)

    def test_does_not_flag_shop_active_within_window(self):
        company = _make_company()
        for _ in range(CHURN_MIN_SALES):
            _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=CHURN_SILENCE_DAYS + 1))
        _make_message(company, age=timedelta(days=1))

        output = _run_audit()

        self.assertNotIn(f"CHURNED: {company.name}", output)

    def test_recent_message_counts_as_active_even_without_recent_sale(self):
        company = _make_company()
        for _ in range(CHURN_MIN_SALES):
            _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=CHURN_SILENCE_DAYS + 10))
        _make_message(company, age=timedelta(days=1))

        output = _run_audit()

        self.assertNotIn(f"CHURNED: {company.name}", output)

    def test_recent_sale_counts_as_active_even_without_recent_message(self):
        company = _make_company()
        for _ in range(CHURN_MIN_SALES):
            _make_sale(company, Sale.Status.CONFIRMED, age=timedelta(days=1))
        _make_message(company, age=timedelta(days=CHURN_SILENCE_DAYS + 10))

        output = _run_audit()

        self.assertNotIn(f"CHURNED: {company.name}", output)

    def test_shop_silent_for_just_under_the_threshold_is_not_yet_churned(self):
        # Pins the strict `<` in pilot_audit.py against the cutoff: a shop whose
        # last activity is just inside CHURN_SILENCE_DAYS is not flagged. (Using
        # exactly CHURN_SILENCE_DAYS is not reliable here — `now` is re-evaluated
        # inside the command, always slightly later than this setup's timestamp,
        # so an exact-boundary age would drift to just past the cutoff by the
        # time the command runs.)
        company = _make_company()
        just_inside = timedelta(days=CHURN_SILENCE_DAYS) - timedelta(minutes=1)
        for _ in range(CHURN_MIN_SALES):
            _make_sale(company, Sale.Status.CONFIRMED, age=just_inside)
        _make_message(company, age=just_inside)

        output = _run_audit()

        self.assertNotIn(f"CHURNED: {company.name}", output)

    def test_never_messaged_shop_is_flagged_silent_not_churned(self):
        company = _make_company()

        output = _run_audit()

        self.assertIn(f"SILENT: {company.name}", output)
        self.assertNotIn(f"CHURNED: {company.name}", output)
