"""Shared utilities and public entry points for WhatsApp message handling.

Business logic lives in:
  - sale_handler.py     — sale creation and sale button actions
  - waitlist_handler.py — onboarding, language selection, approval
  - media_handler.py    — audio download, transcription, R2 upload
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import django.db.utils
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.models import User
from django.db import close_old_connections, connections
from django.utils import timezone

from apps.core.models import Company, UserProfile, WaitlistEntry
from apps.sales.models import Sale
from apps.sales.services import create_sale_from_parsed_items, PriceOverflowError
from apps.whatsapp.services.message_parser import parse_message_unified
from apps.whatsapp.services.message_lock import get_user_lock
from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
from services.openrouter.client import OpenRouterError
from utils.timing import start_tracking, end_tracking, track

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Async execution helpers
# ---------------------------------------------------------------------------

def run_async(coro):
    """Run an async coroutine safely from both sync and async contexts."""
    try:
        loop = asyncio.get_running_loop()
        logger.info("[DEBUG] Event loop detected, using thread pool")
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            if 'test' in settings.DATABASES['default']['NAME']:
                logger.info("[DEBUG] Test mode detected, waiting for thread pool")
                future.result(timeout=10)
                logger.info("[DEBUG] Thread pool completed")
    except RuntimeError:
        logger.info("[DEBUG] No event loop, using asyncio.run")
        asyncio.run(coro)


def db_sync_to_async(func):
    """Like sync_to_async but closes stale DB connections first."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        close_old_connections()
        return func(*args, **kwargs)
    return sync_to_async(wrapper)


# ---------------------------------------------------------------------------
# Localisation
# ---------------------------------------------------------------------------

_LOCALES_DIR = Path(__file__).parent.parent / "locales"
_ALL_STRINGS = {
    "en": json.loads((_LOCALES_DIR / "en.json").read_text(encoding="utf-8")),
    "sn": json.loads((_LOCALES_DIR / "sn.json").read_text(encoding="utf-8")),
}
DEFAULT_LANGUAGE = "sn"


def t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """Get localised string by dot-notation key, with optional format args."""
    strings = _ALL_STRINGS.get(lang, _ALL_STRINGS[DEFAULT_LANGUAGE])
    keys = key.split(".")
    value = strings
    for k in keys:
        value = value[k]
    return value.format(**kwargs) if kwargs else value


# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

from django.conf import settings
ADMIN_PHONE_NUMBER = settings.ADMIN_PHONE_NUMBER or "+14342183470"


def _extract_phone_number(sender: str) -> str:
    """Normalise sender to +E.164 format."""
    if sender.startswith("whatsapp:"):
        sender = sender[9:]
    if not sender.startswith("+"):
        sender = f"+{sender}"
    return sender


@db_sync_to_async
def _get_profile_by_phone(phone_number: str) -> UserProfile | None:
    try:
        return UserProfile.objects.select_related("user").get(phone_number=phone_number)
    except UserProfile.DoesNotExist:
        return None


@db_sync_to_async
def _upload_to_r2(media_data: bytes, media_id: str, mime_type: str, phone_number: str) -> str | None:
    try:
        from services.storage import R2StorageClient
        client = R2StorageClient()
        return client.upload_media(media_data, media_id, mime_type, phone_number)
    except Exception as e:
        logger.error(f"Failed to upload media to R2: {e}", exc_info=True)
        return None


@db_sync_to_async
def _set_profile_language(user_profile_id: int, language: str) -> None:
    UserProfile.objects.filter(id=user_profile_id).update(language=language)


@db_sync_to_async
def _set_company_name(company_id: int, name: str) -> None:
    Company.objects.filter(id=company_id).update(name=name)


async def _send_response(to: str, message: str, reply_to: str | None = None) -> None:
    client = get_whatsapp_client()
    await client.send_message(to, message, reply_to=reply_to)


async def _send_response_with_buttons(
    to: str, message: str, buttons: list[dict[str, str]], reply_to: str | None = None
) -> str | None:
    client = get_whatsapp_client()
    return await client.send_message_with_buttons(to, message, buttons, reply_to=reply_to)


