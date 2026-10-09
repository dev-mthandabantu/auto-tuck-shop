"""Waitlist onboarding, language selection, and approval flows."""

from __future__ import annotations

import hashlib
import logging
import re

from django.contrib.auth.models import User
from django.db import close_old_connections, IntegrityError
from django.utils import timezone

from apps.core.models import Company, UserProfile, WaitlistEntry
from apps.whatsapp.models import WhatsAppMessage
from django.conf import settings

from apps.whatsapp.services.webhook_handler import (
    ADMIN_PHONE_NUMBER,
    ADMIN_PHONE_NUMBERS,
    db_sync_to_async,
    send_to_admins,
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

# A typed answer to "Is *X* your shop's name?" while that question is open.
_TYPED_YES_PATTERN = re.compile(r"^\s*(yes|yeah|yep|ehe|hongu|ndizvo|ndiro|correct)\s*[.!]*\s*$", re.IGNORECASE)
_TYPED_NO_PATTERN = re.compile(r"^\s*(no|nope|kwete|aiwa|haiwa)\s*[.!]*\s*$", re.IGNORECASE)


def _name_tag(candidate: str) -> str:
    """Ties a Yes/No button to the exact text it asked about, so a tap on an older question
    can never save a newer candidate."""
    return hashlib.sha1(candidate.encode("utf-8")).hexdigest()[:8]


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
def _mark_admin_alerted(entry_id: int) -> None:
    WaitlistEntry.objects.filter(id=entry_id).update(admin_alerted_at=timezone.now())


@db_sync_to_async
def _update_waitlist_language(entry_id: int, language: str) -> None:
    WaitlistEntry.objects.filter(id=entry_id).update(language=language)


@db_sync_to_async
def _update_profile_language(profile_id: int, language: str) -> None:
    UserProfile.objects.filter(id=profile_id).update(language=language)


@db_sync_to_async
def _claim_waitlist_company_name(entry_id: int, company_name: str, only_if_candidate: str | None = None) -> bool:
    """Set the shop name only if none is set yet (and, for a Yes tap, only if the candidate
    is still the one that was asked about), in one conditional UPDATE. True if this call set
    it, so the admin is alerted exactly once even if two messages or taps race (#423)."""
    rows = WaitlistEntry.objects.filter(id=entry_id, company_name="", status=WaitlistEntry.Status.PENDING)
    if only_if_candidate is not None:
        rows = rows.filter(pending_name_candidate=only_if_candidate)
    return rows.update(company_name=company_name[:200], pending_name_candidate="") == 1


@db_sync_to_async
def _set_pending_name_candidate(entry_id: int, text: str) -> bool:
    """False if a name is already set (or the entry was decided), so no question is asked."""
    return WaitlistEntry.objects.filter(
        id=entry_id, company_name="", status=WaitlistEntry.Status.PENDING
    ).update(pending_name_candidate=text) == 1


@db_sync_to_async
def _clear_pending_name_candidate(entry_id: int, only_if_candidate: str) -> bool:
    return WaitlistEntry.objects.filter(
        id=entry_id, pending_name_candidate=only_if_candidate
    ).update(pending_name_candidate="") == 1


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
    # phone_number is unique. A previously-removed assistant's row still
    # occupies it (deactivated, not deleted) — reactivate and re-home that
    # row instead of inserting a new one, which would hit the unique
    # constraint. handle_add_assistant already confirmed no *active* profile
    # exists for this number before calling here.
    try:
        existing = UserProfile.objects.select_related("user").get(phone_number=phone_number)
        existing.user.is_active = True
        existing.user.save(update_fields=["is_active"])
        existing.company = company
        existing.role = UserProfile.Role.ASSISTANT
        existing.save(update_fields=["company", "role"])
        profile = existing
    except UserProfile.DoesNotExist:
        username = "".join(c for c in phone_number if c.isalnum())
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1
        user = User.objects.create_user(username=username)
        profile = UserProfile.objects.create(
            user=user,
            company=company,
            role=UserProfile.Role.ASSISTANT,
            phone_number=phone_number,
        )

    # Link any pre-add inbound messages from this phone number — same bug
    # class as #233 (waitlist approval), just via the assistant-add path
    # instead. See apps.core.services.approve_waitlist_entry for the original
    # fix this mirrors.
    #
    # Deliberately NOT touching company.first_message_date here, unlike the
    # owner-approval path: this phone number is an assistant, not the owner,
    # so their pre-add message history isn't the shop's own history — using
    # it to backdate first_message_date would be wrong (an assistant added
    # today could have messaged the bot, unrelated to this shop, years ago).
    # It's also unreachable in practice: the "add assistant" message that
    # triggers this function always sets first_message_date first, via
    # _record_inbound_message, before intent handling ever reaches here.
    WhatsAppMessage.objects.filter(
        phone_number=phone_number, company__isnull=True
    ).update(company=company, user_profile=profile)

    return profile


@db_sync_to_async
def _deactivate_assistant(user_profile_id: int) -> None:
    User.objects.filter(profile__id=user_profile_id).update(is_active=False)


ADMIN_WAITLIST_URL = "https://auto-tuck-shop.fly.dev/admin/core/waitlistentry/"


def _secondary_admins() -> list[str]:
    """Admin numbers other than the primary (see settings.ADMIN_PHONE_NUMBERS)."""
    return [n for n in ADMIN_PHONE_NUMBERS if n != ADMIN_PHONE_NUMBER]


async def _notify_secondary_admins(entry: WaitlistEntry) -> None:
    """Tell the other admins about a signup, without Approve/Reject buttons.

    Only the primary admin gets the interactive message: WaitlistEntry stores a
    single confirmation_message_sid, and the approve/reject handler finds the
    entry by matching it, so a second recipient's buttons would resolve to
    nothing and reply "already processed" on a still-pending entry. Rather than
    ship a dead control, secondary admins get the same information plus the
    admin-panel link and action there (#388).
    """
    recipients = _secondary_admins()
    if not recipients:
        return
    message = t(
        "waitlist_admin.new_request_secondary",
        phone=entry.phone_number,
        message=entry.first_message[:100] if entry.first_message else "(none)",
        admin_url=ADMIN_WAITLIST_URL,
    )
    from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
    client = get_whatsapp_client()
    await send_to_admins(
        lambda recipient: client.send_message(recipient, message),
        recipients=recipients,
    )


async def _send_waitlist_admin_notification(entry: WaitlistEntry) -> None:
    # Template first, buttons as a bonus (#387) — buttons are free-form and
    # only deliver within Meta's 24h window, which is closed most of the
    # time in practice. Never let a failure here propagate silently (#339).
    try:
        await _send_admin_notification_template(entry)
        await _mark_admin_alerted(entry.id)
    except Exception:
        logger.exception(
            "Admin waitlist template notification failed for entry %s (%s)",
            entry.id,
            entry.phone_number,
        )

    message = t(
        "waitlist_admin.new_request",
        phone=entry.phone_number,
        message=entry.first_message[:100] if entry.first_message else "(none)",
    )
    buttons = [
        {"id": f"waitlist_approve_{entry.id}", "title": t("waitlist_admin.btn_approve")},
        {"id": f"waitlist_reject_{entry.id}", "title": t("waitlist_admin.btn_reject")},
    ]
    # Secondary admins are notified regardless of how the primary send goes —
    # the whole point of #388 is that one unreachable admin must not mean
    # nobody else hears about the signup.
    try:
        await _notify_secondary_admins(entry)
    except Exception:
        logger.exception(
            "Secondary admin waitlist notification failed for entry %s (%s)",
            entry.id,
            entry.phone_number,
        )

    try:
        message_sid = await _send_response_with_buttons(ADMIN_PHONE_NUMBER, message, buttons)
    except Exception:
        logger.warning(
            "Admin waitlist button notification raised for entry %s (%s)",
            entry.id,
            entry.phone_number,
        )
        message_sid = None

    if message_sid:
        await _store_waitlist_response_message_sid(entry.id, message_sid)
        await _mark_admin_alerted(entry.id)


async def _send_admin_notification_template(entry: WaitlistEntry) -> None:
    # Approved by Meta, see #281.
    #
    # TODO(#387): language_code defaults to "en" — unconfirmed whether
    # ats_admin_new_signup needs a different one. Needs Sentry/Meta access.
    from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
    message_excerpt = entry.first_message[:100] if entry.first_message else "(none)"
    components = [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": entry.phone_number},
                {"type": "text", "text": message_excerpt},
                {"type": "text", "text": ADMIN_WAITLIST_URL},
            ],
        }
    ]
    await get_whatsapp_client().send_template_message(
        ADMIN_PHONE_NUMBER, "ats_admin_new_signup", components=components
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


async def _save_shop_name(sender: str, entry: WaitlistEntry, name: str, only_if_candidate: str | None = None) -> bool:
    """Set the name, confirm it to the owner and alert the admin, once."""
    if not await _claim_waitlist_company_name(entry.id, name, only_if_candidate):
        return False
    entry.company_name = name
    entry.pending_name_candidate = ""
    await _send_response(sender, t("waitlist.shop_name_noted", lang=entry.language, shop_name=name))
    await _send_waitlist_admin_notification(entry)
    return True


async def _ask_to_confirm_shop_name(sender: str, entry: WaitlistEntry, candidate: str) -> None:
    """Ask "Is *X* your shop's name?" instead of guessing (#423). The buttons carry a tag of this
    exact text, so only a tap on this question can save it."""
    lang = entry.language
    if not await _set_pending_name_candidate(entry.id, candidate):
        # A name was saved meanwhile (another message got there first): nothing to ask.
        await _send_response(sender, t("waitlist.still_pending", lang=lang))
        return
    tag = _name_tag(candidate)
    buttons = [
        {"id": f"shopname_yes_{entry.id}_{tag}", "title": t("waitlist.btn_name_yes", lang=lang)},
        {"id": f"shopname_no_{entry.id}_{tag}", "title": t("waitlist.btn_name_no", lang=lang)},
    ]
    try:
        sent = await _send_response_with_buttons(sender, t("waitlist.confirm_shop_name", lang=lang, name=candidate), buttons)
    except Exception:
        # e.g. Meta answers 200 with no message id. Treat it like a failed send, so a question the
        # owner never saw can't be confirmed by a later typed "yes" (#424 review).
        logger.exception("Shop name confirmation buttons failed for waitlist entry %s", entry.id)
        sent = None
    if not sent:
        # The buttons didn't go out, so nothing can confirm this candidate: drop it, ask plainly.
        await _clear_pending_name_candidate(entry.id, candidate)
        await _send_response(sender, t("waitlist.greeting_prompt_shop_name", lang=lang))


async def process_shop_name_button_async(action: str, entry_id: int, tag: str, sender: str) -> None:
    """Yes / No on "Is *X* your shop's name?" (#423)."""
    entry = await _get_waitlist_entry(entry_id)
    if not entry or entry.phone_number != _extract_phone_number(sender):
        logger.warning("Shop name button %s for entry %s from a different number", action, entry_id)
        return
    lang = entry.language
    if entry.status == WaitlistEntry.Status.REJECTED:
        await _send_response(sender, t("waitlist.rejected", lang=lang))
        return
    if entry.status != WaitlistEntry.Status.PENDING:
        return  # approved: an old question no longer means anything, and the shop is set up
    if entry.company_name:
        await _send_response(sender, t("waitlist.still_pending", lang=lang))
        return
    candidate = entry.pending_name_candidate
    if not candidate or _name_tag(candidate) != tag:
        await _send_response(sender, t("waitlist.shop_name_stale", lang=lang))
        return
    if action == "yes":
        if not await _save_shop_name(sender, entry, candidate, only_if_candidate=candidate):
            await _send_response(sender, t("waitlist.still_pending", lang=lang))
    elif await _clear_pending_name_candidate(entry.id, candidate):
        await _send_response(sender, t("waitlist.shop_name_ask_again", lang=lang))


async def process_waitlisted_message_async(sender: str, text: str, waitlist_entry: WaitlistEntry) -> None:
    lang = waitlist_entry.language
    if waitlist_entry.status == WaitlistEntry.Status.PENDING:
        stripped = text.strip()
        candidate = waitlist_entry.pending_name_candidate
        if not waitlist_entry.company_name and candidate and _TYPED_YES_PATTERN.match(stripped):
            # Typed "Yes" instead of tapping: answer the open question, not a new one.
            await process_shop_name_button_async("yes", waitlist_entry.id, _name_tag(candidate), sender)
        elif not waitlist_entry.company_name and candidate and _TYPED_NO_PATTERN.match(stripped):
            await process_shop_name_button_async("no", waitlist_entry.id, _name_tag(candidate), sender)
        elif not waitlist_entry.company_name and stripped and (
            _GREETING_PATTERN.match(stripped) or _TYPED_YES_PATTERN.match(stripped) or _TYPED_NO_PATTERN.match(stripped)
        ):
            # A greeting, or a yes/no with no question open: not a name, ask for it.
            await _send_response(sender, t("waitlist.greeting_prompt_shop_name", lang=lang))
        elif not waitlist_entry.company_name and stripped:
            # Always confirm (#423): any guess about what is a name can be wrong ("We sell
            # groceries", "bread 2"), and a wrong name reaches the admin and the welcome message.
            # One tap per shop, once, and nothing is saved without the owner's Yes.
            await _ask_to_confirm_shop_name(sender, waitlist_entry, stripped[:200])
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
    # Only if the admin hasn't been told yet: tapping a language button after the name was saved
    # (or tapping it twice) must not send the signup alert again (#424 review).
    entry = await _get_waitlist_entry(entry_id)
    if entry and entry.company_name and not entry.admin_alerted_at:
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
            from apps.whatsapp.services.nudge_service import _template_name_for_lang
            components = [{"type": "body", "parameters": [{"type": "text", "text": company.name}]}]
            template_name = _template_name_for_lang("ats_approval_welcome", lang)
            await get_whatsapp_client().send_template_message(entry.phone_number, template_name, components=components)
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
    if existing and existing.user.is_active:
        await _send_response(sender, t("assistant.already_registered", lang=lang, phone=phone_number))
        return
    await _create_assistant(phone_number, user_profile.company)
    await _send_response(sender, t("assistant.added", lang=lang, phone=phone_number, company=user_profile.company.name))
    if settings.USE_TEMPLATE_MESSAGES:
        try:
            from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
            from apps.whatsapp.services.nudge_service import _template_name_for_lang
            components = [{"type": "body", "parameters": [{"type": "text", "text": user_profile.company.name}]}]
            template_name = _template_name_for_lang("ats_assistant_welcome", lang)
            sent = await get_whatsapp_client().send_template_message(
                phone_number, template_name, components=components
            )
            if sent:
                return
        except Exception:
            logger.exception("Failed to send assistant welcome template")

    await _send_response(phone_number, t("assistant.welcome", lang=lang, company=user_profile.company.name))


async def handle_remove_assistant(sender: str, text: str, user_profile, result) -> None:
    lang = user_profile.language if user_profile else DEFAULT_LANGUAGE
    if not user_profile or user_profile.role != UserProfile.Role.OWNER:
        await _send_response(sender, t("assistant.not_owner", lang=lang))
        return
    phone_number = result.phone_number
    if not phone_number:
        await _send_response(sender, t("assistant.missing_phone", lang=lang))
        return
    target = await _get_profile_by_phone(phone_number)
    if not target or target.company_id != user_profile.company_id or not target.user.is_active:
        await _send_response(sender, t("assistant.not_found", lang=lang, phone=phone_number))
        return
    if target.role != UserProfile.Role.ASSISTANT:
        await _send_response(sender, t("assistant.cannot_remove_owner", lang=lang))
        return
    await _deactivate_assistant(target.pk)
    await _send_response(sender, t("assistant.removed", lang=lang, phone=phone_number, company=user_profile.company.name))
    await _send_response(phone_number, t("assistant.removed_notice", lang=target.language or DEFAULT_LANGUAGE))


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


def handle_shop_name_button_action(button_id: str, sender: str) -> None:
    """shopname_{yes|no}_{entry_id}_{tag}"""
    try:
        close_old_connections()
        _, action, entry_id, tag = button_id.split("_", 3)
        if action not in ("yes", "no"):
            raise ValueError(action)
        run_async(process_shop_name_button_async(action, int(entry_id), tag, sender))
    except ValueError:
        logger.warning("Malformed shop name button id: %s", button_id)
    except Exception as e:
        logger.exception(f"Error handling shop name button {button_id}: {e}")


def handle_waitlist_button_action(action: str, sender: str, original_message_sid: str | None = None) -> None:
    try:
        close_old_connections()
        run_async(process_waitlist_button_action_async(action, sender, original_message_sid))
    except Exception as e:
        logger.exception(f"Error handling waitlist {action}: {e}")
