"""Regression tests for #233: pre-approval inbound messages (recorded with
company=None, since no Company existed yet) must get linked to the shop
once it's approved.

Covers both halves of the fix:
1. approve_waitlist_entry() now backfills automatically at approval time,
   so this can't recur for future shops.
2. The backfill_orphaned_messages management command cleans up messages
   that were already orphaned before the permanent fix shipped.

Run with:
    python manage.py test unit_tests.test_backfill_orphaned_messages
"""

import datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Company, UserProfile, WaitlistEntry
from apps.core.services import approve_waitlist_entry
from apps.whatsapp.models import WhatsAppMessage


def _make_orphaned_message(phone, content="hi", days_ago=None, direction=WhatsAppMessage.Direction.INBOUND):
    msg = WhatsAppMessage.objects.create(
        direction=direction,
        message_type=WhatsAppMessage.MessageType.TEXT,
        phone_number=phone,
        content=content,
        company=None,
    )
    if days_ago is not None:
        # timestamp is auto_now_add=True — Django stamps it with "now" on
        # create and ignores any value passed in, so backdate it afterward
        # via a direct update (auto_now_add only fires on save(), not update()).
        WhatsAppMessage.objects.filter(pk=msg.pk).update(
            timestamp=timezone.now() - datetime.timedelta(days=days_ago)
        )
        msg.refresh_from_db()
    return msg


class ApproveWaitlistEntryBackfillTest(TestCase):
    """The permanent fix — backfill happens automatically on approval."""

    def test_links_orphaned_messages_from_same_phone_on_approval(self):
        phone = "+263771234567"
        msg1 = _make_orphaned_message(phone, "hi")
        msg2 = _make_orphaned_message(phone, "my shop is called Test Shop")
        entry = WaitlistEntry.objects.create(
            phone_number=phone, company_name="Test Shop", language="sn"
        )

        company, profile = approve_waitlist_entry(entry)

        msg1.refresh_from_db()
        msg2.refresh_from_db()
        self.assertEqual(msg1.company_id, company.id)
        self.assertEqual(msg2.company_id, company.id)
        self.assertEqual(msg1.user_profile_id, profile.id)
        self.assertEqual(msg2.user_profile_id, profile.id)

    def test_does_not_touch_messages_from_a_different_phone(self):
        """Safety check: approving one shop must not accidentally link
        another phone number's orphaned messages."""
        own_phone = "+263771234568"
        other_phone = "+263771234569"
        own_msg = _make_orphaned_message(own_phone)
        other_msg = _make_orphaned_message(other_phone)
        entry = WaitlistEntry.objects.create(
            phone_number=own_phone, company_name="Another Shop", language="sn"
        )

        approve_waitlist_entry(entry)

        own_msg.refresh_from_db()
        other_msg.refresh_from_db()
        self.assertIsNotNone(own_msg.company_id)
        self.assertIsNone(other_msg.company_id)

    def test_no_orphaned_messages_is_a_safe_no_op(self):
        """A shop with no pre-approval messages at all must approve normally."""
        entry = WaitlistEntry.objects.create(
            phone_number="+263771234570", company_name="Quiet Shop", language="sn"
        )
        company, profile = approve_waitlist_entry(entry)
        self.assertIsNotNone(company)
        self.assertIsNotNone(profile)

    def test_first_message_date_set_from_earliest_backfilled_message(self):
        """Regression for PR #317 review: must use the message's real historical
        date, not today's date — these messages happened in the past."""
        phone = "+263771234573"
        _make_orphaned_message(phone, "hi", days_ago=10)
        _make_orphaned_message(phone, "my shop name", days_ago=8)
        entry = WaitlistEntry.objects.create(
            phone_number=phone, company_name="Backdated Shop", language="sn"
        )

        company, profile = approve_waitlist_entry(entry)

        expected_date = timezone.localdate(timezone.now() - datetime.timedelta(days=10))
        self.assertEqual(company.first_message_date, expected_date)
        self.assertNotEqual(company.first_message_date, timezone.localdate())

    def test_first_message_date_ignores_outbound_orphaned_messages(self):
        """Only the shop owner's own inbound messages count as 'first message' —
        an outbound onboarding prompt sent before approval must not set the date."""
        phone = "+263771234574"
        _make_orphaned_message(phone, "outbound prompt", days_ago=15, direction=WhatsAppMessage.Direction.OUTBOUND)
        _make_orphaned_message(phone, "real first message", days_ago=5, direction=WhatsAppMessage.Direction.INBOUND)
        entry = WaitlistEntry.objects.create(
            phone_number=phone, company_name="Mixed Direction Shop", language="sn"
        )

        company, profile = approve_waitlist_entry(entry)

        expected_date = timezone.localdate(timezone.now() - datetime.timedelta(days=5))
        self.assertEqual(company.first_message_date, expected_date)


