from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import Company, UserProfile, WaitlistEntry
from apps.sales.models import Sale
from apps.whatsapp.models import WhatsAppMessage

TEST_PHONES = {
    "+27644178150",
    "+27610869293",
    "+27641295093",  # Admin — Brighton's own test/admin number
    "+263789848070",  # Test QA
    "+27794979611",  # Gwenyambira & Sons — Kundai/internal test
    "+260954312554",  # Innocent — BOQ contact, not a pilot shop
    "+27746764737",  # Mukusakusa — Brighton's friend in Cape Town, not a pilot shop (see discussion #167)
}


def _is_test(company):
    owner = UserProfile.objects.filter(company=company, role="owner").first()
    return owner and owner.phone_number in TEST_PHONES


class Command(BaseCommand):
    help = "Pilot activity audit — shops, sales, messages, issues"

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=24,
            help="Lookback window in hours (default: 24)",
        )

    def handle(self, *args, **options):
        hours = options["hours"]
        now = timezone.now()
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        since = now - timedelta(hours=hours)
        w = self.stdout.write
        style = self.style

        w(style.MIGRATE_HEADING(f"\n=== PILOT AUDIT (last {hours}h) — {now.strftime('%Y-%m-%d %H:%M UTC')} ==="))

        # ── New waitlist entries ──────────────────────────────────────────────
        w(style.MIGRATE_LABEL("\n-- New signups --"))
        new_wl = WaitlistEntry.objects.filter(created_at__gte=since).order_by("created_at")
        if new_wl.exists():
            for e in new_wl:
                name = e.company_name or "(no name given)"
                w(f"  {e.phone_number}  {name}  [{e.status}]  {e.created_at.strftime('%Y-%m-%d %H:%M UTC')}")
        else:
            w("  None")

        pending = WaitlistEntry.objects.filter(status="pending")
        if pending.exists():
            w(style.WARNING(f"\n  {pending.count()} pending approval:"))
            for e in pending:
                w(f"    {e.phone_number}  {e.company_name or '(no name)'}  since {e.created_at.strftime('%Y-%m-%d %H:%M UTC')}")

        # ── Per-company summary ───────────────────────────────────────────────
        w(style.MIGRATE_LABEL("\n-- Companies --"))
        companies = Company.objects.filter(active=True).order_by("name")
        issues = []

        for c in companies:
            if _is_test(c):
                continue

            owner = UserProfile.objects.filter(company=c, role="owner").first()
            phone = owner.phone_number if owner else "?"
            members = UserProfile.objects.filter(company=c).count()

            sales_today = Sale.objects.filter(
                company=c, status=Sale.Status.CONFIRMED, sale_timestamp__date=today
            ).count()
            sales_yest = Sale.objects.filter(
                company=c, status=Sale.Status.CONFIRMED, sale_timestamp__date=yesterday
            ).count()
            sales_total = Sale.objects.filter(company=c, status=Sale.Status.CONFIRMED).count()
            cancelled = Sale.objects.filter(company=c, status=Sale.Status.CANCELLED).count()

            last_inbound = (
                WhatsAppMessage.objects.filter(company=c, direction="inbound")
                .order_by("-timestamp")
                .first()
            )
            last_active = (
                last_inbound.timestamp.strftime("%Y-%m-%d %H:%M UTC") if last_inbound else "never"
            )

            msgs_window = WhatsAppMessage.objects.filter(
                company=c, direction="inbound", timestamp__gte=since
            ).count()

            w(f"\n  {c.name}  ({phone})  members={members}")
            w(f"    sales  total={sales_total}  cancelled={cancelled}  yesterday={sales_yest}  today={sales_today}")
            w(f"    last inbound: {last_active}  |  msgs last {hours}h: {msgs_window}")
            w(f"    close={c.normal_closing_time or 'not set'}  |  last_summary={c.last_summary_date or 'never'}")

            # Flag issues
            if c.name == "Unnamed Shop":
                issues.append(f"  UNNAMED: {phone} — shop name was never collected")
            if sales_total == 0 and last_inbound is None:
                issues.append(f"  SILENT: {c.name} ({phone}) — approved but never messaged")
            if sales_total > 0 and c.last_summary_date is None:
                issues.append(f"  NO SUMMARY: {c.name} — has {sales_total} sales but never received a report")
            if sales_total > 0 and not c.normal_closing_time:
                issues.append(f"  NO CLOSE TIME: {c.name} — has sales but no permanent closing time set")

        # ── Recent sales detail ───────────────────────────────────────────────
        w(style.MIGRATE_LABEL(f"\n-- Sales last {hours}h --"))
        recent_sales = (
            Sale.objects.filter(sale_timestamp__gte=since, status=Sale.Status.CONFIRMED)
            .order_by("-sale_timestamp")
            .select_related("company")
            .prefetch_related("items__product")
        )
        real_sales = [s for s in recent_sales if not _is_test(s.company)]
        if real_sales:
            from collections import defaultdict
            from decimal import Decimal
            for s in real_sales:
                currency_totals: dict = defaultdict(Decimal)
                item_parts = []
                for i in s.items.all():
                    item_parts.append(f"{int(i.quantity)}x {i.product.name}")
                    if i.unit_price is not None and i.currency:
                        currency_totals[i.currency] += i.unit_price * i.quantity
                amount_str = "  ".join(f"{cur}{amt}" for cur, amt in currency_totals.items()) or str(s.total_amount)
                items = ", ".join(item_parts)
                w(f"  {s.sale_timestamp.strftime('%Y-%m-%d %H:%M UTC')}  {s.company.name}  {amount_str}  {items}")
        else:
            w("  None")

        # ── Recent inbound messages ───────────────────────────────────────────
        w(style.MIGRATE_LABEL(f"\n-- Inbound messages last {hours}h --"))
        msgs = (
            WhatsAppMessage.objects.filter(direction="inbound", timestamp__gte=since)
            .exclude(phone_number__in=TEST_PHONES)
            .order_by("-timestamp")
            .select_related("company")
        )
        w(f"  total: {msgs.count()}")
        for m in msgs[:20]:
            co = m.company.name if m.company else "unknown"
            preview = (m.content or f"[{m.message_type}]")[:60]
            w(f"  {m.timestamp.strftime('%Y-%m-%d %H:%M UTC')}  {co}  {m.phone_number}  {preview}")

        # ── Issues summary ────────────────────────────────────────────────────
        w(style.MIGRATE_LABEL("\n-- Issues --"))
        if issues:
            for issue in issues:
                w(style.WARNING(issue))
        else:
            w(style.SUCCESS("  None"))

        w("")
