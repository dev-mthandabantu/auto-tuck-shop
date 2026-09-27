import asyncio

import sentry_sdk
from django.core.management.base import BaseCommand

from apps.core.services import NEVER_ALERTED_MINUTES, get_never_alerted_waitlist_entries, mark_stale_alerted
from apps.whatsapp.services.webhook_handler import ADMIN_PHONE_NUMBER, t
from apps.whatsapp.services.whatsapp_client import get_whatsapp_client


class Command(BaseCommand):
    help = (
        "Alert the admin if any waitlist entry has gone unalerted "
        f"{NEVER_ALERTED_MINUTES}+ minutes — see #387"
    )

    def handle(self, *args, **options):
        # Sibling to pilot_activity_alert, not an extension of it — that
        # command's own early-return means folding a second, unrelated
        # condition into it would give one Sentry monitor slug two failure
        # meanings. supercronic runs in UTC (see pilot_activity_alert for
        # detail); this checks every 30 minutes rather than once daily,
        # since NEVER_ALERTED_MINUTES is much shorter than a day.
        monitor_config = {
            "schedule": {"type": "crontab", "value": "*/30 * * * *"},
            "timezone": "UTC",
            "checkin_margin": 5,
            "max_runtime": 5,
            "failure_issue_threshold": 1,
        }
        with sentry_sdk.monitor(monitor_slug="stale-waitlist-alert", monitor_config=monitor_config):
            entries = list(get_never_alerted_waitlist_entries())
            self.stdout.write(f"Never-alerted pending waitlist entries: {len(entries)}")

            if not entries:
                return

            no_name_count = sum(1 for e in entries if not e.company_name)

            with sentry_sdk.new_scope() as scope:
                scope.set_tag("failure_type", "waitlist_admin_never_alerted")
                scope.set_tag("window_minutes", NEVER_ALERTED_MINUTES)
                scope.set_context("waitlist_alert", {
                    "never_alerted_count": len(entries),
                    "no_shop_name_count": no_name_count,
                    "window_minutes": NEVER_ALERTED_MINUTES,
                    "entry_ids": [e.id for e in entries],
                })
                sentry_sdk.capture_message(
                    f"Waitlist: {len(entries)} pending entr{'y' if len(entries) == 1 else 'ies'} "
                    f"never alerted to admin ({no_name_count} with no shop name)",
                    level="error",
                )

            # One WhatsApp per entry, not one per run (#387).
            #
            # TODO(#387): send_message still hits the 24h-window problem —
            # needs template-based sending, held pending root cause.
            unpinged = [e for e in entries if e.stale_alert_sent_at is None]
            if unpinged and ADMIN_PHONE_NUMBER:
                message = t(
                    "waitlist_admin.never_alerted",
                    count=len(unpinged),
                    no_name_count=sum(1 for e in unpinged if not e.company_name),
                )
                client = get_whatsapp_client()
                sent = asyncio.run(client.send_message(ADMIN_PHONE_NUMBER, message))
                if sent:
                    mark_stale_alerted(unpinged)
                else:
                    self.stderr.write("Failed to send waitlist never-alerted WhatsApp alert to admin")
