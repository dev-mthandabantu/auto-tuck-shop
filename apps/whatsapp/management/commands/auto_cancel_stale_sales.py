import sentry_sdk
from django.core.management.base import BaseCommand

from apps.sales.services import auto_cancel_stale_pending_sales


class Command(BaseCommand):
    help = "Auto-cancel PENDING sales the owner never confirmed or rejected within 24h"

    def handle(self, *args, **options):
        monitor_config = {
            "schedule": {"type": "crontab", "value": "*/15 * * * *"},
            "timezone": "Africa/Harare",
            "checkin_margin": 2,
            "max_runtime": 5,
            "failure_issue_threshold": 1,
        }
        with sentry_sdk.monitor(monitor_slug="auto-cancel-stale-sales", monitor_config=monitor_config):
            cancelled_count = auto_cancel_stale_pending_sales()
            self.stdout.write(f"Auto-cancelled stale sales: {cancelled_count}")
