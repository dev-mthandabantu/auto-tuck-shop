"""GTM drip nudge service — sends daily nudges to shops that haven't recorded recently."""

from __future__ import annotations

import logging
import re
from datetime import date, time, timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

logger = logging.getLogger(__name__)

# CTA definitions: key, eligible_ctas function gate, param-builder
_ONBOARDING_DAYS = 14

# Shona-language template names — submitted to Meta as distinct template
# names rather than a language variant of the English template (Meta
# normally supports one template name with multiple language variants
# selected via language_code, but these were submitted as separate names —
# see #235). Falls back to the English template name if no Shona variant
# has been submitted yet for a given template.
_SHONA_TEMPLATE_NAMES = {
    "ats_daily_nudge": "_ats_daily_nudge_shona_version",
    "ats_onboarding_followup": "ats_onboarding_followup_shona_version",
    "ats_reengagement": "ats_reengagement_shona_version",
    "ats_daily_summary": "ats_daily_summary_shona_version",
    "ats_nudge_onboarding_first_sale": "ats_nudge_onboarding_first_sale_shona_version",
    "ats_nudge_onboarding_shona_tip": "ats_nudge_onboarding_shona_tip_shona_version",
    "ats_nudge_retention_no_sales_today": "ats_nudge_retention_no_sales_today_shona_version",
    "ats_nudge_retention_streak": "ats_nudge_retention_streak_shona_version",
    "ats_nudge_discovery_weekly_report": "ats_nudge_discovery_weekly_report_shona_version",
    "ats_nudge_discovery_multi_item": "ats_nudge_discovery_multi_item_shona_version",
    "ats_nudge_discovery_photo_upload": "ats_nudge_discovery_photo_upload_shona_version",
    # ats_unnamed_shop_followup itself (English + Shona) is not yet submitted
    # to Meta — see #316. Code/copy ready now, template submission is a
    # separate fast-follow, same pattern as #251's newer CTAs.
    "ats_unnamed_shop_followup": "ats_unnamed_shop_followup_shona_version",
    "ats_approval_welcome": "ats_approval_welcome_shona_version",
    "ats_assistant_welcome": "ats_assistant_welcome_shona_version",
}

# Maps pick_nudge_cta's cta_key to the Meta template submitted for it (#251).
# onboarding_reports is deliberately excluded — its button handling is still
# an open question (see #251 discussion) — falls back to the old fixed
# ats_daily_nudge template until that's resolved.
_NUDGE_TEMPLATE_NAMES = {
    "onboarding_first_sale": "ats_nudge_onboarding_first_sale",
    "onboarding_shona_tip": "ats_nudge_onboarding_shona_tip",
    "retention_no_sales_today": "ats_nudge_retention_no_sales_today",
    "retention_streak": "ats_nudge_retention_streak",
    "discovery_weekly_report": "ats_nudge_discovery_weekly_report",
    "discovery_multi_item": "ats_nudge_discovery_multi_item",
    "discovery_photo_upload": "ats_nudge_discovery_photo_upload",
}


def _template_name_for_lang(base_name: str, lang: str) -> str:
    """Return the Shona-named template variant if lang is Shona and one has
    been submitted, else the base (English) template name."""
    if lang == "sn":
        return _SHONA_TEMPLATE_NAMES.get(base_name, base_name)
    return base_name


def _compute_streak(company) -> int:
    """Count consecutive days with at least one confirmed sale ending today."""
    from apps.sales.models import Sale
    close_old_connections()
    today = timezone.localdate()
    streak = 0
    check = today
    while True:
        has_sale = Sale.objects.filter(
            company=company,
            status="confirmed",
            sale_timestamp__date=check,
        ).exists()
        if not has_sale:
            break
        streak += 1
        check -= timedelta(days=1)
    return streak


def _last_active_date(company) -> date | None:
    from apps.sales.models import Sale
    close_old_connections()
    result = (
        Sale.objects.filter(company=company, status="confirmed")
        .order_by("-sale_timestamp")
        .values_list("sale_timestamp__date", flat=True)
        .first()
    )
    return result


