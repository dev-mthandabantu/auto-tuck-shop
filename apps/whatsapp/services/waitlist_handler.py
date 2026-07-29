"""Waitlist onboarding, language selection, and approval flows."""

from __future__ import annotations

import logging
import re

from django.contrib.auth.models import User
from django.db import close_old_connections, IntegrityError

from apps.core.models import Company, UserProfile, WaitlistEntry
from django.conf import settings

from apps.whatsapp.services.webhook_handler import (
    ADMIN_PHONE_NUMBER,
    db_sync_to_async,
    DEFAULT_LANGUAGE,
    _extract_phone_number,
    _get_profile_by_phone,
    _send_response,
    _send_response_with_buttons,
    run_async,
    t,
)

logger = logging.getLogger(__name__)

# Greetings sent as the shop-name prompt response — reject rather than
# capture verbatim as company_name (see #256, e.g. a shop named "Hello").
_GREETING_PATTERN = re.compile(
    r"^\s*(hi|hie|hey|hello|makadii|mhoro|yo)\s*[.!?]?\s*$", re.IGNORECASE
)


@db_sync_to_async
def _create_waitlist_entry(phone_number: str, first_message: str) -> tuple[WaitlistEntry, bool]:
    try:
        entry, created = WaitlistEntry.objects.get_or_create(
            phone_number=phone_number,
            defaults={"first_message": first_message},
        )
        if not created and not entry.first_message:
            entry.first_message = first_message
            entry.save(update_fields=["first_message"])
        return entry, created
    except IntegrityError:
        # Lost the race — another request inserted first
        return WaitlistEntry.objects.get(phone_number=phone_number), False


@db_sync_to_async
def _get_waitlist_entry(entry_id: int) -> WaitlistEntry | None:
    try:
        return WaitlistEntry.objects.get(id=entry_id)
    except WaitlistEntry.DoesNotExist:
        return None


@db_sync_to_async
def _store_waitlist_response_message_sid(entry_id: int, message_sid: str) -> None:
    WaitlistEntry.objects.filter(id=entry_id).update(confirmation_message_sid=message_sid)


@db_sync_to_async
def _update_waitlist_language(entry_id: int, language: str) -> None:
    WaitlistEntry.objects.filter(id=entry_id).update(language=language)


@db_sync_to_async
def _update_profile_language(profile_id: int, language: str) -> None:
    UserProfile.objects.filter(id=profile_id).update(language=language)


@db_sync_to_async
def _update_waitlist_company_name(entry_id: int, company_name: str) -> None:
    WaitlistEntry.objects.filter(id=entry_id).update(company_name=company_name)


@db_sync_to_async
def _get_and_update_waitlist_entry(original_message_sid: str | None, action: str) -> WaitlistEntry | None:
    if not original_message_sid:
        return None
    try:
        entry = WaitlistEntry.objects.get(
            confirmation_message_sid=original_message_sid,
            status=WaitlistEntry.Status.PENDING,
        )
        entry.status = WaitlistEntry.Status.APPROVED if action == "approve" else WaitlistEntry.Status.REJECTED
        entry.save(update_fields=["status"])
        return entry
    except WaitlistEntry.DoesNotExist:
        return None


@db_sync_to_async
def _approve_waitlist_entry(entry: WaitlistEntry) -> tuple[Company, UserProfile]:
    from apps.core.services import approve_waitlist_entry
    return approve_waitlist_entry(entry)


@db_sync_to_async
def _create_assistant(phone_number: str, company) -> UserProfile:
    username = "".join(c for c in phone_number if c.isalnum())
    base_username = username
    counter = 1
    while User.objects.filter(username=username).exists():
        username = f"{base_username}_{counter}"
        counter += 1
    user = User.objects.create_user(username=username)
    return UserProfile.objects.create(
        user=user,
        company=company,
        role=UserProfile.Role.ASSISTANT,
        phone_number=phone_number,
    )


async def _send_waitlist_admin_notification(entry: WaitlistEntry) -> None:
    message = t(
        "waitlist_admin.new_request",
        phone=entry.phone_number,
        message=entry.first_message[:100] if entry.first_message else "(none)",
    )
    buttons = [
        {"id": f"waitlist_approve_{entry.id}", "title": t("waitlist_admin.btn_approve")},
        {"id": f"waitlist_reject_{entry.id}", "title": t("waitlist_admin.btn_reject")},
    ]
    message_sid = await _send_response_with_buttons(ADMIN_PHONE_NUMBER, message, buttons)
    if message_sid:
        await _store_waitlist_response_message_sid(entry.id, message_sid)
    else:
        # Button send failed (likely outside 24h window). Fall back to ats_reengagement
        # template — it will deliver reliably and prompt Brighton to check the admin panel.
        # Replace with ats_admin_new_signup once that template is approved (#199).
        logger.warning(
            "Admin waitlist button notification failed for entry %s (%s) — falling back to template",
            entry.id,
            entry.phone_number,
        )
        from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
        components = [{"type": "body", "parameters": [{"type": "text", "text": "Brighton"}]}]
        await get_whatsapp_client().send_template_message(
            ADMIN_PHONE_NUMBER, "ats_reengagement", components=components
        )


