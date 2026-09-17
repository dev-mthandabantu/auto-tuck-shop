"""Pilot metrics dashboard for the admin site."""

from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q
from django.shortcuts import render
from django.utils import timezone

from apps.core.models import Company, UserProfile
from apps.sales.models import Sale
from apps.whatsapp.models import WhatsAppMessage


def _compute_activation(shop, since):
    """Return (first_sale_date, activated) for a shop.

    activated=True means a second confirmed sale was recorded within 7 days of the first.
    Only considers sales within the dashboard's time window.
    """
    first_sale = (
        Sale.objects.filter(company=shop, status=Sale.Status.CONFIRMED, sale_timestamp__gte=since)
        .order_by("sale_timestamp")
        .values_list("sale_timestamp", flat=True)
        .first()
    )
    if first_sale is None:
        return None, None
    activated = Sale.objects.filter(
        company=shop,
        status=Sale.Status.CONFIRMED,
        sale_timestamp__gt=first_sale,
        sale_timestamp__lte=first_sale + timedelta(days=7),
    ).exists()
    return first_sale, activated


def _shop_display_name(company):
    """Return a distinguishable name for a shop: 'Name (owner phone)' or 'Name (slug)'."""
    owner = (
        UserProfile.objects.filter(company=company, role=UserProfile.Role.OWNER)
        .values_list("phone_number", flat=True)
        .first()
    )
    detail = owner or company.slug
    return f"{company.name} ({detail})"


@staff_member_required
def pilot_metrics(request):
    """Dashboard showing key pilot metrics."""
    now = timezone.now()
    days_back = int(request.GET.get("days", 30))
    since = now - timedelta(days=days_back)

    # --- Sales per shop ---
    sale_attempt_types = [
        WhatsAppMessage.MessageType.TEXT,
        WhatsAppMessage.MessageType.AUDIO,
    ]
    sales_per_shop = (
        Company.objects.filter(active=True)
        .annotate(
            total_sales=Count(
                "sales",
                filter=Q(
                    sales__sale_timestamp__gte=since,
                    sales__status=Sale.Status.CONFIRMED,
                ),
                distinct=True,
            ),
            confirmed_sales=Count(
                "sales",
                filter=Q(
                    sales__sale_timestamp__gte=since,
                    sales__status=Sale.Status.CONFIRMED,
                ),
                distinct=True,
            ),
            mistake_count=Count(
                "sales",
                filter=Q(
                    sales__sale_timestamp__gte=since,
                    # "Fix mistake" cancels and flags the sale in one write
                    # (sale_handler._get_and_update_sale), so a flagged sale is
                    # CANCELLED, never CONFIRMED. Requiring CONFIRMED here made
                    # this count structurally 0 (#378).
                    sales__status=Sale.Status.CANCELLED,
                    sales__flagged_as_bot_mistake=True,
                ),
                distinct=True,
            ),
            inbound_messages=Count(
                "whatsapp_messages",
                filter=Q(
                    whatsapp_messages__direction=WhatsAppMessage.Direction.INBOUND,
                    whatsapp_messages__message_type__in=sale_attempt_types,
                    whatsapp_messages__timestamp__gte=since,
                ),
                distinct=True,
            ),
        )
        .order_by("-total_sales")
    )

    # --- Voice vs text split ---
    inbound_messages = WhatsAppMessage.objects.filter(
        direction=WhatsAppMessage.Direction.INBOUND,
        company__isnull=False,
        timestamp__gte=since,
    )
    total_inbound = inbound_messages.count()
    voice_inbound = inbound_messages.filter(
        message_type=WhatsAppMessage.MessageType.AUDIO
    ).count()
    text_inbound = inbound_messages.filter(
        message_type=WhatsAppMessage.MessageType.TEXT
    ).count()
    other_inbound = total_inbound - voice_inbound - text_inbound

    # --- Voice vs text per shop ---
    message_split_per_shop = (
        Company.objects.filter(active=True)
        .annotate(
            text_messages=Count(
                "whatsapp_messages",
                filter=Q(
                    whatsapp_messages__direction=WhatsAppMessage.Direction.INBOUND,
                    whatsapp_messages__message_type=WhatsAppMessage.MessageType.TEXT,
                    whatsapp_messages__timestamp__gte=since,
                ),
                distinct=True,
            ),
            voice_messages=Count(
                "whatsapp_messages",
                filter=Q(
                    whatsapp_messages__direction=WhatsAppMessage.Direction.INBOUND,
                    whatsapp_messages__message_type=WhatsAppMessage.MessageType.AUDIO,
                    whatsapp_messages__timestamp__gte=since,
                ),
                distinct=True,
            ),
        )
        .order_by("-voice_messages")
    )

    # --- Active shops per week ---
    weeks = []
    for i in range(min(days_back // 7, 12)):
        week_end = now - timedelta(weeks=i)
        week_start = week_end - timedelta(weeks=1)
        active_count = (
            Company.objects.filter(
                whatsapp_messages__direction=WhatsAppMessage.Direction.INBOUND,
                whatsapp_messages__timestamp__gte=week_start,
                whatsapp_messages__timestamp__lt=week_end,
            )
            .distinct()
            .count()
        )
        weeks.append(
            {
                "label": week_start.strftime("%b %d") + " - " + week_end.strftime("%b %d"),
                "active_shops": active_count,
            }
        )
    weeks.reverse()

    # --- Build display names for all active shops ---
    active_companies = Company.objects.filter(active=True)
    owner_phones = dict(
        UserProfile.objects.filter(
            company__in=active_companies,
            role=UserProfile.Role.OWNER,
        ).values_list("company_id", "phone_number")
    )
    display_names = {}
    for c in active_companies:
        detail = owner_phones.get(c.id, c.slug)
        display_names[c.id] = f"{c.name} ({detail})"

    # Attach display_name and computed ratios to each queryset's objects
    for qs in [sales_per_shop, message_split_per_shop]:
        for shop in qs:
            shop.display_name = display_names.get(shop.id, shop.name)

    for shop in sales_per_shop:
        shop.msgs_per_sale = (
            round(shop.inbound_messages / shop.total_sales, 1) if shop.total_sales > 0 else None
        )
        shop.mistake_pct = (
            round(shop.mistake_count / shop.confirmed_sales * 100) if shop.confirmed_sales > 0 else None
        )
        shop.first_sale_date, shop.activated = _compute_activation(shop, since)

    shops_with_sales = [s for s in sales_per_shop if s.first_sale_date is not None]
    activated_count = sum(1 for s in shops_with_sales if s.activated)

    context = {
        "title": "Pilot Metrics",
        "days_back": days_back,
        "since": since,
        # Activation summary
        "activated_count": activated_count,
        "shops_with_sales_count": len(shops_with_sales),
        # Sales
        "sales_per_shop": sales_per_shop,
        # Messages
        "total_inbound": total_inbound,
        "voice_inbound": voice_inbound,
        "text_inbound": text_inbound,
        "other_inbound": other_inbound,
        "voice_pct": round(voice_inbound / total_inbound * 100, 1) if total_inbound else 0,
        "message_split_per_shop": message_split_per_shop,
        # Active shops
        "weeks": weeks,
    }

    return render(request, "admin/pilot_metrics.html", context)