def _features_used(company) -> list[str]:
    from apps.whatsapp.models import WhatsAppMessage
    from apps.sales.models import Sale, SaleItem
    close_old_connections()
    features = []
    msgs = WhatsAppMessage.objects.filter(
        company=company, direction=WhatsAppMessage.Direction.INBOUND
    )
    report_pattern = re.compile(r"how much|report|weekly|monthly|svondo|mwedzi", re.IGNORECASE)
    if msgs.filter(content__iregex=report_pattern.pattern).exists():
        features.append("reports")
    if msgs.filter(message_type=WhatsAppMessage.MessageType.AUDIO).exists():
        features.append("audio")
    if SaleItem.objects.filter(sale__company=company).values("sale").annotate(
        cnt=__import__("django.db.models", fromlist=["Count"]).Count("id")
    ).filter(cnt__gt=1).exists():
        features.append("multi_item")
    return features


def build_shop_context(company) -> dict:
    """Compute nudge context for a company at send time (sync — call via sync_to_async)."""
    today = timezone.localdate()
    days_since_onboarding = (
        (today - company.first_message_date).days if company.first_message_date else None
    )
    streak = _compute_streak(company)
    last_active = _last_active_date(company)
    features_used = _features_used(company)
    return {
        "days_since_onboarding": days_since_onboarding,
        "streak": streak,
        "last_active_days_ago": (today - last_active).days if last_active else None,
        "features_used": features_used,
        "nudge_stage": company.nudge_stage,
    }


def _eligible_ctas(context: dict) -> list[dict]:
    """Return list of {key, text, type} dicts eligible for this shop context."""
    from apps.whatsapp.services.webhook_handler import t
    days = context.get("days_since_onboarding")
    streak = context.get("streak", 0)
    features = context.get("features_used", [])
    eligible = []

    def add(key: str, cta_type: str = "text", **params):
        try:
            text = t(f"nudge.{key}", lang="en", **params)
            eligible.append({"key": key, "text": text, "type": cta_type})
        except (KeyError, IndexError):
            logger.warning("Missing nudge locale key: %s", key)

    # Onboarding (first 2 weeks only)
    if days is not None and days <= _ONBOARDING_DAYS:
        if context.get("last_active_days_ago") is None:
            add("onboarding_first_sale")
        add("onboarding_shona_tip")
        add("onboarding_reports", cta_type="button")

    # Retention — only if shop has prior sales history
    if context.get("last_active_days_ago") is not None:
        add("retention_no_sales_today")
    if streak >= 2:
        add("retention_streak", streak=streak)

    # Discovery — gated on features not yet used
    if "reports" not in features:
        add("discovery_weekly_report")
    add("discovery_multi_item")
    add("discovery_photo_upload")

    # Gated CTAs — enabled via settings flags when features ship
    if getattr(settings, "NUDGE_ENABLE_UNDO_CTA", False):
        eligible.append({"key": "discovery_undo", "text": "You can correct a mistake — just say *undo last sale*", "type": "text"})
    if getattr(settings, "NUDGE_ENABLE_INVENTORY_CTA", False):
        eligible.append({"key": "inventory_restock", "text": "Are you running low on anything? Tell me and I'll track it.", "type": "text"})
    if getattr(settings, "NUDGE_ENABLE_COMMUNITY_CTA", False):
        eligible.append({"key": "community_whatsapp", "text": "Join other Auto Tuck Shop owners on WhatsApp.", "type": "text"})
    if getattr(settings, "NUDGE_ENABLE_OPTIONS_CTA", False):
        eligible.append({"key": "options_menu", "text": "Need help? Reply *help* to see what I can do.", "type": "text"})

    return eligible


async def pick_nudge_cta(context: dict, lang: str) -> dict | None:
    """Ask the LLM to pick the best CTA from the eligible pool. Returns {message, cta_type} or None."""
    from services.openrouter import OpenRouterClient
    from services.openrouter.prompts import build_nudge_picker_prompt
    from apps.whatsapp.services.webhook_handler import t

    eligible = _eligible_ctas(context)
    if not eligible:
        return None

    messages = build_nudge_picker_prompt(context, eligible)
    client = OpenRouterClient()
    try:
        result = await client.parse_json_response(messages)
        cta_key = result.response.get("cta_key")
        params = result.response.get("params") or {}
        if not cta_key:
            return None
        # Find the selected CTA to get its type
        selected = next((c for c in eligible if c["key"] == cta_key), None)
        cta_type = selected["type"] if selected else "text"
        try:
            message = t(f"nudge.{cta_key}", lang=lang, **params)
        except (KeyError, IndexError):
            message = selected["text"] if selected else None
        if not message:
            return None
        return {"cta_key": cta_key, "message": message, "cta_type": cta_type}
    except Exception:
        logger.exception("LLM CTA picker failed — skipping nudge")
        return None


