import asyncio

import sentry_sdk
from django.core.management.base import BaseCommand

from apps.core.services import PILOT_SILENCE_WINDOW_DAYS, count_active_pilot_shops
from apps.whatsapp.services.webhook_handler import ADMIN_PHONE_NUMBER, t
from apps.whatsapp.services.whatsapp_client import get_whatsapp_client


class Command(BaseCommand):
    help = "Alert the admin if no real pilot shop has recorded a sale recently — see #273"

    def handle(self, *args, **options):
        # supercronic (the crontab runner, see fly.toml) has no TZ env var set
        # and runs in the container's default UTC — so the `crontab` file entry
        # for this command is "0 6 * * *" (06:00 UTC), which is 08:00 Africa/Harare.
        # This monitor_config declares the *effective local* schedule Sentry
        # should expect; keep it in sync with the crontab entry if either changes.
        monitor_config = {
            "schedule": {"type": "crontab", "value": "0 8 * * *"},
            "timezone": "Africa/Harare",
            "checkin_margin": 5,
            "max_runtime": 5,
            "failure_issue_threshold": 1,
        }
        with sentry_sdk.monitor(monitor_slug="pilot-activity-alert", monitor_config=monitor_config):
            active_shops = count_active_pilot_shops(days=PILOT_SILENCE_WINDOW_DAYS)
            self.stdout.write(
                f"Active pilot shops in last {PILOT_SILENCE_WINDOW_DAYS} days: {active_shops}"
            )

            if active_shops > 0:
                return

            with sentry_sdk.new_scope() as scope:
                scope.set_tag("failure_type", "pilot_activity_silence")
                scope.set_tag("window_days", PILOT_SILENCE_WINDOW_DAYS)
                scope.set_context("pilot_activity", {
                    "active_shops": active_shops,
                    "window_days": PILOT_SILENCE_WINDOW_DAYS,
                })
                sentry_sdk.capture_message(
                    f"Pilot silent: 0 real shops recorded a sale in the last "
                    f"{PILOT_SILENCE_WINDOW_DAYS} days",
                    level="error",
                )

            if ADMIN_PHONE_NUMBER:
                message = t("pilot_admin.silence_alert", days=PILOT_SILENCE_WINDOW_DAYS)
                client = get_whatsapp_client()
                sent = asyncio.run(client.send_message(ADMIN_PHONE_NUMBER, message))
                if not sent:
                    self.stderr.write("Failed to send pilot silence WhatsApp alert to admin")
