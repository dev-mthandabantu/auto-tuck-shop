"""Pilot health checks and alert formatting."""

from __future__ import annotations

import logging
from datetime import timedelta

import sentry_sdk
from django.conf import settings
from django.utils import timezone

from apps.core.models import Company, UserProfile
from apps.sales.models import Sale

logger = logging.getLogger(__name__)

TEST_PHONES = {"+27644178150", "+27610869293"}


def _owner_phone(company) -> str | None:
    owner = UserProfile.objects.filter(company=company, role=UserProfile.Role.OWNER).first()
    return owner.phone_number if owner else None


def _recent_confirmed_sales_count(company, since) -> int:
    return Sale.objects.filter(
        company=company,
        status=Sale.Status.CONFIRMED,
        sale_timestamp__gte=since,
    ).count()


def build_pilot_health_snapshot(days: int = 7, min_active_shops: int = 1, now=None) -> dict:
    """Return the current pilot health snapshot.

    Real pilot shops exclude known test accounts. A shop is counted as active
    when it has at least one confirmed sale in the lookback window.
    """

    now = now or timezone.now()
    since = now - timedelta(days=days)

    real_shops: list[dict] = []
    active_shops: list[dict] = []

    for company in Company.objects.filter(active=True).order_by("name"):
        owner_phone = _owner_phone(company)
        if (owner_phone or "") in TEST_PHONES:
            continue

        recent_sales = _recent_confirmed_sales_count(company, since)
        row = {
            "id": company.id,
            "name": company.name,
            "owner_phone": owner_phone,
            "recent_sales": recent_sales,
        }
        real_shops.append(row)
        if recent_sales > 0:
            active_shops.append(row)

    quiet_shops = [shop for shop in real_shops if shop["recent_sales"] == 0]
    alert_needed = len(active_shops) < min_active_shops

    return {
        "days": days,
        "since": since,
        "min_active_shops": min_active_shops,
        "real_shop_count": len(real_shops),
        "active_count": len(active_shops),
        "active_shops": active_shops,
        "quiet_shops": quiet_shops,
        "alert_needed": alert_needed,
    }


def _format_shop_list(shops: list[dict], *, include_counts: bool = False, limit: int = 5) -> str:
    if not shops:
        return "none"

    rendered = []
    for shop in shops[:limit]:
        if include_counts:
            rendered.append(f"{shop['name']} ({shop['recent_sales']})")
        else:
            rendered.append(shop["name"])

    remaining = len(shops) - limit
    if remaining > 0:
        rendered.append(f"+{remaining} more")

    return ", ".join(rendered)


def format_pilot_health_message(snapshot: dict) -> str:
    days = snapshot["days"]
    min_active_shops = snapshot["min_active_shops"]
    active_count = snapshot["active_count"]
    real_shop_count = snapshot["real_shop_count"]
    active_shops = snapshot["active_shops"]
    quiet_shops = snapshot["quiet_shops"]

    headline = (
        f"ATS pilot health alert: {active_count}/{real_shop_count} real shops recorded a confirmed "
        f"sale in the last {days} days (threshold: {min_active_shops})."
    )
    if real_shop_count == 0:
        headline = f"ATS pilot health alert: no real pilot shops are configured for the last {days} days window."

    parts = [headline]

    if active_shops:
        parts.append(f"Active: {_format_shop_list(active_shops, include_counts=True)}")
    if quiet_shops:
        parts.append(f"Quiet: {_format_shop_list(quiet_shops)}")

    return "\n".join(parts)


async def send_pilot_health_alert(snapshot: dict) -> dict:
    """Notify a human when the pilot looks unhealthy.

    Prefers WhatsApp to the admin number. Falls back to Sentry if WhatsApp is
    unavailable or the message cannot be delivered.
    """

    message = format_pilot_health_message(snapshot)
    admin_phone = settings.ADMIN_PHONE_NUMBER

    if admin_phone:
        try:
            from apps.whatsapp.services.whatsapp_client import get_whatsapp_client

            sent = await get_whatsapp_client().send_message(admin_phone, message)
            if sent:
                logger.warning("Pilot health alert sent to admin WhatsApp number %s", admin_phone)
                return {"channel": "whatsapp", "sent": True, "message": message}
        except Exception:
            logger.exception("Failed to send pilot health alert to admin WhatsApp number %s", admin_phone)

    sentry_sdk.capture_message(message, level="error")
    logger.error("Pilot health alert captured in Sentry")
    return {"channel": "sentry", "sent": True, "message": message}