@sync_to_async
def _load_nudge_candidates(today: date):
    from apps.core.models import Company, UserProfile
    close_old_connections()
    companies = (
        Company.objects.filter(active=True, first_message_date__isnull=False)
        .exclude(last_nudge_date=today)
    )
    # Filter to companies that have at least one non-opted-out member
    eligible = []
    for company in companies:
        has_recipient = UserProfile.objects.filter(
            company=company, nudge_opt_out=False
        ).exists()
        if has_recipient:
            eligible.append(company)
    return eligible


@sync_to_async
def _recently_active(company) -> bool:
    from apps.whatsapp.models import WhatsAppMessage
    import datetime as _dt
    close_old_connections()
    cutoff = timezone.make_aware(_dt.datetime.combine(timezone.localdate(), time.min))
    return WhatsAppMessage.objects.filter(
        company=company,
        direction=WhatsAppMessage.Direction.INBOUND,
        timestamp__gte=cutoff,
    ).exists()


@sync_to_async
def _already_messaged_today(company) -> bool:
    """True if any of the four automated nudge/followup mechanisms already
    sent this company a message today — prevents two mechanisms firing
    same-day (see #262)."""
    from apps.core.models import Company
    close_old_connections()
    today = timezone.localdate()
    return Company.objects.filter(pk=company.pk, last_automated_message_date=today).exists()


@sync_to_async
def _mark_automated_message_sent(company) -> None:
    from apps.core.models import Company
    close_old_connections()
    Company.objects.filter(pk=company.pk).update(last_automated_message_date=timezone.localdate())


@sync_to_async
def _company_owner_lang(company) -> str:
    from apps.core.models import UserProfile
    close_old_connections()
    profile = (
        UserProfile.objects.filter(company=company, role=UserProfile.Role.OWNER, user__is_active=True)
        .first()
    )
    return profile.language if profile else "sn"


@sync_to_async
def _record_nudge_sent(company, today: date) -> None:
    from apps.core.models import Company
    close_old_connections()
    Company.objects.filter(pk=company.pk).update(
        last_nudge_date=today,
        nudge_stage=company.nudge_stage + 1,
    )


@sync_to_async
def _get_non_opted_out_phones(company) -> list[str]:
    from apps.core.models import UserProfile
    close_old_connections()
    return list(
        UserProfile.objects.filter(company=company, nudge_opt_out=False)
        .values_list("phone_number", flat=True)
    )


async def _send_nudge(company, cta_key: str, message: str, cta_type: str = "text", lang: str = "en", streak: int = 0) -> bool:
    """Send the nudge to every non-opted-out recipient. Returns True only if all sends succeeded."""
    from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
    from apps.whatsapp.services.webhook_handler import t
    phones = await _get_non_opted_out_phones(company)
    owner_name = await _get_owner_name(company)
    client = get_whatsapp_client()
    all_succeeded = True
    for phone in phones:
        if settings.USE_TEMPLATE_MESSAGES:
            template_name = _template_name_for_lang(_NUDGE_TEMPLATE_NAMES.get(cta_key, "ats_daily_nudge"), lang)
            if cta_key == "retention_streak":
                components = [{"type": "body", "parameters": [{"type": "text", "text": str(streak)}]}]
            elif cta_key in _NUDGE_TEMPLATE_NAMES:
                components = []
            else:
                components = [{"type": "body", "parameters": [{"type": "text", "text": owner_name}]}]
            success = await client.send_template_message(phone, template_name, components=components)
        elif cta_type == "button":
            success = await client.send_message_with_buttons(
                phone,
                message,
                buttons=[
                    {"id": "nudge_reports_yes", "title": t("nudge.btn_reports_yes", lang=lang)},
                    {"id": "nudge_reports_no", "title": t("nudge.btn_reports_no", lang=lang)},
                ],
            )
        else:
            success = await client.send_message(phone, message)
        all_succeeded = all_succeeded and bool(success)
    return all_succeeded


