from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from datetime import datetime, time, timedelta
from decimal import Decimal
import logging
import re
from typing import Iterable

logger = logging.getLogger(__name__)

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.currencies import DEFAULT_CURRENCY, format_price
from apps.core.models import Company, UserProfile
from apps.sales.models import Sale, SaleItem
from apps.inventory.models import InventoryAdjustment
from apps.whatsapp.models import WhatsAppMessage
from apps.whatsapp.services.whatsapp_client import get_whatsapp_client


LOW_STOCK_THRESHOLD = 5
FALLBACK_SUMMARY_CUTOFF = time(21, 0)
WHATSAPP_SERVICE_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class BusinessSnapshot:
    company_id: int
    report_date: datetime.date
    currency: str
    sales_count: int
    items_sold: int
    revenue: Decimal
    currency_revenues: dict  # per-currency breakdown, e.g. {"USD": Decimal("12.00"), "ZAR": Decimal("45.00")}
    cost: Decimal
    gross_profit: Decimal
    top_products: list[tuple[str, int]]
    low_stock_items: list[tuple[str, int]]
    out_of_stock_items: list[tuple[str, int]]


TIME_RE = re.compile(r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?\b", re.IGNORECASE)


def parse_closing_time_text(text: str) -> time | None:
    """Parse a user reply like '6pm', '17:30', or 'at 8'."""
    normalized = (text or "").strip().lower()
    if not normalized:
        return None

    match = TIME_RE.search(normalized)
    if not match:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = match.group("meridiem")

    if meridiem:
        if hour == 12:
            hour = 0
        if meridiem == "pm":
            hour += 12
    elif hour <= 7 and "close" in normalized:
        # No meridiem but a clear closing context, leave as-is.
        pass

    if hour > 23 or minute > 59:
        return None

    return time(hour=hour, minute=minute)


async def parse_closing_time_llm(text: str) -> time | None:
    """Use the LLM to extract a closing time from a free-form reply.

    Handles natural language, Shona expressions, and correctly rejects sale
    messages that would fool the regex (e.g. "2 plates beef").
    Falls back to None on any error — caller treats that as no time found.
    """
    from services.openrouter.client import OpenRouterClient
    from services.openrouter.prompts import CLOSING_TIME_PARSING_PROMPT

    client = OpenRouterClient()
    messages = [
        {"role": "system", "content": CLOSING_TIME_PARSING_PROMPT},
        {"role": "user", "content": text or ""},
    ]
    try:
        result = await client.parse_json_response(messages)
        raw = result.get("closing_time")
        if not raw:
            return None
        hour, minute = (int(p) for p in raw.split(":"))
        return time(hour=hour, minute=minute)
    except Exception:
        logger.warning(f"LLM closing time parse failed for: {text!r}")
        return None


def _company_recipients(company: Company) -> list[str]:
    phones = list(
        UserProfile.objects.filter(company=company, user__is_active=True)
        .values_list("phone_number", flat=True)
        .distinct()
    )
    return phones or []


def _within_service_window(phone: str) -> bool:
    """True if `phone` sent an inbound WhatsApp message within the last 24
    hours (Meta's customer-service window) — meaning free-form/image
    messages are currently permitted for this recipient."""
    cutoff = timezone.now() - WHATSAPP_SERVICE_WINDOW
    return WhatsAppMessage.objects.filter(
        phone_number=phone,
        direction=WhatsAppMessage.Direction.INBOUND,
        timestamp__gte=cutoff,
    ).exists()


def _day_bounds(day: datetime.date) -> tuple[datetime, datetime]:
    start = timezone.make_aware(datetime.combine(day, time.min))
    end = timezone.make_aware(datetime.combine(day, time.max))
    return start, end


def _effective_closing_time(company: Company, day: datetime.date) -> time:
    if company.daily_closing_date == day and company.daily_closing_time:
        return company.daily_closing_time
    return company.normal_closing_time or time(17, 0)


def build_business_snapshot(company: Company, report_date: datetime.date | None = None) -> BusinessSnapshot:
    """Build a snapshot of the day's business activity."""
    report_date = report_date or timezone.localdate()
    start, end = _day_bounds(report_date)

    sales = list(
        Sale.objects.filter(
            company=company,
            status=Sale.Status.CONFIRMED,
            sale_timestamp__gte=start,
            sale_timestamp__lte=end,
        ).select_related("company").prefetch_related("items__product")
    )

    currency_revenues: dict[str, Decimal] = {}
    items_sold = 0
    product_totals: dict[str, int] = {}

    for sale in sales:
        for item in sale.items.all():
            items_sold += int(item.quantity or 0)
            product_totals[item.product.name] = product_totals.get(item.product.name, 0) + int(item.quantity or 0)
            if item.unit_price is not None and item.currency:
                currency_revenues[item.currency] = (
                    currency_revenues.get(item.currency, Decimal("0.00"))
                    + item.unit_price * item.quantity
                )

    revenue = sum(currency_revenues.values(), Decimal("0.00"))

    top_products = sorted(product_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:5]

    low_stock_items: list[tuple[str, int]] = []
    out_of_stock_items: list[tuple[str, int]] = []
    for product in Product.objects.filter(company=company, active=True):
        stock = int(product.current_stock)
        if stock <= 0:
            out_of_stock_items.append((product.name, stock))
        elif stock <= LOW_STOCK_THRESHOLD:
            low_stock_items.append((product.name, stock))

    low_stock_items.sort(key=lambda kv: (kv[1], kv[0]))
    out_of_stock_items.sort(key=lambda kv: (kv[1], kv[0]))

    return BusinessSnapshot(
        company_id=company.id,
        report_date=report_date,
        currency=company.currency,
        sales_count=len(sales),
        items_sold=items_sold,
        revenue=revenue,
        currency_revenues=currency_revenues,
        cost=Decimal("0.00"),
        gross_profit=revenue,
        top_products=top_products,
        low_stock_items=low_stock_items,
        out_of_stock_items=out_of_stock_items,
    )


def _display_revenue(currency_revenues: dict[str, Decimal]) -> str:
    """Format a revenue amount for WhatsApp display.

    Never assumes a currency that wasn't actually present in the underlying
    transactions — company.currency is NOT used here, since a shop's actual
    transaction currency on a given day/period may differ from their
    configured default.
    """
    if not currency_revenues:
        return format_price(Decimal("0.00"), DEFAULT_CURRENCY)
    if len(currency_revenues) == 1:
        cur, amt = next(iter(currency_revenues.items()))
        return format_price(amt, cur)
    return " + ".join(format_price(amt, cur) for cur, amt in currency_revenues.items())


def format_business_summary(snapshot: BusinessSnapshot) -> str:
    """Render a human-readable business summary message."""
    date_label = snapshot.report_date.strftime("%d %b %Y")
    sale_word = f"{snapshot.sales_count} sale{'s' if snapshot.sales_count != 1 else ''}"
    lines = [f"Summary for {date_label}"]

    lines.append(f"Revenue: {_display_revenue(snapshot.currency_revenues)} from {sale_word}")

    if snapshot.top_products:
        lines.append("")
        lines.append("Top sellers:")
        for name, qty in snapshot.top_products[:3]:
            lines.append(f"  • {name}: {qty}")

    # Closing line based on revenue
    if snapshot.revenue == 0:
        lines.append("")
        lines.append("Quiet day — no sales recorded.")
    elif snapshot.sales_count >= 10:
        lines.append("")
        lines.append("Good day!")

    return "\n".join(lines)


def format_low_stock_summary(snapshot: BusinessSnapshot) -> str:
    lines = [f"Low stock for {snapshot.report_date.strftime('%d %b %Y')}:"]
    if not snapshot.low_stock_items and not snapshot.out_of_stock_items:
        lines.append("- No low stock items found.")
        return "\n".join(lines)

    if snapshot.out_of_stock_items:
        lines.append("Out of stock:")
        for name, stock in snapshot.out_of_stock_items[:10]:
            lines.append(f"  • {name}: {stock}")

    if snapshot.low_stock_items:
        lines.append("Low stock:")
        for name, stock in snapshot.low_stock_items[:10]:
            lines.append(f"  • {name}: {stock}")

    return "\n".join(lines)


def build_comparison_context(company: Company, report_date: datetime.date | None = None) -> dict:
    """Return week-rank and cumulative data for the current week up to report_date."""
    report_date = report_date or timezone.localdate()

    monday = report_date - timedelta(days=report_date.weekday())
    week_day_snapshots: list[BusinessSnapshot] = []
    day = monday
    while day <= report_date:
        snap = build_business_snapshot(company, report_date=day)
        week_day_snapshots.append(snap)
        day += timedelta(days=1)

    week_revenues: list[dict[str, Decimal]] = [snap.currency_revenues for snap in week_day_snapshots]
    week_sales_count: int = sum(snap.sales_count for snap in week_day_snapshots)

    week_currency_revenues: dict[str, Decimal] = {}
    for snap in week_day_snapshots:
        for cur, amt in snap.currency_revenues.items():
            week_currency_revenues[cur] = week_currency_revenues.get(cur, Decimal("0.00")) + amt

    # Compare using primary currency only — summing across currencies produces meaningless numbers
    primary_currency = company.currency
    primary_day_totals = [d.get(primary_currency, Decimal("0.00")) for d in week_revenues]
    today_primary = primary_day_totals[-1]
    prior_days_with_sales = any(t > 0 for t in primary_day_totals[:-1])
    is_best_day_this_week = bool(primary_day_totals) and today_primary >= max(primary_day_totals) and prior_days_with_sales

    # Determine which day name to show in the badge
    best_day_index = primary_day_totals.index(max(primary_day_totals))
    best_day_date = monday + timedelta(days=best_day_index)
    if best_day_date == report_date:
        best_day_label = "Today"
    else:
        best_day_label = best_day_date.strftime("%A")  # e.g. "Monday", "Tuesday"

    return {
        "is_best_day_this_week": is_best_day_this_week,
        "best_day_label": best_day_label,
        "week_revenues": week_revenues,
        "week_sales_count": week_sales_count,
        "week_currency_revenues": week_currency_revenues,
    }


@sync_to_async(thread_sensitive=True)
def set_company_daily_closing_time(company_id: int, closing_time: time, closing_date: datetime.date) -> None:
    Company.objects.filter(id=company_id).update(
        daily_closing_time=closing_time,
        daily_closing_date=closing_date,
    )


@sync_to_async(thread_sensitive=True)
def set_company_normal_closing_time(company_id: int, closing_time: time) -> None:
    # Only write if not already set — single atomic SQL statement, safe under concurrency.
    Company.objects.filter(id=company_id, normal_closing_time__isnull=True).update(
        normal_closing_time=closing_time
    )


@sync_to_async(thread_sensitive=True)
def mark_closing_prompt_sent(company_id: int, prompt_date: datetime.date) -> None:
    Company.objects.filter(id=company_id).update(last_closing_prompt_date=prompt_date)


@sync_to_async(thread_sensitive=True)
def mark_summary_sent(company_id: int, summary_date: datetime.date) -> None:
    Company.objects.filter(id=company_id).update(last_summary_date=summary_date)


@sync_to_async(thread_sensitive=True)
def load_due_companies(now_date: datetime.date, now_time: time) -> list[Company]:
    return list(
        Company.objects.filter(active=True, daily_summary_enabled=True).filter(
            models.Q(last_closing_prompt_date__isnull=True) | ~models.Q(last_closing_prompt_date=now_date)
        )
    )


@sync_to_async(thread_sensitive=True)
def load_active_companies() -> list[Company]:
    return list(Company.objects.filter(active=True, daily_summary_enabled=True))


@sync_to_async(thread_sensitive=True)
def get_company_by_id(company_id: int) -> Company | None:
    return Company.objects.filter(id=company_id).first()


def upload_report_image(image_bytes: bytes, company_id: int, report_date: dt.date) -> str | None:
    """Upload a report card PNG to R2 and return its public URL."""
    try:
        from services.storage.r2_client import R2StorageClient
        client = R2StorageClient()
        date_str = report_date.strftime("%Y-%m-%d")
        # Use a stable key so re-runs overwrite rather than accumulate
        file_key = f"reports/{company_id}/{date_str}.png"
        if not client.client:
            logger.warning("R2 not configured — skipping report image upload")
            return None
        client.client.put_object(
            Bucket=client.bucket_name,
            Key=file_key,
            Body=image_bytes,
            ContentType="image/png",
        )
        if client.public_url:
            return f"{client.public_url.rstrip('/')}/{file_key}"
        return f"{client.endpoint_url.rstrip('/')}/{client.bucket_name}/{file_key}"
    except Exception:
        logger.exception("Failed to upload report image to R2")
        return None


def build_period_summary(company: Company, start_date: datetime.date, end_date: datetime.date) -> dict:
    """Aggregate sales across a date range for week/month/year queries."""
    start = timezone.make_aware(datetime.combine(start_date, time.min))
    end = timezone.make_aware(datetime.combine(end_date, time.max))
    sales = list(
        Sale.objects.filter(
            company=company,
            status=Sale.Status.CONFIRMED,
            sale_timestamp__gte=start,
            sale_timestamp__lte=end,
        ).prefetch_related("items__product")
    )
    currency_revenues: dict[str, Decimal] = {}
    product_totals: dict[str, int] = {}
    for sale in sales:
        for item in sale.items.all():
            product_totals[item.product.name] = product_totals.get(item.product.name, 0) + int(item.quantity or 0)
            if item.unit_price is not None and item.currency:
                currency_revenues[item.currency] = (
                    currency_revenues.get(item.currency, Decimal("0.00"))
                    + item.unit_price * item.quantity
                )
    revenue = sum(currency_revenues.values(), Decimal("0.00"))
    top_products = sorted(product_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return {
        "revenue": revenue,
        "currency_revenues": currency_revenues,
        "sales_count": len(sales),
        "top_products": top_products,
        "currency": company.currency,
    }


def format_period_summary(period_label: str, data: dict) -> str:
    if data["sales_count"] == 0:
        return f"{period_label}: No sales recorded."
    revenue_str = _display_revenue(data.get("currency_revenues", {}))
    sale_word = f"{data['sales_count']} sale{'s' if data['sales_count'] != 1 else ''}"
    lines = [f"{period_label}: {revenue_str} from {sale_word}"]
    if data["top_products"]:
        lines.append("\nTop sellers:")
        for name, qty in data["top_products"][:3]:
            lines.append(f"  • {name}: {qty}")
    return "\n".join(lines)


def _company_owner_lang(company: Company) -> str:
    profile = (
        UserProfile.objects.filter(company=company, role=UserProfile.Role.OWNER, user__is_active=True)
        .first()
    )
    return profile.language if profile else "sn"


async def send_message_to_company(company: Company, message: str) -> list[str]:
    """Send a WhatsApp message to all known members of a company."""
    client = get_whatsapp_client()
    recipients = await sync_to_async(_company_recipients, thread_sensitive=True)(company)
    for phone in recipients:
        await client.send_message(phone, message)
    return recipients


async def send_daily_closing_prompt(company: Company) -> list[str]:
    from apps.whatsapp.services.webhook_handler import t
    lang = await sync_to_async(_company_owner_lang, thread_sensitive=True)(company)
    recipients = await send_message_to_company(company, t("closing.prompt", lang=lang))
    await mark_closing_prompt_sent(company.id, timezone.localdate())
    return recipients


async def send_daily_summary(company: Company, report_date: datetime.date | None = None) -> list[str]:
    report_date = report_date or timezone.localdate()
    snapshot = await sync_to_async(build_business_snapshot, thread_sensitive=True)(company, report_date=report_date)

    if snapshot.sales_count == 0:
        logger.debug("No sales for company %s on %s — skipping daily summary", company.id, report_date)
        return []

    comparison = await sync_to_async(build_comparison_context, thread_sensitive=True)(company, report_date)
    text_summary = format_business_summary(snapshot)

    try:
        from apps.whatsapp.services.report_card import generate_stat_card
        image_bytes = generate_stat_card(snapshot, comparison, shop_name=company.name)
        image_url = await sync_to_async(upload_report_image, thread_sensitive=True)(
            image_bytes, company.id, report_date
        )
    except Exception:
        logger.exception("Failed to generate report card for company %s", company.id)
        image_url = None

    wa_client = get_whatsapp_client()
    recipients = await sync_to_async(_company_recipients, thread_sensitive=True)(company)
    for phone in recipients:
        use_template = False
        if settings.USE_TEMPLATE_MESSAGES:
            in_window = await sync_to_async(_within_service_window, thread_sensitive=True)(phone)
            use_template = not in_window

        if use_template:
            from apps.whatsapp.services.nudge_service import _template_name_for_lang
            revenue_str = _display_revenue(snapshot.currency_revenues)
            top_seller = snapshot.top_products[0][0] if snapshot.top_products else "—"
            components = [{"type": "body", "parameters": [
                {"type": "text", "text": snapshot.report_date.strftime("%d %b %Y")},
                {"type": "text", "text": revenue_str},
                {"type": "text", "text": str(snapshot.sales_count)},
                {"type": "text", "text": top_seller},
            ]}]
            lang = await sync_to_async(_company_owner_lang, thread_sensitive=True)(company)
            template_name = _template_name_for_lang("ats_daily_summary", lang)
            await wa_client.send_template_message(phone, template_name, components=components)
        elif image_url:
            await wa_client.send_image(phone, image_url, caption=text_summary)
        else:
            await wa_client.send_message(phone, text_summary)

    await mark_summary_sent(company.id, report_date)
    return recipients


async def send_low_stock_summary(company: Company, report_date: datetime.date | None = None) -> list[str]:
    report_date = report_date or timezone.localdate()
    snapshot = await sync_to_async(build_business_snapshot, thread_sensitive=True)(company, report_date=report_date)
    message = format_low_stock_summary(snapshot)
    return await send_message_to_company(company, message)


async def maybe_send_daily_notifications(now: datetime | None = None) -> dict[str, list[int]]:
    """Check all active companies and send prompt or summary if due.

    Returns a dict with company IDs for actions taken.
    """
    now = now or timezone.now()
    today = timezone.localdate(now)
    current_time = timezone.localtime(now).time()

    companies = await load_active_companies()
    sent_summary: list[int] = []

    for company in companies:
        closing_set_today = company.daily_closing_date == today and company.daily_closing_time

        if company.normal_closing_time:
            # Owner has set a permanent closing time.
            # Send the summary 1 hour after their normal closing time.
            summary_time = (
                datetime.combine(today, company.normal_closing_time) + timedelta(hours=1)
            ).time()
            if company.last_summary_date != today and current_time >= summary_time:
                await send_daily_summary(company, report_date=today)
                sent_summary.append(company.id)
        else:
            if company.last_summary_date != today:
                if closing_set_today:
                    effective_close = _effective_closing_time(company, today)
                    if current_time >= effective_close:
                        await send_daily_summary(company, report_date=today)
                        sent_summary.append(company.id)
                elif current_time >= FALLBACK_SUMMARY_CUTOFF:
                    # No closing time set — send summary at 9pm fallback
                    await send_daily_summary(company, report_date=today)
                    sent_summary.append(company.id)

    return {"prompt": [], "summary": sent_summary}
