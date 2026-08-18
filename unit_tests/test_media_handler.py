"""Unit tests for inbound image/media message handling.

Tests the _process_media_message_async handler in webhook_handler.py.
Vision LLM calls are mocked — these tests do not require OPENROUTER_API_KEY.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase

from apps.core.models import Company, UserProfile
from apps.whatsapp.services.message_parser import UnifiedMessageResult
from apps.whatsapp.services.webhook_handler import _process_media_message_async


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_profile(phone="+263771000099"):
    company = Company.objects.create(
        name="Test Shop",
        slug="test-shop-media",
        active=True,
    )
    user = User.objects.create_user(username=phone, password="x")
    return UserProfile.objects.create(
        user=user,
        company=company,
        phone_number=phone,
        role=UserProfile.Role.OWNER,
        language="en",
    )


class MediaHandlerTest(TransactionTestCase):

    def setUp(self):
        self.profile = _make_profile()
        self.sender = self.profile.phone_number

    def _run_handler(self, parse_result, media_type="image", r2_url="https://cdn.example.com/img.jpg"):
        send_mock = AsyncMock()
        sale_mock = AsyncMock()
        with (
            patch("apps.whatsapp.services.message_parser.parse_image_for_sale", new=AsyncMock(return_value=parse_result)),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
            patch("apps.whatsapp.services.sale_handler.process_sale_message_unified", new=sale_mock),
        ):
            _run(_process_media_message_async("msg_001", self.sender, r2_url, media_type, self.profile))
        return send_mock, sale_mock

    def test_image_no_sale_sends_fallback(self):
        """Vision returns no items → fallback prompt sent."""
        no_sale_result = UnifiedMessageResult(intent="other", confidence=0.9)
        send_mock, sale_mock = self._run_handler(no_sale_result)

        sale_mock.assert_not_called()
        send_mock.assert_called_once()
        msg = send_mock.call_args[0][1]
        self.assertIn("could not read", msg.lower())

    def test_image_with_items_triggers_sale_flow(self):
        """Vision returns items → sale confirmation flow called."""
        from apps.sales.services import ParsedSaleItem
        from decimal import Decimal

        sale_result = UnifiedMessageResult(
            intent="sale",
            confidence=0.85,
            items=[ParsedSaleItem(product_name="Mazoe", quantity=2, unit_price=Decimal("3.00"), currency="USD")],
            currency="USD",
        )

        send_mock = AsyncMock()
        sale_mock = AsyncMock()
        with (
            patch("apps.whatsapp.services.message_parser.parse_image_for_sale", new=AsyncMock(return_value=sale_result)),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
            patch("apps.whatsapp.services.sale_handler.process_sale_message_unified", new=sale_mock),
        ):
            _run(_process_media_message_async("msg_001b", self.sender, "https://cdn.example.com/img.jpg", "image", self.profile))

        sale_mock.assert_called_once()
        send_mock.assert_not_called()

    def test_video_sends_unsupported(self):
        """Video message → unsupported message, no vision call."""
        send_mock = AsyncMock()
        parse_mock = AsyncMock()
        with (
            patch("apps.whatsapp.services.message_parser.parse_image_for_sale", new=parse_mock),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
        ):
            _run(_process_media_message_async("msg_002", self.sender, "https://cdn.example.com/vid.mp4", "video", self.profile))

        parse_mock.assert_not_called()
        send_mock.assert_called_once()
        msg = send_mock.call_args[0][1]
        self.assertIn("voice", msg.lower())

    def test_document_sends_unsupported(self):
        """Document message → unsupported message, no vision call."""
        send_mock = AsyncMock()
        parse_mock = AsyncMock()
        with (
            patch("apps.whatsapp.services.message_parser.parse_image_for_sale", new=parse_mock),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
        ):
            _run(_process_media_message_async("msg_003", self.sender, "https://cdn.example.com/doc.pdf", "document", self.profile))

        parse_mock.assert_not_called()
        send_mock.assert_called_once()

    def test_vision_exception_sends_fallback(self):
        """Vision parse raises exception → graceful fallback, no crash."""
        send_mock = AsyncMock()
        with (
            patch("apps.whatsapp.services.message_parser.parse_image_for_sale", new=AsyncMock(side_effect=Exception("OpenRouter timeout"))),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
        ):
            _run(_process_media_message_async("msg_004", self.sender, "https://cdn.example.com/img.jpg", "image", self.profile))

        send_mock.assert_called_once()
        msg = send_mock.call_args[0][1]
        self.assertIn("could not read", msg.lower())

    def test_missing_r2_url_sends_fallback(self):
        """Empty R2 URL (upload failed) → fallback without calling vision."""
        send_mock = AsyncMock()
        parse_mock = AsyncMock()
        with (
            patch("apps.whatsapp.services.message_parser.parse_image_for_sale", new=parse_mock),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=send_mock),
        ):
            _run(_process_media_message_async("msg_005", self.sender, "", "image", self.profile))

        parse_mock.assert_not_called()
        send_mock.assert_called_once()

    def test_caption_is_forwarded_to_vision_parse(self):
        """#294 regression: an image caption must reach parse_image_for_sale, not get dropped."""
        no_sale_result = UnifiedMessageResult(intent="other", confidence=0.9)
        parse_mock = AsyncMock(return_value=no_sale_result)
        with (
            patch("apps.whatsapp.services.message_parser.parse_image_for_sale", new=parse_mock),
            patch("apps.whatsapp.services.webhook_handler._send_response", new=AsyncMock()),
        ):
            _run(_process_media_message_async(
                "msg_006", self.sender, "https://cdn.example.com/img.jpg", "image", self.profile,
                caption="3 pen @ 2usd each",
            ))

        parse_mock.assert_called_once()
        self.assertEqual(parse_mock.call_args.kwargs.get("caption"), "3 pen @ 2usd each")