def _in_send_window() -> bool:
    """Return True if current local time is within the nudge send window."""
    now_local = timezone.localtime(timezone.now())
    return settings.NUDGE_SEND_WINDOW_START <= now_local.hour < settings.NUDGE_SEND_WINDOW_END


async def maybe_send_nudges(now=None) -> dict:
    """Check all active companies and send a nudge if due. Called by send_nudges management command."""
    if not _in_send_window():
        logger.debug("Outside nudge send window — skipping")
        return {"sent": [], "skipped": [], "reason": "outside_window"}

    today = timezone.localdate(now) if now else timezone.localdate()
    companies = await _load_nudge_candidates(today)
    sent = []
    skipped = []

    for company in companies:
        if await _recently_active(company):
            logger.debug("Company %s recently active — skipping nudge", company.id)
            skipped.append(company.id)
            continue

        if await _already_messaged_today(company):
            logger.debug("Company %s already messaged today — skipping nudge", company.id)
            skipped.append(company.id)
            continue

        context = await sync_to_async(build_shop_context)(company)
        lang = await _company_owner_lang(company)
        cta = await pick_nudge_cta(context, lang)

        if not cta:
            logger.debug("No eligible CTA for company %s — skipping", company.id)
            skipped.append(company.id)
            continue

        try:
            success = await _send_nudge(
                company,
                cta["cta_key"],
                cta["message"],
                cta_type=cta["cta_type"],
                lang=lang,
                streak=context.get("streak", 0),
            )
        except Exception:
            logger.exception("Failed to send nudge to company %s", company.id)
            skipped.append(company.id)
            continue

        if success:
            await _record_nudge_sent(company, today)
            await _mark_automated_message_sent(company)
            logger.info("Nudge sent to company %s (stage %s)", company.id, company.nudge_stage)
            sent.append(company.id)
        else:
            logger.warning("Nudge send failed for company %s — not advancing nudge_stage", company.id)
            skipped.append(company.id)

    return {"sent": sent, "skipped": skipped}


@sync_to_async
def _load_onboarding_followup_candidates():
    from apps.sales.models import Sale
    from apps.core.models import Company
    close_old_connections()
    today = timezone.localdate()
    companies = Company.objects.filter(
        active=True,
        first_message_date__isnull=False,
        onboarding_followup_sent=False,
    ).exclude(first_message_date=today)
    result = []
    for company in companies:
        has_sale = Sale.objects.filter(company=company, status="confirmed").exists()
        if not has_sale:
            result.append(company)
    return result


@sync_to_async
def _mark_onboarding_followup_sent(company) -> None:
    from apps.core.models import Company
    close_old_connections()
    Company.objects.filter(pk=company.pk).update(onboarding_followup_sent=True)


async def maybe_send_onboarding_followup() -> dict:
    """Send ats_onboarding_followup template to approved shops that have never recorded a sale."""
    from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
    candidates = await _load_onboarding_followup_candidates()
    sent = []
    skipped = []
    client = get_whatsapp_client()
    for company in candidates:
        if await _already_messaged_today(company):
            logger.debug("Company %s already messaged today — skipping onboarding followup", company.id)
            skipped.append(company.id)
            continue
        phones = await _get_non_opted_out_phones(company)
        if not phones:
            skipped.append(company.id)
            continue
        lang = await _company_owner_lang(company)
        template_name = _template_name_for_lang("ats_onboarding_followup", lang)
        try:
            all_succeeded = True
            for phone in phones:
                success = await client.send_template_message(phone, template_name)
                all_succeeded = all_succeeded and bool(success)
            if all_succeeded:
                await _mark_onboarding_followup_sent(company)
                await _mark_automated_message_sent(company)
                logger.info("Onboarding followup sent to company %s", company.id)
                sent.append(company.id)
            else:
                logger.warning("Onboarding followup send failed for company %s — not marking as sent", company.id)
                skipped.append(company.id)
        except Exception:
            logger.exception("Failed to send onboarding followup to company %s", company.id)
            skipped.append(company.id)
    return {"sent": sent, "skipped": skipped}


