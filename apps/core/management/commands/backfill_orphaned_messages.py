"""One-time cleanup for pre-approval inbound messages that were never linked
to a Company once the shop was approved (see #233).

The permanent fix lives in apps.core.services.approve_waitlist_entry, which
now does this automatically at approval time going forward. This command
only exists to clean up messages orphaned before that fix shipped.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import UserProfile
from apps.whatsapp.models import WhatsAppMessage


class Command(BaseCommand):
    help = "Link pre-approval inbound messages (company=None) to the shop that now owns their phone number"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be updated without writing to the database",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        total_linked = 0

        for profile in UserProfile.objects.select_related("company").all():
            orphaned = WhatsAppMessage.objects.filter(
                phone_number=profile.phone_number, company__isnull=True
            )

            if dry_run:
                count = orphaned.count()
                if count == 0:
                    continue
                total_linked += count
                self.stdout.write(
                    f"[DRY RUN] {profile.company.name} "
                    f"({profile.phone_number}): would link {count} orphaned message(s)"
                )
                continue

            earliest_inbound = (
                orphaned.filter(direction=WhatsAppMessage.Direction.INBOUND)
                .order_by("timestamp")
                .values_list("timestamp", flat=True)
                .first()
            )
            count = orphaned.update(company=profile.company, user_profile=profile)
            if count == 0:
                continue

            total_linked += count
            self.stdout.write(
                f"{profile.company.name} ({profile.phone_number}): linked {count} orphaned message(s)"
            )

            # Same first_message_date backfill as approve_waitlist_entry (#233
            # review) — use the message's real historical date, not today's.
            if earliest_inbound and not profile.company.first_message_date:
                profile.company.first_message_date = timezone.localdate(earliest_inbound)
                profile.company.save(update_fields=["first_message_date"])

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN — would link {total_linked} message(s). "
                    "Re-run without --dry-run to apply."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Linked {total_linked} orphaned message(s)."))