async def process_new_waitlist_entry_async(sender: str, text: str) -> None:
    phone_number = _extract_phone_number(sender)
    entry, created = await _create_waitlist_entry(phone_number, text)
    if not created:
        logger.info("Duplicate new-user message from %s, skipping prompt", phone_number)
        return
    buttons = [
        {"id": f"lang_en_{entry.id}", "title": t("language.btn_en")},
        {"id": f"lang_sn_{entry.id}", "title": t("language.btn_sn")},
    ]
    message_sid = await _send_response_with_buttons(sender, t("language.prompt"), buttons)
    if message_sid:
        await _store_waitlist_response_message_sid(entry.id, message_sid)
    if text.strip():
        # Their first message may have been a real sale attempt — acknowledge
        # it instead of silently discarding it (see #255).
        await _send_response(sender, t("waitlist.first_message_noted"))


async def process_waitlisted_message_async(sender: str, text: str, waitlist_entry: WaitlistEntry) -> None:
    lang = waitlist_entry.language
    if waitlist_entry.status == WaitlistEntry.Status.PENDING:
        stripped = text.strip()
        if not waitlist_entry.company_name and stripped and _GREETING_PATTERN.match(stripped):
            await _send_response(sender, t("waitlist.greeting_prompt_shop_name", lang=lang))
        elif not waitlist_entry.company_name and stripped:
            await _update_waitlist_company_name(waitlist_entry.id, stripped)
            waitlist_entry.company_name = stripped
            await _send_response(sender, t("waitlist.shop_name_noted", lang=lang, shop_name=stripped))
            await _send_waitlist_admin_notification(waitlist_entry)
        else:
            await _send_response(sender, t("waitlist.still_pending", lang=lang))
    elif waitlist_entry.status == WaitlistEntry.Status.REJECTED:
        await _send_response(sender, t("waitlist.rejected", lang=lang))


async def process_language_button_async(lang: str, entry_id: int, sender: str) -> None:
    await _update_waitlist_language(entry_id, lang)
    phone_number = _extract_phone_number(sender)
    profile = await _get_profile_by_phone(phone_number)
    if profile:
        await _update_profile_language(profile.id, lang)
    await _send_response(sender, t("language.confirmed", lang=lang))
    await _send_response(sender, t("waitlist.welcome", lang=lang))

    # Notify admin now only if user already provided a shop name.
    # Otherwise notification fires when shop name arrives (process_waitlisted_message_async).
    entry = await _get_waitlist_entry(entry_id)
    if entry and entry.company_name:
        await _send_waitlist_admin_notification(entry)


async def process_waitlist_button_action_async(
    action: str,
    sender: str,
    original_message_sid: str | None = None,
) -> None:
    entry = await _get_and_update_waitlist_entry(original_message_sid, action)
    if not entry:
        await _send_response(sender, t("waitlist.already_processed"))
        return

    lang = entry.language

    if action == "approve":
        company, profile = await _approve_waitlist_entry(entry)
        await _send_response(sender, t("waitlist_admin.approved", phone=entry.phone_number, company=company.name))
        if settings.USE_TEMPLATE_MESSAGES:
            from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
            components = [{"type": "body", "parameters": [{"type": "text", "text": company.name}]}]
            await get_whatsapp_client().send_template_message(entry.phone_number, "ats_approval_welcome", components=components)
        else:
            await _send_response(entry.phone_number, t("approval.welcome", lang=lang, company=company.name))
    else:
        await _send_response(sender, t("waitlist_admin.rejected", phone=entry.phone_number))
        await _send_response(entry.phone_number, t("waitlist.rejected", lang=lang))


async def handle_add_assistant(sender: str, text: str, user_profile, result) -> None:
    lang = user_profile.language if user_profile else DEFAULT_LANGUAGE
    if not user_profile or user_profile.role != UserProfile.Role.OWNER:
        await _send_response(sender, t("assistant.not_owner", lang=lang))
        return
    phone_number = result.phone_number
    if not phone_number:
        await _send_response(sender, t("assistant.missing_phone", lang=lang))
        return
    existing = await _get_profile_by_phone(phone_number)
    if existing:
        await _send_response(sender, t("assistant.already_registered", lang=lang, phone=phone_number))
        return
    await _create_assistant(phone_number, user_profile.company)
    await _send_response(sender, t("assistant.added", lang=lang, phone=phone_number, company=user_profile.company.name))
    if settings.USE_TEMPLATE_MESSAGES:
        try:
            from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
            components = [{"type": "body", "parameters": [{"type": "text", "text": user_profile.company.name}]}]
            sent = await get_whatsapp_client().send_template_message(
                phone_number, "ats_assistant_welcome", components=components
            )
            if sent:
                return
        except Exception:
            logger.exception("Failed to send assistant welcome template")

    await _send_response(phone_number, t("assistant.welcome", lang=lang, company=user_profile.company.name))


def handle_new_waitlist_entry(sender: str, text: str) -> None:
    try:
        close_old_connections()
        run_async(process_new_waitlist_entry_async(sender, text))
    except Exception as e:
        logger.exception(f"Error handling new waitlist entry for {sender}: {e}")


def handle_waitlisted_message(sender: str, text: str, waitlist_entry: WaitlistEntry) -> None:
    try:
        close_old_connections()
        run_async(process_waitlisted_message_async(sender, text, waitlist_entry))
    except Exception as e:
        logger.exception(f"Error handling waitlisted message for {sender}: {e}")


def handle_language_button_action(lang: str, entry_id: int, sender: str) -> None:
    try:
        close_old_connections()
        run_async(process_language_button_async(lang, entry_id, sender))
    except Exception as e:
        logger.exception(f"Error handling language selection for {sender}: {e}")


def handle_waitlist_button_action(action: str, sender: str, original_message_sid: str | None = None) -> None:
    try:
        close_old_connections()
        run_async(process_waitlist_button_action_async(action, sender, original_message_sid))
    except Exception as e:
        logger.exception(f"Error handling waitlist {action}: {e}")