class BackfillOrphanedMessagesCommandTest(TestCase):
    """The one-time cleanup command for messages orphaned before the fix shipped."""

    def setUp(self):
        self.company = Company.objects.create(name="Existing Shop", slug="existing-shop", active=True)
        from django.contrib.auth.models import User
        user = User.objects.create_user(username="+263771234571", password="x")
        self.profile = UserProfile.objects.create(
            user=user,
            company=self.company,
            phone_number="+263771234571",
            role=UserProfile.Role.OWNER,
        )

    def test_links_existing_orphaned_messages(self):
        msg = _make_orphaned_message(self.profile.phone_number)

        out = StringIO()
        call_command("backfill_orphaned_messages", stdout=out)

        msg.refresh_from_db()
        self.assertEqual(msg.company_id, self.company.id)
        self.assertEqual(msg.user_profile_id, self.profile.id)
        self.assertIn("Linked 1 orphaned message", out.getvalue())

    def test_dry_run_does_not_write(self):
        msg = _make_orphaned_message(self.profile.phone_number)

        out = StringIO()
        call_command("backfill_orphaned_messages", "--dry-run", stdout=out)

        msg.refresh_from_db()
        self.assertIsNone(msg.company_id)
        self.assertIn("DRY RUN", out.getvalue())

    def test_unrelated_phone_number_is_not_linked(self):
        unrelated = _make_orphaned_message("+263771234572")

        call_command("backfill_orphaned_messages", stdout=StringIO())

        unrelated.refresh_from_db()
        self.assertIsNone(unrelated.company_id)

    def test_sets_first_message_date_from_earliest_message(self):
        _make_orphaned_message(self.profile.phone_number, "hi", days_ago=20)

        call_command("backfill_orphaned_messages", stdout=StringIO())

        self.company.refresh_from_db()
        expected_date = timezone.localdate(timezone.now() - datetime.timedelta(days=20))
        self.assertEqual(self.company.first_message_date, expected_date)

    def test_does_not_overwrite_an_already_set_first_message_date(self):
        """A shop that was already approved and already has first_message_date
        set (from a normal post-approval message) must keep its real value,
        not get overwritten by an older pre-approval message discovered later."""
        existing_date = timezone.localdate() - datetime.timedelta(days=2)
        self.company.first_message_date = existing_date
        self.company.save(update_fields=["first_message_date"])
        _make_orphaned_message(self.profile.phone_number, "older message", days_ago=30)

        call_command("backfill_orphaned_messages", stdout=StringIO())

        self.company.refresh_from_db()
        self.assertEqual(self.company.first_message_date, existing_date)

    def test_dry_run_does_not_set_first_message_date(self):
        _make_orphaned_message(self.profile.phone_number, "hi", days_ago=5)

        call_command("backfill_orphaned_messages", "--dry-run", stdout=StringIO())

        self.company.refresh_from_db()
        self.assertIsNone(self.company.first_message_date)