def _send_fallback_error(sender: str, user_profile: UserProfile | None) -> None:
    """Best-effort reply so an unhandled downstream failure is never fully silent to the user.

    Called from the outer except blocks in the handle_incoming_* entry points, which
    previously only logged — leaving the shop owner with no reply at all on WhatsApp (#284).
    """
    lang = user_profile.language if user_profile else DEFAULT_LANGUAGE
    try:
        run_async(_send_response(sender, t("error.processing_failed", lang=lang)))
    except Exception:
        logger.exception("Failed to send fallback error reply to %s", sender)


# ---------------------------------------------------------------------------
# Public entry points (called from views.py)
# ---------------------------------------------------------------------------

def handle_new_waitlist_entry(sender: str, text: str) -> None:
    from apps.whatsapp.services.waitlist_handler import handle_new_waitlist_entry as _handle
    _handle(sender, text)


def handle_waitlisted_message(sender: str, text: str, waitlist_entry: WaitlistEntry) -> None:
    from apps.whatsapp.services.waitlist_handler import handle_waitlisted_message as _handle
    _handle(sender, text, waitlist_entry)


def handle_language_button_action(lang: str, entry_id: int, sender: str) -> None:
    from apps.whatsapp.services.waitlist_handler import handle_language_button_action as _handle
    _handle(lang, entry_id, sender)


def handle_waitlist_button_action(action: str, sender: str, original_message_sid: str | None = None) -> None:
    from apps.whatsapp.services.waitlist_handler import handle_waitlist_button_action as _handle
    _handle(action, sender, original_message_sid)


def handle_sale_button_action(action: str, sender: str, original_message_sid: str | None = None) -> None:
    from apps.whatsapp.services.sale_handler import handle_sale_button_action as _handle
    _handle(action, sender, original_message_sid)


def handle_nudge_reports_yes(sender: str) -> None:
    """User tapped 'Yes, set it up' on the onboarding_reports nudge — enter closing time setup flow."""
    phone_number = _extract_phone_number(sender)
    run_async(_handle_nudge_reports_yes_async(phone_number))


def handle_nudge_reports_no(sender: str) -> None:
    """User tapped 'Not now' on the onboarding_reports nudge — send a brief acknowledgement."""
    phone_number = _extract_phone_number(sender)
    run_async(_handle_nudge_reports_no_async(phone_number))


async def _handle_nudge_reports_yes_async(phone_number: str) -> None:
    profile = await _get_profile_by_phone(phone_number)
    lang = profile.language if profile else DEFAULT_LANGUAGE
    await _send_response_with_buttons(
        phone_number,
        t("closing.setup_prompt_buttons", lang=lang),
        [
            {"id": "closing_early", "title": t("closing.btn_early", lang=lang)},
            {"id": "closing_mid",   "title": t("closing.btn_mid",   lang=lang)},
            {"id": "closing_late",  "title": t("closing.btn_late",  lang=lang)},
        ],
    )


async def _handle_nudge_reports_no_async(phone_number: str) -> None:
    profile = await _get_profile_by_phone(phone_number)
    lang = profile.language if profile else DEFAULT_LANGUAGE
    await _send_response(phone_number, t("nudge.reports_declined", lang=lang))


from datetime import time as _time

_CLOSING_TIME_BUTTON_MAP = {
    "closing_early": _time(18, 0),  # 6pm → summary at 7pm
    "closing_mid":   _time(19, 0),  # 7pm → summary at 8pm
    "closing_late":  _time(21, 0),  # 9pm → summary at 10pm
}


def handle_closing_time_button_action(sender: str, button_id: str) -> None:
    run_async(_handle_closing_time_button_async(sender, button_id))


async def _handle_closing_time_button_async(sender: str, button_id: str) -> None:
    closing_time = _CLOSING_TIME_BUTTON_MAP.get(button_id)
    if not closing_time:
        return
    phone_number = _extract_phone_number(sender)
    profile = await _get_profile_by_phone(phone_number)
    if not profile or not profile.company_id:
        return
    lang = profile.language if profile else DEFAULT_LANGUAGE
    from apps.whatsapp.services.business_reports import set_company_normal_closing_time
    await set_company_normal_closing_time(profile.company_id, closing_time)
    from datetime import datetime as _dt, timedelta as _td
    today = timezone.localdate()
    summary_time = (_dt.combine(today, closing_time) + _td(hours=1)).time()
    time_label = summary_time.strftime("%I:%M %p").lstrip("0")
    await _send_response(sender, t("closing.normal_time_set", lang=lang, time=time_label))


