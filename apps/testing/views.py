"""
Staging-only test endpoints for reading the mock WhatsApp outbox
and pre-loading mock media for audio tests.
"""

import base64
import json
import logging

import boto3
from botocore.client import Config
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from services.whatsapp.mock_client import MockWhatsAppClient

logger = logging.getLogger(__name__)


def _check_api_key(request: HttpRequest) -> HttpResponse | None:
    """Return an error response if the API key is missing/wrong, else None."""
    expected = getattr(settings, "TEST_API_KEY", None)
    if not expected:
        return HttpResponse("Test API not configured", status=503)
    provided = request.headers.get("X-Test-Api-Key", "")
    if provided != expected:
        return HttpResponse("Forbidden", status=403)
    return None


@method_decorator(csrf_exempt, name="dispatch")
class OutboxView(View):
    """GET /test/outbox/?phone=+27821234567 — read mock outbound messages."""

    def get(self, request: HttpRequest) -> JsonResponse:
        err = _check_api_key(request)
        if err:
            return err

        phone = request.GET.get("phone", "")

        if phone:
            # Normalize: match with or without '+' prefix
            variants = {phone, phone.lstrip("+"), "+" + phone.lstrip("+")}
            messages = [m for m in MockWhatsAppClient.sent_messages if m["to"] in variants]
            buttons = [b for b in MockWhatsAppClient.sent_buttons if b["to"] in variants]
            # Filter timeline for this phone when possible
            timeline = [t for t in MockWhatsAppClient.timeline if (t.get("phone") in variants) or (not t.get("phone"))]
        else:
            messages = list(MockWhatsAppClient.sent_messages)
            buttons = list(MockWhatsAppClient.sent_buttons)
            timeline = list(MockWhatsAppClient.timeline)

        return JsonResponse({"messages": messages, "buttons": buttons, "timeline": timeline})

    def delete(self, request: HttpRequest) -> HttpResponse:
        """DELETE /test/outbox/ — reset mock state.

        Pass ?phone=+27821234567 to reset only that phone's messages.
        Omit phone to reset all state.
        """
        err = _check_api_key(request)
        if err:
            return err
        phone = request.GET.get("phone", "")
        if phone:
            MockWhatsAppClient.reset_phone(phone)
        else:
            MockWhatsAppClient.reset()
        return HttpResponse("OK", status=200)


@method_decorator(csrf_exempt, name="dispatch")
class MockMediaView(View):
    """POST /test/mock-media/ — pre-load audio for mock WhatsApp downloads."""

    def post(self, request: HttpRequest) -> HttpResponse:
        err = _check_api_key(request)
        if err:
            return err

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponse("Invalid JSON", status=400)

        media_id = data.get("media_id")
        audio_b64 = data.get("audio_base64")
        mime_type = data.get("mime_type", "audio/ogg")

        if not media_id or not audio_b64:
            return HttpResponse("media_id and audio_base64 required", status=400)

        audio_bytes = base64.b64decode(audio_b64)
        MockWhatsAppClient.media_downloads[media_id] = (audio_bytes, mime_type)

        logger.info(f"[TEST] Loaded mock media {media_id}: {len(audio_bytes)} bytes")
        return HttpResponse("OK", status=200)


class SampleAudioView(View):
    """GET /test/r2-sample-audio/?key=...&bucket=... — fetch a specific audio file from R2.

    Query params:
        key     — exact R2 object key (required)
        bucket  — R2 bucket to read from (default: production bucket "auto-tuck-shop")
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        err = _check_api_key(request)
        if err:
            return err

        key = request.GET.get("key", "")
        bucket = request.GET.get("bucket", "auto-tuck-shop")

        if not key:
            return HttpResponse("'key' query param is required", status=400)

        if not all([settings.R2_ACCESS_KEY_ID, settings.R2_SECRET_ACCESS_KEY, settings.R2_ENDPOINT_URL]):
            return HttpResponse("R2 credentials not configured", status=503)

        client = boto3.client(
            "s3",
            endpoint_url=settings.R2_ENDPOINT_URL,
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4"),
        )

        try:
            obj = client.get_object(Bucket=bucket, Key=key)
            audio_bytes = obj["Body"].read()
            content_type = obj.get("ContentType", "audio/ogg")

            logger.info(f"[TEST] Serving audio from R2: {bucket}/{key} ({len(audio_bytes)} bytes)")
            response = HttpResponse(audio_bytes, content_type=content_type)
            response["X-R2-Key"] = key
            return response

        except client.exceptions.NoSuchKey:
            return HttpResponse(f"Key not found: {bucket}/{key}", status=404)
        except Exception as e:
            logger.error(f"[TEST] Failed to fetch audio from R2: {e}", exc_info=True)
            return HttpResponse(f"R2 error: {e}", status=500)
