from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from datetime import datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
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
from apps.sales.services import _singular_plural_variants, summarize_profit
from apps.inventory.models import InventoryAdjustment
from apps.whatsapp.models import WhatsAppMessage
from apps.whatsapp.services.whatsapp_client import get_whatsapp_client


LOW_STOCK_THRESHOLD = 5
# Longest "last N days" a query may ask for (weeks are capped to the same span).
MAX_PERIOD_DAYS = 366
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
    top_products: list[tuple[str, int]]
    low_stock_items: list[tuple[str, int]]
    out_of_stock_items: list[tuple[str, int]]
    # Profit per currency, from lines with a known cost only (#409).
    currency_profits: dict = field(default_factory=dict)
    # Priced lines left out of profit (no cost yet, or cost in another currency).
    uncosted_lines: int = 0
    # Top products by profit, ranked within each currency: {currency: [(name, profit)]}.
    profit_products: dict = field(default_factory=dict)
    # Products sold without a cost price, most units first (so the owner can recall them).
    uncosted_products: list = field(default_factory=list)
    # Lines left out because their cost is in a different currency from their price.
    mismatched_currency_lines: int = 0
    # The cost the profit used for the best-selling costed product, (name, cost, currency),
    # and how many of the shop's active products have a cost, so the owner sees where they stand.
    cost_example: tuple | None = None
    costed_products: int = 0
    total_products: int = 0


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


def _company_recipient_roles(company: Company) -> dict[str, bool]:
    """{phone: is_owner} for everyone who receives the shop's messages."""
    roles: dict[str, bool] = {}
    for phone, role in UserProfile.objects.filter(company=company, user__is_active=True).values_list(
        "phone_number", "role"
    ):
        roles[phone] = roles.get(phone, False) or role == UserProfile.Role.OWNER
    return roles


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
    all_items = []

    for sale in sales:
        for item in sale.items.all():
            all_items.append(item)
            items_sold += int(item.quantity or 0)
            product_totals[item.product.name] = product_totals.get(item.product.name, 0) + int(item.quantity or 0)
            if item.unit_price is not None and item.currency:
                currency_revenues[item.currency] = (
                    currency_revenues.get(item.currency, Decimal("0.00"))
                    + item.unit_price * item.quantity
                )

    revenue = sum(currency_revenues.values(), Decimal("0.00"))
    profit = summarize_profit(all_items)

    top_products = sorted(product_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:5]

    low_stock_items: list[tuple[str, int]] = []
    out_of_stock_items: list[tuple[str, int]] = []
    total_products = 0
    costed_products = 0
    for product in Product.objects.filter(company=company, active=True):
        total_products += 1
        costed_products += product.cost is not None
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
        top_products=top_products,
        low_stock_items=low_stock_items,
        out_of_stock_items=out_of_stock_items,
        currency_profits=profit.by_currency,
        uncosted_lines=profit.uncosted_lines,
        profit_products=profit.most_profitable(),
        uncosted_products=profit.unknown_cost_names(),
        mismatched_currency_lines=profit.mismatched_currency_lines,
        cost_example=profit.cost_example(),
        costed_products=costed_products,
        total_products=total_products,
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


def _display_profit(currency_profits: dict[str, Decimal]) -> str:
    """Format per-currency profit. Currencies are listed side by side, never
    added together, and a loss reads as -$0.50 rather than $-0.50."""
    parts = []
    for cur, amt in currency_profits.items():
        text = format_price(abs(amt), cur)
        parts.append(f"-{text}" if amt < 0 else text)
    return ", ".join(parts)


MAX_UNCOSTED_NAMES = 5


def _uncosted_products_text(names: list[str], lang: str) -> str:
    from apps.whatsapp.services.webhook_handler import t

    shown = ", ".join(names[:MAX_UNCOSTED_NAMES])
    extra = len(names) - MAX_UNCOSTED_NAMES
    if extra > 0:
        shown += " " + t("summary.and_more", lang=lang, count=extra)
    return t("summary.profit_uncosted_products", lang=lang, products=shown)