@sync_to_async
def _load_unnamed_shop_followup_candidates():
    """Shops still on the "Unnamed Shop" placeholder set at approval time
    (see apps.core.services.approve_waitlist_entry) — ask once for the real
    name. #316."""
    from apps.core.models import Company
    close_old_connections()
    return list(
        Company.objects.filter(
            active=True,
            name="Unnamed Shop",
            unnamed_shop_followup_sent=False,
        )
    )


@sync_to_async
def _mark_unnamed_shop_followup_sent(company) -> None:
    from apps.core.models import Company
    close_old_connections()
    Company.objects.filter(pk=company.pk).update(unnamed_shop_followup_sent=True)


async def maybe_send_unnamed_shop_followup() -> dict:
    """Ask shops still stuck on the "Unnamed Shop" placeholder for their real
    name — one-shot, same pattern as maybe_send_onboarding_followup. #316."""
    from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
    candidates = await _load_unnamed_shop_followup_candidates()
    sent = []
    skipped = []
    client = get_whatsapp_client()
    for company in candidates:
        if await _already_messaged_today(company):
            logger.debug("Company %s already messaged today — skipping unnamed-shop followup", company.id)
            skipped.append(company.id)
            continue
        phones = await _get_non_opted_out_phones(company)
        if not phones:
            skipped.append(company.id)
            continue
        lang = await _company_owner_lang(company)
        template_name = _template_name_for_lang("ats_unnamed_shop_followup", lang)
        try:
            all_succeeded = True
            for phone in phones:
                success = await client.send_template_message(phone, template_name)
                all_succeeded = all_succeeded and bool(success)
            if all_succeeded:
                await _mark_unnamed_shop_followup_sent(company)
                await _mark_automated_message_sent(company)
                logger.info("Unnamed-shop followup sent to company %s", company.id)
                sent.append(company.id)
            else:
                logger.warning("Unnamed-shop followup send failed for company %s — not marking as sent", company.id)
                skipped.append(company.id)
        except Exception:
            logger.exception("Failed to send unnamed-shop followup to company %s", company.id)
            skipped.append(company.id)
    return {"sent": sent, "skipped": skipped}


@sync_to_async
def _load_reengagement_candidates():
    from apps.sales.models import Sale
    from apps.core.models import Company
    from django.db import models as django_models
    close_old_connections()
    today = timezone.localdate()
    cutoff = today - timedelta(days=2)
    seven_days_ago = today - timedelta(days=7)
    companies = Company.objects.filter(active=True).filter(
        django_models.Q(last_reengagement_date__isnull=True)
        | django_models.Q(last_reengagement_date__lte=seven_days_ago)
    )
    result = []
    for company in companies:
        last_sale_date = (
            Sale.objects.filter(company=company, status="confirmed")
            .order_by("-sale_timestamp")
            .values_list("sale_timestamp__date", flat=True)
            .first()
        )
        if last_sale_date and last_sale_date <= cutoff:
            result.append(company)
    return result


@sync_to_async
def _get_owner_name(company) -> str:
    from apps.core.models import UserProfile
    close_old_connections()
    profile = (
        UserProfile.objects.filter(company=company, role=UserProfile.Role.OWNER, user__is_active=True)
        .select_related("user")
        .first()
    )
    if profile:
        full_name = profile.user.get_full_name()
        if full_name:
            return full_name
    name = company.name
    if not name or name.lower() == "unnamed shop":
        return "there"
    return name


@sync_to_async
def _mark_reengagement_sent(company) -> None:
    from apps.core.models import Company
    close_old_connections()
    Company.objects.filter(pk=company.pk).update(last_reengagement_date=timezone.localdate())


