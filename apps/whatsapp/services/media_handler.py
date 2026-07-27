"""Audio download, transcription, and R2 upload."""

from __future__ import annotations

import asyncio
import logging

from apps.whatsapp.services.webhook_handler import (
    db_sync_to_async,
    DEFAULT_LANGUAGE,
    _extract_phone_number,
    _send_response,
    _upload_to_r2,
    t,
)
from apps.whatsapp.services.whatsapp_client import get_whatsapp_client
from utils.timing import start_tracking, end_tracking

logger = logging.getLogger(__name__)


async def process_audio_message_async(
    message_id: str,
    sender: str,
    media_id: str,
    user_profile=None,
) -> None:
    """Download, transcribe, and process an incoming audio message."""
    start_tracking(request_id=message_id)
    try:
        from apps.whatsapp.services.sale_handler import process_sale_message_unified
        from apps.whatsapp.services.message_parser import parse_message_unified

        company = user_profile.company if user_profile else None
        lang = user_profile.language if user_profile else DEFAULT_LANGUAGE
        phone_number = _extract_phone_number(sender)

        whatsapp_client = get_whatsapp_client()
        media_result = await whatsapp_client.download_media(media_id)

        if not media_result:
            await _send_response(sender, t("audio.download_failed", lang=lang))
            return

        audio_data, mime_type = media_result

        from django.conf import settings
        from services.elevenlabs import ElevenLabsClient, ElevenLabsError

        mime_to_extension = {
            "audio/ogg": "ogg",
            "audio/mpeg": "mp3",
            "audio/mp4": "m4a",
            "audio/aac": "aac",
            "audio/amr": "amr",
        }
        extension = mime_to_extension.get(mime_type, "ogg")
        filename = f"audio.{extension}"

        if not getattr(settings, "ELEVENLABS_API_KEY", None):
            logger.error("ELEVENLABS_API_KEY not configured — cannot transcribe audio for message %s", message_id)
            await _send_response(sender, t("audio.transcription_failed", lang=lang))
            return

        try:
            elevenlabs_client = ElevenLabsClient()

            async def _background_r2_upload():
                r2_url = await _upload_to_r2(audio_data, media_id, mime_type, phone_number)
                if r2_url:
                    @db_sync_to_async
                    def _update_r2_url(msg_id: str, url: str):
                        from apps.whatsapp.models import WhatsAppMessage
                        WhatsAppMessage.objects.filter(whatsapp_message_id=msg_id).update(r2_media_url=url)
                    await _update_r2_url(message_id, r2_url)
                    logger.info(f"R2 upload completed for {message_id}: {r2_url}")

            asyncio.create_task(_background_r2_upload())

            from apps.whatsapp.services.message_parser import _get_active_products
            products = await _get_active_products(company)
            product_names = [p.name for p in products]

            transcribed_text = await elevenlabs_client.transcribe_audio(
                audio_data, filename, product_names=product_names
            )
            logger.info(f"Transcribed audio message {message_id}: {transcribed_text[:100]}...")

            @db_sync_to_async
            def _update_message_transcription(msg_id: str, transcript: str):
                from apps.whatsapp.models import WhatsAppMessage
                WhatsAppMessage.objects.filter(whatsapp_message_id=msg_id).update(
                    transcribed_text=transcript,
                    content=transcript,
                )
            await _update_message_transcription(message_id, transcribed_text)

        except ElevenLabsError as e:
            logger.exception(f"Failed to transcribe audio message {message_id}: {e}")
            await _send_response(sender, t("audio.transcription_failed", lang=lang))
            return

        try:
            result = await parse_message_unified(transcribed_text, company=company, message_id=message_id)
        except Exception as e:
            logger.exception(f"LLM processing failed for audio message {message_id}: {e}")
            await _send_response(sender, t("error.processing_failed", lang=lang))
            return

        logger.info(f"Parsed audio - intent: {result.intent}, confidence: {result.confidence}")

        import sentry_sdk
        sentry_sdk.set_tag("intent", result.intent)
        sentry_sdk.set_context("llm_parse", {
            "intent": result.intent,
            "confidence": result.confidence,
            "message_id": message_id,
            "notes": result.notes,
        })

        from apps.core.models import UserProfile as UP
        if result.intent == "add_assistant":
            from apps.whatsapp.services.waitlist_handler import handle_add_assistant
            await handle_add_assistant(sender, transcribed_text, user_profile, result)
            return

        await process_sale_message_unified(
            message_id, sender, transcribed_text, company, result, is_from_audio=True, lang=lang
        )
    finally:
        end_tracking()
