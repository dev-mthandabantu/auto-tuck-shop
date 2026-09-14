import hashlib
import hmac
import json
import logging
from enum import Enum

from django.conf import settings
from django.db import IntegrityError, close_old_connections
from django.http import HttpRequest, HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.core.models import UserProfile, WaitlistEntry
from apps.whatsapp.models import WhatsAppMessage
from apps.whatsapp.services.webhook_handler import (
    handle_closing_time_button_action,
    handle_incoming_message,
    handle_language_button_action,
    handle_new_waitlist_entry,
    handle_nudge_reports_no,
    handle_nudge_reports_yes,
    handle_unhandled_button_tap,
    handle_sale_button_action,
    handle_waitlist_button_action,
    handle_waitlisted_message,
    run_async,
)
from apps.whatsapp.services.sale_handler import handle_currency_clarification

logger = logging.getLogger(__name__)


class SenderStatus(Enum):
    """Status of a message sender."""

    KNOWN_USER = "known_user"
    WAITLISTED = "waitlisted"
    UNKNOWN = "unknown"


def _extract_phone_number(sender: str) -> str:
    """
    Extract and normalize phone number.

    Meta sends plain numbers like '1234567890'.
    Stored numbers may have '+' prefix like '+1234567890'.
    """
    # Remove any whatsapp: prefix (for backwards compatibility)
    if sender.startswith("whatsapp:"):
        sender = sender[9:]
    # Ensure + prefix for database lookup
    if not sender.startswith("+"):
        sender = f"+{sender}"
    return sender


def _lookup_sender(sender: str) -> tuple[SenderStatus, UserProfile | None, WaitlistEntry | None]:
    """
    Lookup sender status and associated profile/waitlist entry.

    Returns:
        Tuple of (status, user_profile, waitlist_entry)
        - KNOWN_USER: has profile, None waitlist entry
        - WAITLISTED: None profile, has waitlist entry
        - UNKNOWN: None profile, None waitlist entry

    A deactivated profile (e.g. a removed assistant) is treated as UNKNOWN —
    they can freely re-onboard as a new assistant or shop owner. Only their
    past sales history is preserved; the profile itself no longer counts as
    "known" for lookup/registration purposes.
    """
    phone_number = _extract_phone_number(sender)

    # Check for existing UserProfile first
    try:
        profile = UserProfile.objects.select_related("company").get(
            phone_number=phone_number, user__is_active=True
        )
        return (SenderStatus.KNOWN_USER, profile, None)
    except UserProfile.DoesNotExist:
        pass

    # Check for existing WaitlistEntry
    try:
        entry = WaitlistEntry.objects.get(phone_number=phone_number)
        return (SenderStatus.WAITLISTED, None, entry)
    except WaitlistEntry.DoesNotExist:
        pass

    # Unknown sender
    return (SenderStatus.UNKNOWN, None, None)


def _is_duplicate_message(whatsapp_message_id: str) -> bool:
    if not whatsapp_message_id:
        return False
    return WhatsAppMessage.objects.filter(whatsapp_message_id=whatsapp_message_id).exists()


def _record_inbound_message(
    phone_number: str,
    message_type: str,
    content: str = "",
    button_id: str = "",
    whatsapp_message_id: str = "",
    reply_to_message_id: str = "",
    raw_payload: dict | None = None,
    user_profile: UserProfile | None = None,
    waitlist_entry: WaitlistEntry | None = None,
    media_id: str = "",
    media_url: str = "",
    r2_media_url: str = "",
    transcribed_text: str = "",
) -> None:
    """
    Record an inbound WhatsApp message to the database.

    This is a fire-and-forget operation - failures won't break message processing.
    
    Duplicate detection: If whatsapp_message_id is already in the DB, this will
    raise IntegrityError (due to unique constraint). We catch it and log a warning
    rather than breaking the webhook. This handles the case where Meta retries
    the same webhook message.
    """
    try:
        WhatsAppMessage.objects.create(
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type=message_type,
            phone_number=phone_number,
            content=content,
            button_id=button_id,
            whatsapp_message_id=whatsapp_message_id,
            reply_to_message_id=reply_to_message_id,
            raw_payload=raw_payload,
            user_profile=user_profile,
            company=user_profile.company if user_profile else None,
            waitlist_entry=waitlist_entry,
            media_id=media_id,
            media_url=media_url,
            r2_media_url=r2_media_url,
            transcribed_text=transcribed_text,
        )
        logger.debug(f"Recorded inbound message from {phone_number}")
        if user_profile and user_profile.company and not user_profile.company.first_message_date:
            from django.utils import timezone
            from apps.core.models import Company
            Company.objects.filter(pk=user_profile.company_id).update(first_message_date=timezone.localdate())
    except IntegrityError as e:
        if "whatsapp_message_id" in str(e):
            logger.warning(f"Duplicate message detected: {whatsapp_message_id}. Ignoring.")
        else:
            logger.error(f"Failed to record inbound message: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Failed to record inbound message: {e}", exc_info=True)