def handle_incoming_message(
    message_id: str,
    sender: str,
    text: str,
    user_profile: UserProfile | None = None,
) -> None:
    phone_number = _extract_phone_number(sender)
    with get_user_lock(phone_number):
        try:
            close_old_connections()
            run_async(_process_message_async(message_id, sender, text, user_profile))
        except django.db.utils.OperationalError:
            logger.exception(f"DB connection error for message {message_id}, retrying...")
            for conn in connections.all():
                conn.close()
            try:
                run_async(_process_message_async(message_id, sender, text, user_profile))
            except Exception as e:
                # The retry itself failed — this is a sibling except to the one below,
                # so without this nested try it would propagate uncaught and skip the
                # fallback reply entirely (the exact #284 failure mode this fixes).
                logger.exception(f"Retry failed for message {message_id}: {e}")
                _send_fallback_error(sender, user_profile)
        except Exception as e:
            logger.exception(f"Error handling message {message_id}: {e}")
            _send_fallback_error(sender, user_profile)


def handle_incoming_audio_message(
    message_id: str,
    sender: str,
    media_id: str,
    user_profile: UserProfile | None = None,
) -> None:
    phone_number = _extract_phone_number(sender)
    with get_user_lock(phone_number):
        try:
            close_old_connections()
            from apps.whatsapp.services.media_handler import process_audio_message_async
            run_async(process_audio_message_async(message_id, sender, media_id, user_profile))
        except django.db.utils.OperationalError:
            logger.exception(f"DB connection error for audio message {message_id}, retrying...")
            for conn in connections.all():
                conn.close()
            try:
                from apps.whatsapp.services.media_handler import process_audio_message_async
                run_async(process_audio_message_async(message_id, sender, media_id, user_profile))
            except Exception as e:
                # See handle_incoming_message — the retry is a sibling except, not
                # covered by the except Exception below, so it needs its own catch.
                logger.exception(f"Retry failed for audio message {message_id}: {e}")
                _send_fallback_error(sender, user_profile)
        except Exception as e:
            logger.exception(f"Error handling audio message {message_id}: {e}")
            _send_fallback_error(sender, user_profile)


async def _process_media_message_async(
    message_id: str,
    sender: str,
    r2_url: str,
    media_type: str,
    user_profile: UserProfile,
    caption: str = "",
) -> None:
    """Handle an inbound image/video/document from a known user.

    Images: attempt vision parse → sale confirmation flow or fallback prompt.
    Video/document: graceful unsupported message.
    """
    from apps.whatsapp.services.sale_handler import process_sale_message_unified
    from apps.whatsapp.services.message_parser import parse_image_for_sale

    lang = user_profile.language if user_profile else DEFAULT_LANGUAGE
    company = user_profile.company if user_profile else None

    if media_type != "image":
        await _send_response(sender, t("media.unsupported", lang=lang))
        return

    if not r2_url:
        await _send_response(sender, t("media.image_no_sale", lang=lang))
        return

    try:
        result = await parse_image_for_sale(r2_url, company=company, caption=caption)
    except Exception:
        logger.exception(f"Vision parse failed for message {message_id} from {sender}")
        await _send_response(sender, t("media.image_no_sale", lang=lang))
        return

    if result.intent == "sale" and result.items:
        await process_sale_message_unified(
            message_id=message_id,
            sender=sender,
            text="[Image]",
            company=company,
            result=result,
            lang=lang,
        )
    else:
        await _send_response(sender, t("media.image_no_sale", lang=lang))


def handle_incoming_media_message(
    message_id: str,
    sender: str,
    r2_url: str,
    media_type: str,
    user_profile: UserProfile,
    caption: str = "",
) -> None:
    phone_number = _extract_phone_number(sender)
    with get_user_lock(phone_number):
        try:
            close_old_connections()
            run_async(_process_media_message_async(message_id, sender, r2_url, media_type, user_profile, caption))
        except django.db.utils.OperationalError:
            logger.exception(f"DB connection error for media message {message_id}, retrying...")
            for conn in connections.all():
                conn.close()
            try:
                run_async(_process_media_message_async(message_id, sender, r2_url, media_type, user_profile, caption))
            except Exception as e:
                # See handle_incoming_message — the retry is a sibling except, not
                # covered by the except Exception below, so it needs its own catch.
                logger.exception(f"Retry failed for media message {message_id}: {e}")
                _send_fallback_error(sender, user_profile)
        except Exception as e:
            logger.exception(f"Error handling media message {message_id}: {e}")
            _send_fallback_error(sender, user_profile)


