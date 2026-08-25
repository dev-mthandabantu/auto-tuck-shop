"""Client for sending messages via Meta WhatsApp Business Cloud API."""

import logging

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings

from utils.timing import track

logger = logging.getLogger(__name__)

META_GRAPH_API_URL = "https://graph.facebook.com/v21.0"


def get_whatsapp_client():
    """Factory function that returns mock or real WhatsApp client based on settings."""
    if settings.USE_MOCK_WHATSAPP:
        from services.whatsapp.mock_client import MockWhatsAppClient
        logger.info("Using MockWhatsAppClient (USE_MOCK_WHATSAPP=True)")
        return MockWhatsAppClient()

    return WhatsAppClient()


def _record_outbound_message_sync(
    phone_number: str,
    message_type: str,
    content: str = "",
    whatsapp_message_id: str = "",
    api_success: bool = True,
    api_error: str = "",
    raw_payload: dict | None = None,
) -> None:
    """
    Record an outbound WhatsApp message to the database (sync version).

    This is a fire-and-forget operation - failures won't break message sending.
    """
    try:
        from apps.core.models import UserProfile
        from apps.whatsapp.models import WhatsAppMessage

        # Normalize phone number
        if phone_number.startswith("whatsapp:"):
            phone_number = phone_number[9:]
        if not phone_number.startswith("+"):
            phone_number = f"+{phone_number}"

        # Try to lookup user profile for enrichment
        user_profile = None
        company = None
        try:
            user_profile = UserProfile.objects.select_related("company").get(phone_number=phone_number)
            company = user_profile.company
        except UserProfile.DoesNotExist:
            pass

        WhatsAppMessage.objects.create(
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type=message_type,
            phone_number=phone_number,
            content=content,
            whatsapp_message_id=whatsapp_message_id,
            api_success=api_success,
            api_error=api_error,
            raw_payload=raw_payload,
            user_profile=user_profile,
            company=company,
        )
        logger.debug(f"Recorded outbound message to {phone_number}")
    except Exception as e:
        logger.error(f"Failed to record outbound message: {e}", exc_info=True)


# Create async wrapper
_record_outbound_message = sync_to_async(_record_outbound_message_sync, thread_sensitive=True)


