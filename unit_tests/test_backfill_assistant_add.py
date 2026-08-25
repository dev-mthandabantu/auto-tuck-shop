"""Regression tests for #329: pre-add inbound messages from a phone number
must get linked to the shop once that number is added as an assistant —
the same bug class as #233 (waitlist approval), via a second code path.

Run with:
    python manage.py test unit_tests.test_backfill_assistant_add
"""

import asyncio

from django.contrib.auth.models import User
from django.test import TransactionTestCase

from apps.core.models import Company, UserProfile
from apps.whatsapp.models import WhatsAppMessage
from apps.whatsapp.services.waitlist_handler import _create_assistant


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_company(name="Test Shop", slug="test-shop-assistant-backfill", first_message_date=None):
    return Company.objects.create(name=name, slug=slug, active=True, first_message_date=first_message_date)


def _make_orphaned_message(phone, content="hi"):
    return WhatsAppMessage.objects.create(
        direction=WhatsAppMessage.Direction.INBOUND,
        message_type=WhatsAppMessage.MessageType.TEXT,
        phone_number=phone,
        content=content,
        company=None,
    )


class CreateAssistantBackfillTest(TransactionTestCase):
    # _create_assistant is @db_sync_to_async — its DB work runs on a separate
    # thread with its own connection. A plain TestCase's per-test transaction
    # isn't committed, so it's invisible to that other thread (same reason
    # documented in test_sale_confirmation.py for _get_confirmed_sale).
    def test_links_orphaned_messages_when_creating_new_assistant(self):
        phone = "+263771234580"
        msg = _make_orphaned_message(phone, "hi")
        company = _make_company(first_message_date="2026-01-01")

        profile = _run(_create_assistant(phone, company))

        msg.refresh_from_db()
        self.assertEqual(msg.company_id, company.id)
        self.assertEqual(msg.user_profile_id, profile.id)
        self.assertEqual(profile.role, UserProfile.Role.ASSISTANT)

    def test_links_orphaned_messages_when_reactivating_existing_assistant(self):
        """A previously-removed assistant's row is reactivated, not recreated
        (see the existing comment in _create_assistant) — backfill must still
        run on that path, not just the brand-new-profile path."""
        phone = "+263771234581"
        old_company = _make_company(name="Old Shop", slug="old-shop-reactivate", first_message_date="2026-01-01")
        user = User.objects.create_user(username=phone, password="x", is_active=False)
        UserProfile.objects.create(
            user=user, company=old_company, phone_number=phone, role=UserProfile.Role.ASSISTANT
        )
        msg = _make_orphaned_message(phone, "hi again")
        new_company = _make_company(name="New Shop", slug="new-shop-reactivate", first_message_date="2026-02-01")

        profile = _run(_create_assistant(phone, new_company))

        msg.refresh_from_db()
        self.assertEqual(msg.company_id, new_company.id)
        self.assertEqual(msg.user_profile_id, profile.id)

    def test_does_not_set_first_message_date_from_assistant_history(self):
        """Deliberate: an assistant's own pre-add message history is not the
        shop's history — must not backdate the company's first_message_date
        from it, even when the message predates the company's own date."""
        phone = "+263771234582"
        _make_orphaned_message(phone, "old unrelated message")
        company = _make_company(first_message_date="2026-06-01")

        _run(_create_assistant(phone, company))

        company.refresh_from_db()
        self.assertEqual(str(company.first_message_date), "2026-06-01")

    def test_does_not_touch_messages_from_a_different_phone(self):
        own_phone = "+263771234583"
        other_phone = "+263771234584"
        own_msg = _make_orphaned_message(own_phone)
        other_msg = _make_orphaned_message(other_phone)
        company = _make_company(first_message_date="2026-01-01")

        _run(_create_assistant(own_phone, company))

        own_msg.refresh_from_db()
        other_msg.refresh_from_db()
        self.assertIsNotNone(own_msg.company_id)
        self.assertIsNone(other_msg.company_id)

    def test_no_orphaned_messages_is_a_safe_no_op(self):
        company = _make_company(first_message_date="2026-01-01")
        profile = _run(_create_assistant("+263771234585", company))
        self.assertIsNotNone(profile)
        self.assertEqual(profile.role, UserProfile.Role.ASSISTANT)