async def _handle_sales_query(
    sender: str,
    user_profile: UserProfile | None,
    result,
    lang: str = DEFAULT_LANGUAGE,
) -> None:
    from datetime import timedelta
    from asgiref.sync import sync_to_async as _s2a
    from apps.whatsapp.services.business_reports import (
        build_business_snapshot,
        build_comparison_context,
        build_period_summary,
        format_period_summary,
        upload_report_image,
        format_business_summary,
    )
    from apps.whatsapp.services.report_card import generate_stat_card

    if not user_profile or not user_profile.company:
        await _send_response(sender, t("sales_query.not_registered", lang=lang))
        return

    company = user_profile.company
    today = timezone.localdate()
    timeframe = result.timeframe or "today"

    # Multi-day aggregates — return text only, no image card
    if timeframe == "week":
        monday = today - timedelta(days=today.weekday())
        data = await _s2a(build_period_summary, thread_sensitive=True)(company, monday, today)
        label = f"This week ({monday.strftime('%d %b')} - {today.strftime('%d %b')})"
        await _send_response(sender, format_period_summary(label, data))
        return
    elif timeframe == "month":
        first = today.replace(day=1)
        data = await _s2a(build_period_summary, thread_sensitive=True)(company, first, today)
        await _send_response(sender, format_period_summary(today.strftime("%B"), data))
        return
    elif timeframe == "year":
        first = today.replace(month=1, day=1)
        data = await _s2a(build_period_summary, thread_sensitive=True)(company, first, today)
        await _send_response(sender, format_period_summary(str(today.year), data))
        return

    # Single-day queries — today or yesterday, with image card
    if timeframe == "yesterday":
        report_date = today - timedelta(days=1)
    else:
        report_date = today

    snapshot = await _s2a(build_business_snapshot, thread_sensitive=True)(company, report_date=report_date)

    if snapshot.sales_count == 0:
        date_label = report_date.strftime("%d %b %Y")
        no_sales_key = "sales_query.no_sales_today" if report_date == today else "sales_query.no_sales_past"
        await _send_response(sender, t(no_sales_key, lang=lang, date=date_label))
        return

    comparison = await _s2a(build_comparison_context, thread_sensitive=True)(company, report_date)
    text_summary = format_business_summary(snapshot, lang=lang)

    image_url = None
    try:
        image_bytes = generate_stat_card(snapshot, comparison, shop_name=company.name, lang=lang)
        image_url = await _s2a(upload_report_image, thread_sensitive=True)(
            image_bytes, company.id, report_date
        )
    except Exception as e:
        logger.warning("Failed to generate sales query report card for company %s", company.id, exc_info=e)
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("failure_type", "report_card_generation")
            scope.set_tag("company_id", company.id)
            scope.set_tag("source", "sales_query_on_demand")
            scope.set_context("report_card", {
                "company_id": company.id,
                "report_date": str(report_date),
                "source": "sales_query_on_demand",
            })
            sentry_sdk.capture_exception(e)

    wa_client = get_whatsapp_client()
    if image_url:
        await wa_client.send_image(sender, image_url, caption=text_summary)
    else:
        await _send_response(sender, text_summary)