def _partial_profit_note(names: list[str], uncosted: int, lang: str, mismatched: int = 0) -> str:
    """Why profit leaves lines out: name the products with no cost, say when a cost
    exists but is in another currency, and only count lines when neither is known."""
    from apps.whatsapp.services.webhook_handler import t

    parts = []
    if names:
        parts.append(_uncosted_products_text(names, lang))
    if mismatched:
        parts.append(t("summary.profit_currency_mismatch", lang=lang))
    if not parts:
        return t("summary.profit_partial_note", lang=lang, count=uncosted)
    return " ".join(parts)


def _no_profit_lines(names: list[str], lang: str, mismatched: int = 0) -> list[str]:
    """Shown instead of a profit figure when no sold line can be counted. If the
    only reason is a cost in another currency, don't tell the owner to add a cost
    they already gave."""
    from apps.whatsapp.services.webhook_handler import t

    lines = []
    if names or not mismatched:
        example = names[0] if names else "bread"
        lines.append(t("summary.profit_none_note", lang=lang, example=example))
    if names:
        lines.append(_uncosted_products_text(names, lang))
    if mismatched:
        lines.append(t("summary.profit_currency_mismatch", lang=lang))
    return lines


def _cost_footer(costed: int, total: int, cost_example, lang: str) -> list[str]:
    """One closing line (#418): how many of the shop's products have a cost, and how to change
    one, since costs go stale and the bot only asks once. The example is a round number just
    above a cost it used, so it reads as a format to copy, not a suggested price."""
    from apps.whatsapp.services.webhook_handler import t

    parts = []
    if total:
        parts.append(t("summary.costs_saved", lang=lang, have=costed, total=total))
    if cost_example:
        name, cost, currency = cost_example
        example = cost.to_integral_value(rounding=ROUND_FLOOR) + 1
        parts.append(t("summary.profit_cost_used", lang=lang, name=name, example=format_price(example, currency)))
    return ["", " ".join(parts)] if parts else []


def _most_profitable_lines(profit_products: dict, lang: str) -> list[str]:
    from apps.whatsapp.services.webhook_handler import t

    if not profit_products:
        return []
    lines = ["", t("summary.most_profitable", lang=lang)]
    for currency, ranked in profit_products.items():
        for name, amount in ranked:
            lines.append(t("summary.most_profitable_line", lang=lang, name=name, profit=_display_profit({currency: amount})))
    return lines


def format_business_summary(snapshot: BusinessSnapshot, lang: str | None = None, show_profit: bool = True) -> str:
    """Render a human-readable business summary message.

    `show_profit=False` leaves out every profit line (profit, notes, most profitable):
    what a shop pays for stock is the owner's private information (#409).
    """
    from apps.whatsapp.services.webhook_handler import t, DEFAULT_LANGUAGE

    lang = lang or DEFAULT_LANGUAGE
    date_label = snapshot.report_date.strftime("%d %b %Y")
    sale_word = t(
        "summary.sale_word", lang=lang,
        count=snapshot.sales_count, plural="s" if snapshot.sales_count != 1 else "",
    )
    lines = [t("summary.heading", lang=lang, date=date_label)]

    lines.append(t(
        "summary.revenue_line", lang=lang,
        revenue=_display_revenue(snapshot.currency_revenues), sale_word=sale_word,
    ))

    # Sales, then profit, then the rankings, then one line about costs (#418).
    footer: list[str] = []
    if not show_profit:
        pass
    elif snapshot.currency_profits:
        lines.append(t("summary.profit_line", lang=lang, profit=_display_profit(snapshot.currency_profits)))
        if snapshot.uncosted_lines:
            lines.append(_partial_profit_note(
                snapshot.uncosted_products, snapshot.uncosted_lines, lang, snapshot.mismatched_currency_lines))
        footer = _cost_footer(snapshot.costed_products, snapshot.total_products, snapshot.cost_example, lang)
    elif snapshot.sales_count and snapshot.uncosted_lines:
        lines.extend(_no_profit_lines(snapshot.uncosted_products, lang, snapshot.mismatched_currency_lines))
        footer = _cost_footer(snapshot.costed_products, snapshot.total_products, None, lang)

    if snapshot.top_products:
        lines.append("")
        lines.append(t("summary.top_sellers", lang=lang))
        for name, qty in snapshot.top_products[:3]:
            lines.append(t("summary.top_seller_line", lang=lang, name=name, qty=qty))

    if show_profit:
        lines.extend(_most_profitable_lines(snapshot.profit_products, lang))
    lines.extend(footer)

    # Closing line based on revenue
    if snapshot.revenue == 0:
        lines.append("")
        is_today = snapshot.report_date == timezone.localdate()
        quiet_day_key = "summary.quiet_day_today" if is_today else "summary.quiet_day_past"
        lines.append(t(quiet_day_key, lang=lang))
    elif snapshot.sales_count >= 10:
        lines.append("")
        lines.append(t("summary.good_day", lang=lang))

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


