import asyncio

from django.core.management.base import BaseCommand

from apps.whatsapp.services.pilot_health import build_pilot_health_snapshot, send_pilot_health_alert


class Command(BaseCommand):
    help = "Alert when real pilot shop activity drops below the expected threshold"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Lookback window in days (default: 7)",
        )
        parser.add_argument(
            "--min-active-shops",
            type=int,
            default=1,
            help="Minimum real shops with at least one confirmed sale required before alerting",
        )

    def handle(self, *args, **options):
        days = options["days"]
        min_active_shops = options["min_active_shops"]

        snapshot = build_pilot_health_snapshot(days=days, min_active_shops=min_active_shops)
        self.stdout.write(
            f"Pilot health: {snapshot['active_count']}/{snapshot['real_shop_count']} real shops active "
            f"in the last {days} days"
        )

        if not snapshot["alert_needed"]:
            self.stdout.write(self.style.SUCCESS("Pilot health is within the expected threshold."))
            return

        alert_result = asyncio.run(send_pilot_health_alert(snapshot))
        self.stdout.write(
            self.style.WARNING(
                f"Alert sent via {alert_result['channel']}: {alert_result['message'].splitlines()[0]}"
            )
        )