async def _process_message_async(
    message_id: str,
    sender: str,
    text: str,
    user_profile: UserProfile | None = None,
) -> None:
    start_tracking(request_id=message_id)
    try:
        company = user_profile.company if user_profile else None
        lang = user_profile.language if user_profile else DEFAULT_LANGUAGE

        # Send typing indicator while LLM processes the message (if supported).
        # Skip for short acknowledgements — they produce no reply and the typing flicker is confusing.
        #
        # This same pattern also gates whether an "other/greeting"-classified message stays
        # silent (see below) — a code-level backstop in case the LLM ever mis-classifies
        # something as "greeting" despite the prompt's guidance. Must cover every example
        # services/openrouter/prompts.py's "greeting" sub-intent trains the LLM on ("hi",
        # "okay", "thanks", "good morning", "👍", "🙏", "❤️", "✅"), or a real greeting the
        # LLM correctly classifies could still fall through to other.deferral and get an
        # unwanted reply. Keep this in sync with the prompt's example list.
        _SILENT_PATTERN = re.compile(
            r"^\s*("
            r"(good\s+)?(morning|afternoon|evening|night)|"
            r"ok(ay)?|thanks?(\s+you)?(\s+(so|very)\s+much)?|thx|noted|cool|sure|yes|no|yep|nope|"
            r"hi|hello|hey|"
            r"makadii|ndatenda|zvakanaka|ehe|kwete|mangwanani|masikati|manheru"
            r")\s*[.!]?\s*$",
            re.IGNORECASE,
        )
        # Bare emoji-only messages (👍, 🙏, ❤️, ✅, etc.) are also trained "greeting" examples
        # and must not fall through to a reply — checked separately from the word pattern
        # since emoji don't fit a word-boundary regex cleanly.
        _EMOJI_ONLY_PATTERN = re.compile(
            r"^\s*[\U0001F300-\U0001FAFF☀-➿️\s]+\s*$"
        )

        def _is_silent_greeting(candidate: str) -> bool:
            return bool(_SILENT_PATTERN.match(candidate) or _EMOJI_ONLY_PATTERN.match(candidate))

        # Guards the unnamed-shop name-capture gate below (#316 review) — a
        # digit alongside a currency indicator is almost certainly a sale
        # attempt ("2 Mazoe $1 each"), not a shop name, and must not be
        # captured as one. Deliberately a narrow heuristic, not a parser —
        # matches the same "exclude, don't validate" shape the greeting gate
        # above uses, not a full re-implementation of sale parsing.
        _SALE_SHAPED_PATTERN = re.compile(
            r"\$|\bUSD\b|\bZAR\b|\bZWG\b|\bZiG\b|\brand\b|\bdollars?\b|R\d",
            re.IGNORECASE,
        )

        def _looks_like_sale_attempt(candidate: str) -> bool:
            return bool(re.search(r"\d", candidate)) and bool(_SALE_SHAPED_PATTERN.search(candidate))

        _typing_sent = False
        if settings.ENABLE_WHATSAPP_TYPING and not _is_silent_greeting(text):
            try:
                client = get_whatsapp_client()
                try:
                    await client.send_typing_indicator(sender, "typing_on", message_id=message_id)
                    _typing_sent = True
                except Exception:
                    logger.debug("Typing indicator not available or failed to send")
            except Exception:
                logger.debug("WhatsApp client not available for typing indicator")

        # Intercept nudge opt-out before anything else.
        if text.strip().lower() in ("stop", "unsubscribe") and user_profile:
            @db_sync_to_async
            def _set_opt_out():
                from apps.core.models import UserProfile as UP
                UP.objects.filter(pk=user_profile.pk).update(nudge_opt_out=True)
            await _set_opt_out()
            await _send_response(sender, t("nudge.opt_out_confirmed", lang=lang))
            return

        # Intercept help/options/menu before hitting the LLM.
        if text.strip().lower() in ("help", "options", "menu", "rubatsiro"):
            await _send_response(sender, t("help.menu", lang=lang, support_phone=settings.SUPPORT_PHONE_NUMBER))
            return

        # Intercept closing time messages before hitting the LLM.
        if company:
            today = timezone.localdate()
            from apps.whatsapp.services.business_reports import (
                parse_closing_time_llm,
                set_company_daily_closing_time,
                set_company_normal_closing_time,
            )

            # Detect "closes at X every day" — set permanent closing time.
            _permanent_pattern = re.compile(
                r"\b(every\s+day|always|daily|each\s+day|mazuva\s+ose|nguva\s+dzose)\b",
                re.IGNORECASE,
            )
            if _permanent_pattern.search(text):
                parsed_time = await parse_closing_time_llm(text)
                if parsed_time:
                    from datetime import datetime as _dt, timedelta as _td
                    await set_company_normal_closing_time(company.id, parsed_time)
                    summary_time = (_dt.combine(today, parsed_time) + _td(hours=1)).time()
                    time_label = summary_time.strftime("%I:%M %p").lstrip("0")
                    await _send_response(sender, t("closing.normal_time_set", lang=lang, time=time_label))
                    return

            # Intercept daily closing time reply — only when a prompt was sent today.
            closing_set_today = company.daily_closing_date == today and company.daily_closing_time
            if company.last_closing_prompt_date == today and not closing_set_today:
                parsed_time = await parse_closing_time_llm(text)
                if parsed_time:
                    await set_company_daily_closing_time(company.id, parsed_time, today)
                    await _send_response(sender, t("closing.acknowledged", lang=lang))
                    return
                # Otherwise fall through — they sent a sale or unrelated message during the closing window

            # Intercept the reply to the unnamed-shop followup (#316) — only
            # while still on the "Unnamed Shop" placeholder and we've already
            # asked. A filler reply ("ok", "thanks") must not get captured as
            # the shop's name — same _is_silent_greeting check already used
            # above to gate the typing indicator / greeting silence. A
            # sale-shaped reply ("2 Mazoe $1 each") must not get captured
            # either — the very first message after the followup could
            # legitimately be a sale, not an answer to the name question
            # (review on PR #341: without this, that sale would silently
            # become the shop's new name instead of being recorded).
            if company.name == "Unnamed Shop" and company.unnamed_shop_followup_sent:
                stripped = text.strip()
                if stripped and not _is_silent_greeting(stripped) and not _looks_like_sale_attempt(stripped):
                    await _set_company_name(company.id, stripped)
                    await _send_response(sender, t("company.name_updated", lang=lang, shop_name=stripped))
                    return
                # Otherwise fall through — greeting/filler, sale-shaped, or empty; not a name attempt

        try:
            result = await parse_message_unified(text, company=company, message_id=message_id)
        except Exception as e:
            logger.exception(f"LLM processing failed for message {message_id}: {e}")
            await _send_response(sender, t("error.processing_failed", lang=lang))
            return
        finally:
            # Turn off typing indicator
            if _typing_sent:
                try:
                    client = get_whatsapp_client()
                    try:
                        await client.send_typing_indicator(sender, "typing_off")
                    except Exception:
                        logger.debug("Failed to send typing_off")
                except Exception:
                    pass

        logger.info(f"Parsed message - intent: {result.intent}, confidence: {result.confidence}")

        import sentry_sdk
        sentry_sdk.set_tag("intent", result.intent)
        sentry_sdk.set_context("llm_parse", {
            "intent": result.intent,
            "confidence": result.confidence,
            "message_id": message_id,
            "notes": result.notes,
        })

        if result.intent == "language_switch":
            if result.target_language and user_profile:
                await _set_profile_language(user_profile.pk, result.target_language)
                await _send_response(sender, t("language.switched", lang=result.target_language))
            else:
                await _send_response(sender, t("language.switch_unclear", lang=lang))
            return

        if result.intent == "add_assistant":
            from apps.whatsapp.services.waitlist_handler import handle_add_assistant
            await handle_add_assistant(sender, text, user_profile, result)
            return

        if result.intent == "remove_assistant":
            from apps.whatsapp.services.waitlist_handler import handle_remove_assistant
            await handle_remove_assistant(sender, text, user_profile, result)
            return

        if result.intent == "sales_query":
            await _handle_sales_query(sender, user_profile, result, lang=lang)
            return

        if result.intent == "other":
            sub = result.other_sub_intent
            # The prompt guides the LLM to classify reactive emoji/fillers as "greeting" —
            # but as a code-level backstop against misclassification, only actually stay
            # silent when the raw text itself matches the recognised filler/emoji pattern,
            # never on the LLM label alone (see #284).
            if sub == "greeting" and _is_silent_greeting(text):
                pass  # No response — replying to "hi" / "thanks" adds noise
            elif sub == "question":
                await _send_response(sender, t("help.menu", lang=lang, support_phone=settings.SUPPORT_PHONE_NUMBER))
            else:  # deferral, unknown, or a "greeting" that wasn't a recognised filler word
                await _send_response(sender, t("other.deferral", lang=lang))
            return

        from apps.whatsapp.services.sale_handler import process_sale_message_unified
        await process_sale_message_unified(message_id, sender, text, company, result, lang=lang)
    finally:
        end_tracking()