def resolve_period(
    timeframe: str | None,
    count: int | None,
    today: datetime.date,
    force_range: bool = False,
    lang: str | None = None,
) -> tuple[datetime.date, datetime.date, str] | None:
    """Turn a parsed timeframe into (start, end, label) for a multi-day answer.

    Returns None for a single day (today/yesterday) — those get the stat card —
    unless `force_range` asks for a text answer for that day too (profit questions).
    "days"/"weeks" with a count are rolling windows ending today: "past 2 days"
    is yesterday and today, "last 2 weeks" is the last 14 days.
    """
    from apps.whatsapp.services.webhook_handler import t

    lang = lang or "en"  # callers in the bot always pass the owner's language
    fmt = lambda d: d.strftime("%d %b")  # noqa: E731
    if timeframe == "weeks" and not count:
        count = 1  # "last week" with no number; don't silently fall back to today
    if timeframe == "week":
        monday = today - timedelta(days=today.weekday())
        return monday, today, t("period.label_week", lang=lang, start=fmt(monday), end=fmt(today))
    if timeframe == "month":
        return today.replace(day=1), today, t(f"period.months.{today.month}", lang=lang)
    if timeframe == "year":
        return today.replace(month=1, day=1), today, str(today.year)
    if timeframe in ("days", "weeks") and count and count > 0:
        if timeframe == "days":
            n = min(count, MAX_PERIOD_DAYS)
            key, number = "period.label_last_days", n
        else:
            number = min(count, MAX_PERIOD_DAYS // 7)
            n = number * 7
            key = "period.label_last_week" if number == 1 else "period.label_last_weeks"
        if n > 1:
            start = today - timedelta(days=n - 1)
            return start, today, t(key, lang=lang, n=number, start=fmt(start), end=fmt(today))
    if force_range:
        day = today - timedelta(days=1) if timeframe == "yesterday" else today
        key = "period.label_yesterday" if timeframe == "yesterday" else "period.label_today"
        return day, day, t(key, lang=lang, date=fmt(day))
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
    all_items = []
    for sale in sales:
        for item in sale.items.all():
            all_items.append(item)
            product_totals[item.product.name] = product_totals.get(item.product.name, 0) + int(item.quantity or 0)
            if item.unit_price is not None and item.currency:
                currency_revenues[item.currency] = (
                    currency_revenues.get(item.currency, Decimal("0.00"))
                    + item.unit_price * item.quantity
                )
    revenue = sum(currency_revenues.values(), Decimal("0.00"))
    top_products = sorted(product_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    profit = summarize_profit(all_items)
    product_counts = Product.objects.filter(company=company, active=True).aggregate(
        total=models.Count("id"), costed=models.Count("cost"))
    costed_products, total_products = product_counts["costed"], product_counts["total"]
    return {
        "revenue": revenue,
        "currency_revenues": currency_revenues,
        "sales_count": len(sales),
        "top_products": top_products,
        "currency": company.currency,
        "currency_profits": profit.by_currency,
        "uncosted_lines": profit.uncosted_lines,
        "profit_products": profit.most_profitable(),
        "uncosted_products": profit.unknown_cost_names(),
        "mismatched_currency_lines": profit.mismatched_currency_lines,
        "cost_example": profit.cost_example(),
        "costed_products": costed_products,
        "total_products": total_products,
    }


def _catalog_name_query(product_filter: str, field_path: str = "name") -> models.Q:
    """Build the OR'd icontains Q object for `product_filter` and its
    singular/plural variants, against `field_path` (a Django lookup path
    ending in a name field, e.g. "name" for a direct Product queryset, or
    "items__product__name" through a Sale queryset's relation) — shared by
    the sales query-level filter and the catalog-existence check below
    (#447 review — Omar's code note: one place generates this condition, so
    a future change to the matching rule can't update one call site and
    miss the other)."""
    filter_candidates = [product_filter] + _singular_plural_variants(product_filter)
    name_query = models.Q()
    lookup = f"{field_path}__icontains"
    for candidate in filter_candidates:
        name_query |= models.Q(**{lookup: candidate})
    return name_query


def _product_name_matches_filter(product_name: str, product_filter: str) -> bool:
    """Case-insensitive substring match against a product name OR any of its
    singular/plural variants (#447 review — Omar). A plain substring check
    alone misses an owner asking in the opposite form from how the product
    was saved: a shop with "Egg" (singular, however the owner first typed
    it) asked about in the plural ("how much eggs did I sell") would
    otherwise get told "No sales of eggs" despite having sold plenty — worse
    than main's pre-#436 behaviour, which at least showed everything. Tried
    against the filter's own variants (not the product name's) since the
    filter is the shorter, LLM-extracted side of the match; checking it
    against _singular_plural_variants(product_name) instead would also work
    but is less intuitive to read at the call site.

    Used identically at both the query level and the per-item level in
    build_itemised_sales_list — kept as one function (Omar's #447 review
    code note) so the two checks can't drift out of step."""
    lower_name = product_name.lower()
    candidates = [product_filter] + _singular_plural_variants(product_filter)
    return any(candidate.lower() in lower_name for candidate in candidates)


def build_itemised_sales_list(
    company: Company, start_date: dt.date, end_date: dt.date, product_filter: str | None = None,
) -> dict:
    """Every product sold in a date range, grouped by product and sorted by
    quantity (#408 — per Brighton's review, grouped rather than one line per
    individual sale: a per-sale view hit the 20-line display limit almost
    immediately for a week-long query, and owners asked "how much of each
    did I sell", which is inherently a grouped question). Kept as plain
    dicts, not formatted text, so the same data can later feed a dashboard
    view, not just a WhatsApp message.

    product_filter (#436): narrows to one product, e.g. "show me all the
    bread I sold this week". A case-insensitive substring match against the
    product name — the LLM has already extracted the name from the owner's
    own words, so this doesn't need its own fuzzy-matching logic, matching
    the "exclude, don't validate" pattern used elsewhere in this file.
    Filtered at the query level (not after grouping) so sales_count and
    currency_totals reflect only the matching product too, not the whole
    shop's activity for the period."""
    start = timezone.make_aware(datetime.combine(start_date, time.min))
    end = timezone.make_aware(datetime.combine(end_date, time.max))
    sales_qs = Sale.objects.filter(
        company=company,
        status=Sale.Status.CONFIRMED,
        sale_timestamp__gte=start,
        sale_timestamp__lte=end,
    )
    if product_filter:
        # OR'd across the filter and its singular/plural variants (#447
        # review — Omar), so a plural question ("eggs") still finds a
        # product saved singular ("Egg") instead of narrowing to zero sales
        # at the query level before the per-item check even runs.
        sales_qs = sales_qs.filter(
            _catalog_name_query(product_filter, field_path="items__product__name")
        ).distinct()
    sales = list(sales_qs.prefetch_related("items__product"))
    # Grouped by (product, currency), not product alone — shops here take
    # USD, rand and ZiG in the same day, so $3 of bread and R20 of bread
    # must stay as two separate lines, never summed as one "$23" total
    # (review on PR #425: the daily summary keeps currencies apart for the
    # same reason).
    products: dict[tuple[str, str | None], dict] = {}
    currency_totals: dict[str, Decimal] = {}
    for sale in sales:
        for item in sale.items.all():
            if product_filter and not _product_name_matches_filter(item.product.name, product_filter):
                continue
            key = (item.product.name, item.currency)
            entry = products.setdefault(key, {
                "product": item.product.name, "quantity": 0, "revenue": Decimal("0.00"), "currency": item.currency,
            })
            entry["quantity"] += item.quantity
            if item.unit_price is not None and item.currency:
                entry["revenue"] += item.unit_price * item.quantity
                currency_totals[item.currency] = (
                    currency_totals.get(item.currency, Decimal("0.00"))
                    + item.unit_price * item.quantity
                )
    entries = sorted(products.values(), key=lambda e: (-e["quantity"], e["product"]))

    # #447 review (Omar): a filter matching zero sales is ambiguous between
    # "the shop genuinely sold none of this" and "the bot doesn't recognise
    # this product at all" (a typo, an unrecognised word like "mazai" for
    # "eggs", or a multi-product phrase like "bread and coke" the LLM
    # sometimes returns as a single product_filter string). Checked against
    # the company's active catalog, not just this period's sales, so the
    # distinction holds even when the product exists but genuinely had zero
    # sales in the requested period.
    product_filter_known = True
    if product_filter and not entries:
        product_filter_known = Product.objects.filter(active=True, company=company).filter(
            _catalog_name_query(product_filter)
        ).exists()

    return {
        "entries": entries,
        "currency_totals": currency_totals,
        "sales_count": len(sales),
        "product_filter_known": product_filter_known,
    }


# A WhatsApp text message has a practical length limit; past this many
# itemised lines, truncate rather than risk a message too long to send (#408).
MAX_ITEMISED_LINES = 20


def format_itemised_sales_list(
    period_label: str, data: dict, lang: str | None = None, product_filter: str | None = None,
) -> str:
    from apps.whatsapp.services.webhook_handler import t, DEFAULT_LANGUAGE

    lang = lang or DEFAULT_LANGUAGE
    # resolve_period()'s label is capitalised for standalone use (e.g. the
    # #414 summary path, where it opens the sentence). Here it sits
    # mid-sentence ("Zvawatengesa nhasi (09 Oct):"), so in Shona only,
    # lowercase its first letter — per Brighton's review on #425.
    if lang == "sn" and period_label:
        period_label = period_label[0].lower() + period_label[1:]

    if data["sales_count"] == 0:
        # #436: a filter that matched nothing gets a message naming the
        # product, not a generic "no sales" that reads as if the owner sold
        # nothing at all that period — but only when the shop actually has
        # a product matching the filter (#447 review — Omar). Otherwise the
        # bot doesn't recognise the filter itself (a typo, an unrecognised
        # word, or a multi-product phrase), and claiming "no sales of X"
        # would misrepresent not-understanding as a real, checked fact —
        # say the bot couldn't find it instead, matching this repo's
        # "ask, don't guess" rule rather than asserting a fact it can't back.
        if product_filter and data.get("product_filter_known", True):
            return t("itemised_sales.no_sales_for_product", lang=lang, period=period_label, product=product_filter)
        if product_filter:
            return t("itemised_sales.product_filter_not_found", lang=lang, period=period_label, product=product_filter)
        return t("itemised_sales.no_sales", lang=lang, period=period_label)

    lines = [t("itemised_sales.heading", lang=lang, period=period_label)]
    entries = data["entries"]
    shown = entries[:MAX_ITEMISED_LINES]
    no_price = t("itemised_sales.no_price", lang=lang)
    for entry in shown:
        price = (
            format_price(entry["revenue"], entry["currency"])
            if entry["revenue"] and entry["currency"]
            else no_price
        )
        lines.append(t(
            "itemised_sales.item_line", lang=lang,
            quantity=entry["quantity"], product=entry["product"], price=price,
        ))

    remaining = len(entries) - len(shown)
    if remaining > 0:
        lines.append(t("itemised_sales.more_items", lang=lang, count=remaining))

    lines.append(t("itemised_sales.total_line", lang=lang, total=_display_revenue(data["currency_totals"])))
    return "\n".join(lines)


def format_period_summary(
    period_label: str, data: dict, lang: str | None = None, metric: str = "revenue", show_profit: bool = True
) -> str:
    """Text answer for a date range."""
    from apps.whatsapp.services.webhook_handler import t

    lang = lang or "en"  # callers in the bot always pass the owner's language
    if data["sales_count"] == 0:
        return t("period.no_sales", lang=lang, label=period_label)

    if not show_profit:
        # Same answer without any profit: only the owner sees what products cost.
        data = {**data, "currency_profits": {}, "uncosted_lines": 0, "profit_products": {}, "uncosted_products": [],
                "cost_example": None, "costed_products": 0, "total_products": 0}
        metric = "revenue"
    profits = data.get("currency_profits") or {}
    uncosted = data.get("uncosted_lines", 0)
    revenue_str = _display_revenue(data.get("currency_revenues", {}))
    sale_word = t(
        "summary.sale_word", lang=lang,
        count=data["sales_count"], plural="s" if data["sales_count"] != 1 else "",
    )

    names = data.get("uncosted_products", [])
    mismatched = data.get("mismatched_currency_lines", 0)
    costed, total = data.get("costed_products", 0), data.get("total_products", 0)
    profit_lines: list[str] = []
    footer: list[str] = []
    if profits:
        profit_lines.append(t("summary.profit_line", lang=lang, profit=_display_profit(profits)))
        if uncosted:
            profit_lines.append(_partial_profit_note(names, uncosted, lang, mismatched))
        footer = _cost_footer(costed, total, data.get("cost_example"), lang)
    elif uncosted:
        # Nothing to say when there are no priced lines at all.
        profit_lines.extend(_no_profit_lines(names, lang, mismatched))
        footer = _cost_footer(costed, total, None, lang)

    # Sales, then profit, then the rankings, then one line about costs (#418).
    if metric == "profit":
        lines = [period_label, t("summary.revenue_line", lang=lang, revenue=revenue_str, sale_word=sale_word), *profit_lines]
        lines.extend(_most_profitable_lines(data.get("profit_products", {}), lang))
        lines.extend(footer)
        return "\n".join(lines)

    lines = [t("period.total_line", lang=lang, label=period_label, revenue=revenue_str, sale_word=sale_word)]
    if profits:
        lines.extend(profit_lines)
    if data["top_products"]:
        lines.append("")
        lines.append(t("summary.top_sellers", lang=lang))
        for name, qty in data["top_products"][:3]:
            lines.append(t("summary.top_seller_line", lang=lang, name=name, qty=qty))
    lines.extend(_most_profitable_lines(data.get("profit_products", {}), lang))
    if profits:
        lines.extend(footer)
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
    lang = await sync_to_async(_company_owner_lang, thread_sensitive=True)(company)
    # Profit lines go to owners only; assistants get the same summary without them (#409).
    text_with_profit = format_business_summary(snapshot, lang=lang, show_profit=True)
    text_without_profit = format_business_summary(snapshot, lang=lang, show_profit=False)

    try:
        from apps.whatsapp.services.report_card import generate_stat_card
        image_bytes = generate_stat_card(snapshot, comparison, shop_name=company.name, lang=lang)
        image_url = await sync_to_async(upload_report_image, thread_sensitive=True)(
            image_bytes, company.id, report_date
        )
    except Exception as e:
        logger.warning("Failed to generate report card for company %s", company.id, exc_info=e)
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("failure_type", "report_card_generation")
            scope.set_tag("company_id", company.id)
            scope.set_tag("source", "send_daily_summary")
            scope.set_context("report_card", {
                "company_id": company.id,
                "report_date": str(report_date),
                "source": "send_daily_summary",
            })
            sentry_sdk.capture_exception(e)
        image_url = None

    wa_client = get_whatsapp_client()
    recipients = await sync_to_async(_company_recipients, thread_sensitive=True)(company)
    owner_by_phone = await sync_to_async(_company_recipient_roles, thread_sensitive=True)(company)
    for phone in recipients:
        text_summary = text_with_profit if owner_by_phone.get(phone) else text_without_profit
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
            template_name = _template_name_for_lang("ats_daily_summary", lang)
            await wa_client.send_template_message(phone, template_name, components=components)
        elif image_url:
            await wa_client.send_image(phone, image_url, caption=text_summary)
        else:
            await wa_client.send_message(phone, text_summary)

    await mark_summary_sent(company.id, report_date)
    return recipients


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
