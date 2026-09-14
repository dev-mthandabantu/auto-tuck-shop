import asyncio

import sentry_sdk
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.whatsapp.services.nudge_service import (
    maybe_send_nudges,
    maybe_send_onboarding_followup,
    maybe_send_reengagement,
    maybe_send_unhandled_tap_followup,
    maybe_send_unnamed_shop_followup,
    maybe_send_zero_contact_followup,
)


class Command(BaseCommand):
    help = "Send daily nudge messages to shops that have not recorded sales recently"

    def handle(self, *args, **options):
        monitor_config = {
            "schedule": {"type": "crontab", "value": "*/15 * * * *"},
            "timezone": "Africa/Harare",
            "checkin_margin": 2,
            "max_runtime": 5,
            "failure_issue_threshold": 1,
        }
        with sentry_sdk.monitor(monitor_slug="send-nudges", monitor_config=monitor_config):
            # Event-triggered by an inbound tap, not part of the daily drip
            # the send window paces — deliberately not window-gated (#209).
            # Must run before the outside_window return below, or it only
            # fires ~3h/day like everything else in this command.
            tap_followup = asyncio.run(maybe_send_unhandled_tap_followup())
            self.stdout.write(
                f"Tap followups sent: {tap_followup['sent']}, skipped: {tap_followup['skipped']}"
            )

            result = asyncio.run(maybe_send_nudges())
            reason = result.get("reason", "")
            if reason == "outside_window":
                self.stdout.write("Outside nudge send window — nothing sent")
                return
            self.stdout.write(
                f"Nudges sent: {result['sent']}, skipped: {result['skipped']}"
            )

            if settings.USE_TEMPLATE_MESSAGES:
                followup = asyncio.run(maybe_send_onboarding_followup())
                self.stdout.write(
                    f"Onboarding followups sent: {followup['sent']}, skipped: {followup['skipped']}"
                )
                reengagement = asyncio.run(maybe_send_reengagement())
                self.stdout.write(
                    f"Reengagements sent: {reengagement['sent']}, skipped: {reengagement['skipped']}"
                )
                zero_contact = asyncio.run(maybe_send_zero_contact_followup())
                self.stdout.write(
                    f"Zero-contact followups sent: {zero_contact['sent']}, skipped: {zero_contact['skipped']}"
                )
                unnamed_shop = asyncio.run(maybe_send_unnamed_shop_followup())
                self.stdout.write(
                    f"Unnamed-shop followups sent: {unnamed_shop['sent']}, skipped: {unnamed_shop['skipped']}"
                )