class WhatsAppClient:
    """Client for Meta WhatsApp Business Cloud API."""

    def __init__(self):
        self.access_token = settings.META_WHATSAPP_ACCESS_TOKEN
        self.phone_number_id = settings.META_WHATSAPP_PHONE_NUMBER_ID

    def _get_api_url(self) -> str:
        return f"{META_GRAPH_API_URL}/{self.phone_number_id}/messages"

    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _normalize_phone_number(self, phone: str) -> str:
        """
        Normalize phone number for Meta API.

        Meta expects plain numbers without 'whatsapp:' prefix and without '+'.
        e.g., '1234567890' or '27821234567'
        """
        # Remove whatsapp: prefix if present
        if phone.startswith("whatsapp:"):
            phone = phone[9:]
        # Remove + prefix if present
        if phone.startswith("+"):
            phone = phone[1:]
        return phone

    async def send_message_with_buttons(
        self,
        to: str,
        body: str,
        buttons: list[dict[str, str]],
        reply_to: str | None = None,
    ) -> str | None:
        """
        Send a WhatsApp message with interactive buttons via Meta Cloud API.

        Args:
            to: The recipient's phone number (e.g., +1234567890 or whatsapp:+1234567890)
            body: The message body text
            buttons: List of button dicts with 'title' and 'id' keys
            reply_to: Optional message ID to reply to (for quoted replies)

        Returns:
            The message ID if successful, None otherwise
        """
        # Create content string with buttons for recording
        button_titles = [btn["title"] for btn in buttons[:3]]
        content_with_buttons = f"{body}\n\nButtons: {', '.join(button_titles)}"

        if not self.access_token or not self.phone_number_id:
            logger.error("Meta WhatsApp credentials not configured")
            await _record_outbound_message(
                phone_number=to,
                message_type="INTERACTIVE_BUTTON",
                content=content_with_buttons,
                api_success=False,
                api_error="Meta WhatsApp credentials not configured",
            )
            return None

        to_number = self._normalize_phone_number(to)

        # Build interactive message with buttons (max 3 buttons)
        button_objects = [
            {
                "type": "reply",
                "reply": {
                    "id": btn["id"],
                    "title": btn["title"][:20],  # Max 20 chars for button title
                }
            }
            for btn in buttons[:3]
        ]

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": body[:1024],  # Max 1024 chars for body
                },
                "action": {
                    "buttons": button_objects,
                }
            }
        }

        # Add reply context if provided
        if reply_to:
            payload["context"] = {"message_id": reply_to}

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    self._get_api_url(),
                    headers=self._get_headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                message_id = data.get("messages", [{}])[0].get("id")
                logger.info(f"Sent WhatsApp button message to {to_number}, id={message_id}")

                # Record successful message
                await _record_outbound_message(
                    phone_number=to,
                    message_type="INTERACTIVE_BUTTON",
                    content=content_with_buttons,
                    whatsapp_message_id=message_id,
                    api_success=True,
                )

                return message_id
            except httpx.HTTPStatusError as e:
                error_msg = f"{e.response.status_code} - {e.response.text}"
                logger.exception(f"Failed to send WhatsApp button message: {error_msg}")

                # Record failed message
                await _record_outbound_message(
                    phone_number=to,
                    message_type="INTERACTIVE_BUTTON",
                    content=content_with_buttons,
                    api_success=False,
                    api_error=error_msg,
                )

                return None
            except httpx.RequestError as e:
                error_msg = str(e)
                logger.exception(f"Meta WhatsApp request error: {error_msg}")

                # Record failed message
                await _record_outbound_message(
                    phone_number=to,
                    message_type="INTERACTIVE_BUTTON",
                    content=content_with_buttons,
                    api_success=False,
                    api_error=error_msg,
                )

                return None

    async def send_message(self, to: str, text: str, reply_to: str | None = None) -> bool:
        """
        Send a WhatsApp message via Meta Cloud API.

        Args:
            to: The recipient's phone number (e.g., +1234567890 or whatsapp:+1234567890)
            text: The message text
            reply_to: Optional message ID to reply to (for quoted replies)

        Returns:
            True if successful, False otherwise
        """
        async with track("meta_send"):
            if not self.access_token or not self.phone_number_id:
                logger.error("Meta WhatsApp credentials not configured")
                await _record_outbound_message(
                    phone_number=to,
                    message_type="TEXT",
                    content=text,
                    api_success=False,
                    api_error="Meta WhatsApp credentials not configured",
                )
                return False

            to_number = self._normalize_phone_number(to)

            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_number,
                "type": "text",
                "text": {
                    "preview_url": False,
                    "body": text,
                }
            }

            # Add reply context if provided
            if reply_to:
                payload["context"] = {"message_id": reply_to}

            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    response = await client.post(
                        self._get_api_url(),
                        headers=self._get_headers(),
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                    message_id = data.get("messages", [{}])[0].get("id", "")

                    logger.info(f"Sent WhatsApp message to {to_number}")

                    # Record successful message
                    await _record_outbound_message(
                        phone_number=to,
                        message_type="TEXT",
                        content=text,
                        whatsapp_message_id=message_id,
                        api_success=True,
                    )

                    return True
                except httpx.HTTPStatusError as e:
                    error_msg = f"{e.response.status_code} - {e.response.text}"
                    logger.exception(f"Failed to send WhatsApp message: {error_msg}")

                    # Record failed message
                    await _record_outbound_message(
                        phone_number=to,
                        message_type="TEXT",
                        content=text,
                        api_success=False,
                        api_error=error_msg,
                    )

                    return False
                except httpx.RequestError as e:
                    error_msg = str(e)
                    logger.exception(f"Meta WhatsApp request error: {error_msg}")

                    # Record failed message
                    await _record_outbound_message(
                        phone_number=to,
                        message_type="TEXT",
                        content=text,
                        api_success=False,
                        api_error=error_msg,
                    )

                    return False

    async def send_template_message(
        self,
        to: str,
        template_name: str,
        language_code: str = "en",
        components: list | None = None,
    ) -> bool:
        """Send a pre-approved Meta template message (required for out-of-24h-window messages)."""
        from apps.whatsapp.models import WhatsAppMessage

        if not self.access_token or not self.phone_number_id:
            logger.error("Meta WhatsApp credentials not configured for send_template_message")
            return False

        to_number = self._normalize_phone_number(to)
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
                "components": components or [],
            },
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(self._get_api_url(), headers=self._get_headers(), json=payload)
                response.raise_for_status()
                data = response.json()
                message_id = data.get("messages", [{}])[0].get("id", "")
                logger.info(f"Sent template '{template_name}' to {to_number}: {message_id}")
                await _record_outbound_message(
                    phone_number=to,
                    message_type=WhatsAppMessage.MessageType.TEMPLATE,
                    content=f"[template: {template_name}]",
                    whatsapp_message_id=message_id,
                    api_success=True,
                )
                return True
            except httpx.HTTPStatusError as e:
                error_msg = f"{e.response.status_code} - {e.response.text}"
                logger.exception(f"Failed to send template '{template_name}' to {to_number}: {error_msg}")
                await _record_outbound_message(
                    phone_number=to,
                    message_type=WhatsAppMessage.MessageType.TEMPLATE,
                    content=f"[template: {template_name}]",
                    api_success=False,
                    api_error=error_msg,
                )
                return False
            except httpx.RequestError as e:
                error_msg = str(e)
                logger.exception(f"Meta WhatsApp request error sending template '{template_name}' to {to_number}: {error_msg}")
                await _record_outbound_message(
                    phone_number=to,
                    message_type=WhatsAppMessage.MessageType.TEMPLATE,
                    content=f"[template: {template_name}]",
                    api_success=False,
                    api_error=error_msg,
                )
                return False

    async def mark_as_read(self, message_id: str) -> bool:
        """Mark an inbound message as read (send read receipt to Meta)."""
        if not self.access_token or not self.phone_number_id:
            logger.error("Meta WhatsApp credentials not configured for mark_as_read")
            return False

        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(self._get_api_url(), headers=self._get_headers(), json=payload)
                response.raise_for_status()
                logger.info(f"Marked message {message_id} as read")
                return True
            except Exception:
                logger.exception(f"Failed to mark message {message_id} as read")
                return False

    async def send_typing_indicator(self, to: str, action: str = "typing_on", message_id: str | None = None) -> bool:
        """Send typing indicator via Meta Cloud API.

        Must be combined with a read receipt (status=read + message_id). Typing_off
        is a no-op — the indicator auto-dismisses after 25s or when a reply is sent.
        Requires message_id; skips silently if not provided.
        """
        if action == "typing_off":
            return True

        if not message_id:
            return True

        if not self.access_token or not self.phone_number_id:
            return False

        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(self._get_api_url(), headers=self._get_headers(), json=payload)
                response.raise_for_status()
                logger.info(f"Sent typing indicator to {to}")
                return True
            except Exception:
                logger.exception(f"Failed to send typing indicator to {to}")
                return False

    async def send_image(self, to: str, image_url: str, caption: str = "") -> bool:
        """Send a WhatsApp image message via URL via Meta Cloud API."""
        if not self.access_token or not self.phone_number_id:
            logger.error("Meta WhatsApp credentials not configured")
            await _record_outbound_message(
                phone_number=to,
                message_type="IMAGE",
                content=caption,
                api_success=False,
                api_error="Meta WhatsApp credentials not configured",
            )
            return False

        to_number = self._normalize_phone_number(to)
        image_payload: dict = {"link": image_url}
        if caption:
            image_payload["caption"] = caption[:1024]

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "image",
            "image": image_payload,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    self._get_api_url(),
                    headers=self._get_headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                message_id = data.get("messages", [{}])[0].get("id", "")
                logger.info(f"Sent WhatsApp image to {to_number}, id={message_id}")
                await _record_outbound_message(
                    phone_number=to,
                    message_type="IMAGE",
                    content=caption,
                    whatsapp_message_id=message_id,
                    api_success=True,
                )
                return True
            except httpx.HTTPStatusError as e:
                error_msg = f"{e.response.status_code} - {e.response.text}"
                logger.exception(f"Failed to send WhatsApp image: {error_msg}")
                await _record_outbound_message(
                    phone_number=to,
                    message_type="IMAGE",
                    content=caption,
                    api_success=False,
                    api_error=error_msg,
                )
                return False
            except httpx.RequestError as e:
                error_msg = str(e)
                logger.exception(f"Meta WhatsApp image request error: {error_msg}")
                await _record_outbound_message(
                    phone_number=to,
                    message_type="IMAGE",
                    content=caption,
                    api_success=False,
                    api_error=error_msg,
                )
                return False

    async def get_media_url(self, media_id: str) -> tuple[str, str] | None:
        """
        Get the pre-signed URL for a media file without downloading it.

        Args:
            media_id: The Meta media ID

        Returns:
            Tuple of (media_url, mime_type) if successful, None otherwise
        """
        async with track("meta_get_url"):
            if not self.access_token:
                logger.error("Meta WhatsApp credentials not configured")
                return None

            media_info_url = f"{META_GRAPH_API_URL}/{media_id}"

            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    response = await client.get(
                        media_info_url,
                        headers=self._get_headers(),
                    )
                    response.raise_for_status()
                    media_info = response.json()

                    media_url = media_info.get("url")
                    mime_type = media_info.get("mime_type", "audio/ogg")

                    if not media_url:
                        logger.error(f"No URL in media info response: {media_info}")
                        return None

                    logger.info(f"Got media URL for {media_id}, type={mime_type}")
                    return (media_url, mime_type)

                except httpx.HTTPStatusError as e:
                    logger.exception(f"Failed to get media info: {e.response.status_code} - {e.response.text}")
                    return None
                except httpx.RequestError as e:
                    logger.exception(f"Media info request error: {e}")
                    return None

    async def download_media(self, media_id: str) -> tuple[bytes, str] | None:
        """
        Download media file from Meta's CDN.

        Args:
            media_id: The Meta media ID

        Returns:
            Tuple of (audio_data, mime_type) if successful, None otherwise
        """
        async with track("meta_download"):
            if not self.access_token:
                logger.error("Meta WhatsApp credentials not configured")
                return None

            # Step 1: Get media info (URL and mime type)
            media_info_url = f"{META_GRAPH_API_URL}/{media_id}"

            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    response = await client.get(
                        media_info_url,
                        headers=self._get_headers(),
                    )
                    response.raise_for_status()
                    media_info = response.json()

                    media_url = media_info.get("url")
                    mime_type = media_info.get("mime_type", "audio/ogg")

                    if not media_url:
                        logger.error(f"No URL in media info response: {media_info}")
                        return None

                except httpx.HTTPStatusError as e:
                    logger.exception(f"Failed to get media info: {e.response.status_code} - {e.response.text}")
                    return None
                except httpx.RequestError as e:
                    logger.exception(f"Media info request error: {e}")
                    return None

            # Step 2: Download the actual media file
            download_headers = {
                "Authorization": f"Bearer {self.access_token}",
                "User-Agent": "AutoTuckShop/1.0",
            }

            async with httpx.AsyncClient(timeout=60.0) as client:
                try:
                    response = await client.get(
                        media_url,
                        headers=download_headers,
                    )
                    response.raise_for_status()

                    logger.info(f"Downloaded media {media_id}, size={len(response.content)} bytes, type={mime_type}")
                    return (response.content, mime_type)

                except httpx.HTTPStatusError as e:
                    logger.exception(f"Failed to download media: {e.response.status_code} - {e.response.text}")
                    return None
                except httpx.RequestError as e:
                    logger.exception(f"Media download request error: {e}")
                    return None