@method_decorator(csrf_exempt, name="dispatch")
class WhatsAppWebhookView(View):
    """Webhook endpoint for Meta WhatsApp Business Cloud API."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Handle webhook verification from Meta."""
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")

        verify_token = settings.META_WHATSAPP_VERIFY_TOKEN

        if mode == "subscribe" and token == verify_token:
            logger.info("Webhook verification successful")
            return HttpResponse(challenge, content_type="text/plain")

        logger.warning(f"Webhook verification failed: mode={mode}, token_match={token == verify_token}")
        return HttpResponse("Verification failed", status=403)

    def post(self, request: HttpRequest) -> HttpResponse:
        """Handle incoming webhook from Meta."""
        # Verify signature
        if not self._verify_signature(request):
            logger.warning("Invalid Meta webhook signature")
            return HttpResponse("Invalid signature", status=401)

        # Parse JSON body
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in webhook body: {request.body[:500]}")
            logger.warning(f"Content-Type: {request.content_type}")
            return HttpResponse("Invalid JSON", status=400)

        print(f"[DEBUG VIEW POST] Received webhook data: {data}", flush=True)

        # Process each entry
        for entry in data.get("entry", []):
            print(f"[DEBUG VIEW POST] Processing entry", flush=True)
            for change in entry.get("changes", []):
                print(f"[DEBUG VIEW POST] Processing change, field={change.get('field')}", flush=True)
                if change.get("field") != "messages":
                    continue

                value = change.get("value", {})
                messages = value.get("messages", [])
                print(f"[DEBUG VIEW POST] Found {len(messages)} messages", flush=True)

                for message in messages:
                    self._handle_message(message)

                for status in value.get("statuses", []):
                    self._handle_status_update(status)

        # Always return 200 OK for Meta webhooks
        return HttpResponse("OK")

    def _handle_message(self, message: dict) -> None:
        """Process a single message from the webhook payload."""
        print(f"[DEBUG VIEW] _handle_message called with message type: {message.get('type', '')}", flush=True)
        message_id = message.get("id", "")
        sender = message.get("from", "")  # Plain number like '1234567890'
        message_type = message.get("type", "")

        if not sender:
            # No phone number in payload — likely a WhatsApp username/BSUID
            # contact messaging us for the first time (Meta is rolling this
            # out gradually). We have no way to look up or reply to a sender
            # we can't identify by phone number yet, so drop it and get
            # visibility via Sentry rather than creating a bad "+" record.
            logger.warning(
                "Inbound message %s (type=%s) has no phone number — "
                "possible BSUID/username contact, dropping",
                message_id, message_type,
            )
            return

        import sentry_sdk
        sentry_sdk.set_user({"id": sender, "username": sender})

        if _is_duplicate_message(message_id):
            logger.info("Duplicate message %s from %s, skipping", message_id, sender)
            return

        # Handle interactive button responses (buttons we send ourselves via
        # send_message_with_buttons — sale confirm/fix, waitlist, language,
        # closing time, currency, onboarding_reports)
        if message_type == "interactive":
            interactive = message.get("interactive", {})
            if interactive.get("type") == "button_reply":
                button_id = interactive.get("button_reply", {}).get("id", "")
                self._handle_button_response(sender, button_id, message)
                return

        # Handle a Meta-approved template's quick-reply button tap (e.g.
        # ats_reengagement) — a different payload shape from the interactive
        # buttons above: {"type": "button", "button": {"payload": ..., "text": ...}}.
        # Routes through the same handler once button_id is extracted, since
        # everything downstream (known-ID dispatch, unhandled-tap catch-all)
        # is identical regardless of which shape it arrived in (#209).
        if message_type == "button":
            button = message.get("button", {})
            button_id = button.get("payload", "")
            if button_id:
                self._handle_button_response(sender, button_id, message)
                return

        # Handle text messages
        if message_type == "text":
            body = message.get("text", {}).get("body", "")
            if body:
                self._handle_text_message(message_id, sender, body)
            return

        # Handle audio/voice messages
        if message_type == "audio" or message_type == "voice":
            audio = message.get("audio", {}) or message.get("voice", {})
            media_id = audio.get("id", "")
            if media_id:
                self._handle_audio_message(message_id, sender, media_id, message)
            return

        # Handle image messages
        if message_type == "image":
            image = message.get("image", {})
            media_id = image.get("id", "")
            caption = image.get("caption", "")
            if media_id:
                self._handle_media_message(message_id, sender, media_id, "image", caption, message)
            return

        # Handle video messages
        if message_type == "video":
            video = message.get("video", {})
            media_id = video.get("id", "")
            caption = video.get("caption", "")
            if media_id:
                self._handle_media_message(message_id, sender, media_id, "video", caption, message)
            return

        # Handle document messages
        if message_type == "document":
            document = message.get("document", {})
            media_id = document.get("id", "")
            filename = document.get("filename", "")
            if media_id:
                self._handle_media_message(message_id, sender, media_id, "document", filename, message)
            return

        logger.info(f"Ignoring message type: {message_type}")

    def _handle_status_update(self, status: dict) -> None:
        """Update outbound message delivery status from Meta webhook callback."""
        from apps.whatsapp.models import WhatsAppMessage
        wamid = status.get("id")
        delivery_status = status.get("status")
        if not wamid or delivery_status not in ("failed", "delivered", "read"):
            return
        errors = status.get("errors", [])
        error_detail = errors[0].get("error_data", {}).get("details", "") if errors else ""
        error_code = str(errors[0].get("code", "")) if errors else ""
        api_error = f"[{error_code}] {error_detail}".strip("[] ") if error_code else error_detail
        update = {"api_success": delivery_status != "failed"}
        if api_error:
            update["api_error"] = api_error
        updated = WhatsAppMessage.objects.filter(whatsapp_message_id=wamid).update(**update)
        if updated:
            logger.info(f"Delivery status {delivery_status} for wamid {wamid}" + (f" — {api_error}" if api_error else ""))
        else:
            logger.debug(f"No message found for wamid {wamid} (status: {delivery_status})")

    def _handle_button_response(self, sender: str, button_id: str, message: dict) -> None:
        """Handle a button click response."""
        logger.info(f"Received button click from {sender}: {button_id}")

        # Get the context (original message being replied to)
        context = message.get("context", {})
        original_message_id = context.get("id", "")
        message_id = message.get("id", "")

        if _is_duplicate_message(message_id):
            logger.info("Duplicate button response %s from %s, skipping", message_id, sender)
            return

        # Lookup sender for recording
        phone_number = _extract_phone_number(sender)
        status, profile, waitlist_entry = _lookup_sender(sender)
        if profile:
            import sentry_sdk
            sentry_sdk.set_user({"id": sender, "username": sender, "name": profile.company.name})

        # Record inbound button response
        _record_inbound_message(
            phone_number=phone_number,
            message_type=WhatsAppMessage.MessageType.BUTTON_RESPONSE,
            button_id=button_id,
            whatsapp_message_id=message_id,
            reply_to_message_id=original_message_id,
            raw_payload=message,
            user_profile=profile,
            waitlist_entry=waitlist_entry,
        )

        # Mark inbound button response as read so WhatsApp shows read receipts
        if settings.ENABLE_WHATSAPP_MARK_AS_READ:
            try:
                from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
                client = get_whatsapp_client()
                # fire-and-forget
                try:
                    run_async(client.mark_as_read(message_id))
                except Exception:
                    logger.exception("Failed to fire mark_as_read")
            except Exception:
                logger.debug("Mark-as-read not available")

        # Parse button ID to determine action type
        # Format: "confirm_{sale_id}", "fix_{sale_id}", "waitlist_approve_{entry_id}", "waitlist_reject_{entry_id}"
        if button_id.startswith("confirm_") or button_id.startswith("fix_"):
            action = "confirm" if button_id.startswith("confirm_") else "fix"
            handle_sale_button_action(
                action=action,
                sender=sender,
                original_message_sid=original_message_id or None,
            )
        elif button_id.startswith("waitlist_approve_") or button_id.startswith("waitlist_reject_"):
            action = "approve" if button_id.startswith("waitlist_approve_") else "reject"
            handle_waitlist_button_action(
                action=action,
                sender=sender,
                original_message_sid=original_message_id or None,
            )
        elif button_id.startswith("lang_en_") or button_id.startswith("lang_sn_"):
            # Language selection: "lang_en_{entry_id}" or "lang_sn_{entry_id}"
            parts = button_id.split("_", 2)  # ["lang", "en", "123"]
            lang = parts[1]
            entry_id = int(parts[2])
            handle_language_button_action(
                lang=lang,
                entry_id=entry_id,
                sender=sender,
            )
        elif button_id == "nudge_reports_yes":
            handle_nudge_reports_yes(sender=sender)
        elif button_id == "nudge_reports_no":
            handle_nudge_reports_no(sender=sender)
        elif button_id in ("closing_early", "closing_mid", "closing_late"):
            handle_closing_time_button_action(sender=sender, button_id=button_id)
        elif button_id in ("currency_usd", "currency_zwg", "currency_zar"):
            handle_currency_clarification(sender=sender, button_id=button_id)
        else:
            # No code-side handler for this button_id — most commonly a
            # quick-reply button on a Meta-approved template (e.g.
            # ats_reengagement) that isn't one of the interactive buttons we
            # send ourselves above. Still record it as an engagement signal
            # so the tap-followup check can catch the shop while warm (#209).
            logger.warning(f"Unknown button ID format: {button_id}")
            handle_unhandled_button_tap(sender=sender)

    def _handle_text_message(self, message_id: str, sender: str, body: str) -> None:
        """Handle a text message."""
        print(f"[DEBUG VIEW] _handle_text_message called: sender={sender}, body={body[:50]}...", flush=True)

        # Lookup sender status
        status, profile, waitlist_entry = _lookup_sender(sender)
        phone_number = _extract_phone_number(sender)
        print(f"[DEBUG VIEW] Sender status: {status.value}, phone={phone_number}", flush=True)
        if profile:
            import sentry_sdk
            sentry_sdk.set_user({"id": sender, "username": sender, "name": profile.company.name})

        # Record inbound message
        _record_inbound_message(
            phone_number=phone_number,
            message_type=WhatsAppMessage.MessageType.TEXT,
            content=body,
            whatsapp_message_id=message_id,
            user_profile=profile,
            waitlist_entry=waitlist_entry,
        )

        # Mark inbound message as read (so blue ticks appear)
        if settings.ENABLE_WHATSAPP_MARK_AS_READ:
            try:
                from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
                client = get_whatsapp_client()
                try:
                    run_async(client.mark_as_read(message_id))
                except Exception:
                    logger.exception("Failed to fire mark_as_read")
            except Exception:
                logger.debug("Mark-as-read not available")

        if status == SenderStatus.UNKNOWN:
            # New user - add to waitlist
            print(f"[DEBUG VIEW] Calling handle_new_waitlist_entry for unknown sender {sender}", flush=True)
            handle_new_waitlist_entry(
                sender=sender,
                text=body,
            )
            print(f"[DEBUG VIEW] handle_new_waitlist_entry returned", flush=True)
        elif status == SenderStatus.WAITLISTED:
            # Already on waitlist - send pending message (or capture company name)
            handle_waitlisted_message(
                sender=sender,
                text=body,
                waitlist_entry=waitlist_entry,
            )
        else:
            # Known user - handle the message normally
            handle_incoming_message(
                message_id=message_id,
                sender=sender,
                text=body,
                user_profile=profile,
            )

    def _handle_audio_message(self, message_id: str, sender: str, media_id: str, message: dict) -> None:
        """Handle an audio/voice message."""
        logger.info(f"Received WhatsApp audio message from {sender}, media_id={media_id}")

        # Lookup sender status
        status, profile, waitlist_entry = _lookup_sender(sender)
        phone_number = _extract_phone_number(sender)

        # Record inbound message (R2 URL is populated later during async processing)
        _record_inbound_message(
            phone_number=phone_number,
            message_type=WhatsAppMessage.MessageType.AUDIO,
            content="[Audio message]",
            whatsapp_message_id=message_id,
            user_profile=profile,
            waitlist_entry=waitlist_entry,
            media_id=media_id,
            r2_media_url="",
            raw_payload=message,
        )

        # Mark inbound audio message as read
        if settings.ENABLE_WHATSAPP_MARK_AS_READ:
            try:
                from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
                client = get_whatsapp_client()
                try:
                    run_async(client.mark_as_read(message_id))
                except Exception:
                    logger.exception("Failed to fire mark_as_read")
            except Exception:
                logger.debug("Mark-as-read not available")

        if status == SenderStatus.UNKNOWN:
            # New user - add to waitlist
            handle_new_waitlist_entry(
                sender=sender,
                text="[Audio message]",
            )
        elif status == SenderStatus.WAITLISTED:
            # Already on waitlist - send pending message
            handle_waitlisted_message(
                sender=sender,
                text="[Audio message]",
                waitlist_entry=waitlist_entry,
            )
        else:
            # Known user - process audio message
            from apps.whatsapp.services.webhook_handler import handle_incoming_audio_message
            handle_incoming_audio_message(
                message_id=message_id,
                sender=sender,
                media_id=media_id,
                user_profile=profile,
            )

    def _handle_media_message(
        self, message_id: str, sender: str, media_id: str, media_type: str, caption: str, message: dict
    ) -> None:
        """Handle an image/video/document message."""
        logger.info(f"Received WhatsApp {media_type} message from {sender}, media_id={media_id}")

        # Lookup sender status
        status, profile, waitlist_entry = _lookup_sender(sender)
        phone_number = _extract_phone_number(sender)

        # Download media and upload to R2
        r2_url = self._download_and_upload_media(media_id, phone_number)

        # Record inbound message
        content = f"[{media_type.capitalize()} message]"
        if caption:
            content = f"{content}: {caption}"

        _record_inbound_message(
            phone_number=phone_number,
            message_type=WhatsAppMessage.MessageType.UNKNOWN,  # Add specific types later if needed
            content=content,
            whatsapp_message_id=message_id,
            user_profile=profile,
            waitlist_entry=waitlist_entry,
            media_id=media_id,
            r2_media_url=r2_url or "",
            raw_payload=message,
        )

        logger.info(f"Saved {media_type} message from {sender}, R2 URL: {r2_url}")

        if status == SenderStatus.KNOWN_USER and profile:
            from apps.whatsapp.services.webhook_handler import handle_incoming_media_message
            handle_incoming_media_message(
                message_id=message_id,
                sender=sender,
                r2_url=r2_url or "",
                media_type=media_type,
                user_profile=profile,
                caption=caption,
            )

    def _download_and_upload_media(self, media_id: str, phone_number: str) -> str | None:
        """
        Download media from Meta and upload to R2 storage.

        This is a synchronous operation that should complete quickly.
        For production, consider offloading to a background task queue.

        Args:
            media_id: The Meta media ID
            phone_number: The phone number of the sender

        Returns:
            The R2 public URL, or None if the operation failed
        """
        import asyncio
        from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
        from services.storage import R2StorageClient

        async def _download_and_upload():
            # Download from Meta
            whatsapp_client = get_whatsapp_client()
            media_result = await whatsapp_client.download_media(media_id)

            if not media_result:
                logger.error(f"Failed to download media {media_id}")
                return None

            media_data, mime_type = media_result

            # Upload to R2
            r2_client = R2StorageClient()
            r2_url = r2_client.upload_media(media_data, media_id, mime_type, phone_number)

            return r2_url

        try:
            close_old_connections()
            return asyncio.run(_download_and_upload())
        except Exception as e:
            logger.error(f"Error downloading and uploading media {media_id}: {e}", exc_info=True)
            return None

    def _verify_signature(self, request: HttpRequest) -> bool:
        """Verify the X-Hub-Signature-256 header."""
        signature_header = request.headers.get("X-Hub-Signature-256", "")
        app_secret = settings.META_WHATSAPP_APP_SECRET

        if not app_secret:
            # Skip verification if no app secret configured (dev mode)
            logger.warning("META_WHATSAPP_APP_SECRET not set, skipping signature verification")
            return True

        if not signature_header:
            logger.error("X-Hub-Signature-256 header missing, rejecting request")
            return False

        if not signature_header.startswith("sha256="):
            logger.warning(f"Invalid signature format: {signature_header[:20]}...")
            return False

        expected_signature = signature_header[7:]  # Remove 'sha256=' prefix

        # Calculate HMAC-SHA256 of the raw body
        calculated_signature = hmac.new(
            app_secret.encode(),
            request.body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_signature, calculated_signature):
            logger.warning(f"Signature mismatch: expected={expected_signature[:20]}..., calculated={calculated_signature[:20]}...")
            return False

        return True
