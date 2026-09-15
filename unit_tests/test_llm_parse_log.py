"""Unit tests for LlmParseLog creation via parse_message_unified."""

import asyncio

from unit_tests.async_helpers import run_async_test as _run
from unittest.mock import AsyncMock, patch

from django.test import TransactionTestCase

from apps.whatsapp.models import LlmParseLog, WhatsAppMessage
from apps.whatsapp.services.message_parser import parse_message_unified
from services.openrouter.client import ParseJsonResult




class LlmParseLogSuccessTest(TransactionTestCase):

    def setUp(self):
        self.wa_msg = WhatsAppMessage.objects.create(
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type=WhatsAppMessage.MessageType.TEXT,
            phone_number="+263771000001",
            content="2 cokes",
            whatsapp_message_id="wamid.test_llmlog_001",
        )

    @patch("apps.whatsapp.services.message_parser.OpenRouterClient")
    def test_log_created_on_success(self, MockClient):
        mock_instance = MockClient.return_value
        mock_instance.parse_json_response = AsyncMock(return_value=ParseJsonResult(
            response={
                "intent": "sale",
                "confidence": 0.95,
                "items": [{"product_name": "coke", "quantity": 2, "unit_price": None, "currency": None}],
                "currency": None,
                "notes": None,
            },
            prompt_tokens=120,
            completion_tokens=45,
        ))

        _run(parse_message_unified("2 cokes", message_id="wamid.test_llmlog_001"))

        log = LlmParseLog.objects.get(message=self.wa_msg)
        self.assertEqual(log.intent, "sale")
        self.assertAlmostEqual(log.confidence, 0.95)
        self.assertEqual(log.prompt_tokens, 120)
        self.assertEqual(log.completion_tokens, 45)
        self.assertEqual(log.parse_error, "")
        self.assertIsNotNone(log.raw_response)

    @patch("apps.whatsapp.services.message_parser.OpenRouterClient")
    def test_log_created_for_sales_query(self, MockClient):
        mock_instance = MockClient.return_value
        mock_instance.parse_json_response = AsyncMock(return_value=ParseJsonResult(
            response={"intent": "sales_query", "confidence": 0.88, "timeframe": "today", "notes": None},
            prompt_tokens=80,
            completion_tokens=20,
        ))

        _run(parse_message_unified("how much today", message_id="wamid.test_llmlog_001"))

        log = LlmParseLog.objects.get(message=self.wa_msg)
        self.assertEqual(log.intent, "sales_query")
        self.assertEqual(log.prompt_tokens, 80)


class LlmParseLogErrorTest(TransactionTestCase):

    def setUp(self):
        self.wa_msg = WhatsAppMessage.objects.create(
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type=WhatsAppMessage.MessageType.TEXT,
            phone_number="+263771000002",
            content="test message",
            whatsapp_message_id="wamid.test_llmlog_002",
        )

    @patch("apps.whatsapp.services.message_parser.OpenRouterClient")
    def test_log_created_on_llm_failure(self, MockClient):
        mock_instance = MockClient.return_value
        mock_instance.parse_json_response = AsyncMock(side_effect=Exception("OpenRouter timeout"))

        with self.assertRaises(Exception):
            _run(parse_message_unified("some message", message_id="wamid.test_llmlog_002"))

        log = LlmParseLog.objects.get(message=self.wa_msg)
        self.assertIn("OpenRouter timeout", log.parse_error)
        self.assertEqual(log.intent, "")
        self.assertIsNone(log.confidence)

    @patch("apps.whatsapp.services.message_parser.OpenRouterClient")
    def test_no_log_without_message_id(self, MockClient):
        mock_instance = MockClient.return_value
        mock_instance.parse_json_response = AsyncMock(return_value=ParseJsonResult(
            response={"intent": "sale", "confidence": 0.9, "items": [], "currency": None, "notes": None},
            prompt_tokens=50,
            completion_tokens=10,
        ))

        _run(parse_message_unified("2 bread"))

        self.assertEqual(LlmParseLog.objects.count(), 0)