async def maybe_send_reengagement() -> dict:
    """Send ats_reengagement template to shops that have been silent for 2+ days."""
    from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
    candidates = await _load_reengagement_candidates()
    sent = []
    skipped = []
    client = get_whatsapp_client()
    for company in candidates:
        if await _already_messaged_today(company):
            logger.debug("Company %s already messaged today — skipping reengagement", company.id)
            skipped.append(company.id)
            continue
        phones = await _get_non_opted_out_phones(company)
        if not phones:
            skipped.append(company.id)
            continue
        owner_name = await _get_owner_name(company)
        components = [{"type": "body", "parameters": [{"type": "text", "text": owner_name}]}]
        lang = await _company_owner_lang(company)
        template_name = _template_name_for_lang("ats_reengagement", lang)
        try:
            all_succeeded = True
            for phone in phones:
                success = await client.send_template_message(phone, template_name, components=components)
                all_succeeded = all_succeeded and bool(success)
            if all_succeeded:
                await _mark_reengagement_sent(company)
                await _mark_automated_message_sent(company)
                logger.info("Reengagement sent to company %s", company.id)
                sent.append(company.id)
            else:
                logger.warning("Reengagement send failed for company %s — not marking as sent", company.id)
                skipped.append(company.id)
        except Exception:
            logger.exception("Failed to send reengagement to company %s", company.id)
            skipped.append(company.id)
    return {"sent": sent, "skipped": skipped}


# Days after approval (Company.created_at) to send each zero-contact follow-up
# attempt. Capped at 3 — a shop still silent after day 7 needs personal
# outreach (see #149), not more automated attempts.
_ZERO_CONTACT_FOLLOWUP_DAYS = (2, 5, 7)


@sync_to_async
def _load_zero_contact_followup_candidates():
    from apps.core.models import Company
    close_old_connections()
    now = timezone.now()
    candidates = []
    for days in _ZERO_CONTACT_FOLLOWUP_DAYS:
        cutoff_start = now - timedelta(days=days, hours=1)
        cutoff_end = now - timedelta(days=days)
        candidates.append(
            Company.objects.filter(
                active=True,
                first_message_date__isnull=True,
                created_at__gte=cutoff_start,
                created_at__lt=cutoff_end,
            )
        )
    seen_ids = set()
    result = []
    for qs in candidates:
        for company in qs:
            if company.id not in seen_ids and company.zero_contact_followup_count < len(_ZERO_CONTACT_FOLLOWUP_DAYS):
                seen_ids.add(company.id)
                result.append(company)
    return result


@sync_to_async
def _mark_zero_contact_followup_sent(company) -> None:
    from apps.core.models import Company
    close_old_connections()
    Company.objects.filter(pk=company.pk).update(
        zero_contact_followup_count=company.zero_contact_followup_count + 1,
        last_zero_contact_followup_date=timezone.localdate(),
    )


async def maybe_send_zero_contact_followup() -> dict:
    """Send ats_zero_contact_followup template to shops that were approved but have
    never sent a single message. Fires on day 2, 5, and 7 after approval, then stops —
    still-silent shops after that need personal outreach (see #149), not more nudges."""
    from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
    candidates = await _load_zero_contact_followup_candidates()
    sent = []
    skipped = []
    client = get_whatsapp_client()
    today = timezone.localdate()
    for company in candidates:
        if company.last_zero_contact_followup_date == today:
            skipped.append(company.id)
            continue
        if await _already_messaged_today(company):
            logger.debug("Company %s already messaged today — skipping zero-contact followup", company.id)
            skipped.append(company.id)
            continue
        phones = await _get_non_opted_out_phones(company)
        if not phones:
            skipped.append(company.id)
            continue
        lang = await _company_owner_lang(company)
        try:
            all_succeeded = True
            for phone in phones:
                success = await client.send_template_message(
                    phone, "ats_zero_contact_followup", language_code=lang
                )
                all_succeeded = all_succeeded and bool(success)
            if all_succeeded:
                await _mark_zero_contact_followup_sent(company)
                await _mark_automated_message_sent(company)
                logger.info(
                    "Zero-contact followup sent to company %s (attempt %s/%s)",
                    company.id, company.zero_contact_followup_count + 1, len(_ZERO_CONTACT_FOLLOWUP_DAYS),
                )
                sent.append(company.id)
            else:
                logger.warning("Zero-contact followup send failed for company %s — not marking as sent", company.id)
                skipped.append(company.id)
        except Exception:
            logger.exception("Failed to send zero-contact followup to company %s", company.id)
            skipped.append(company.id)
    return {"sent": sent, "skipped": skipped}
