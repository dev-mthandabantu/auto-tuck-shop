"""Regression tests for #356: the "Unmatched" sale response must tell the
shop owner what quantity was understood and exactly what to send next,
instead of a bare "⚠ Unmatched: <name>" dead end.

Exercises process_sale_message_unified end-to-end (real DB, no LLM call —
the parsed result is constructed directly) so these assert on the actual
copy shown to the owner, not just the intermediate data shape.
"""

import asyncio

from unit_tests.async_helpers import run_async_test as _run
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.test import TransactionTestCase

from apps.core.models import Company
from apps.whatsapp.services.message_parser import UnifiedMessageResult
from apps.whatsapp.services.sale_handler import process_sale_message_unified




def _make_company(slug="test-shop-unmatched", currency="USD"):
    return Company.objects.create(name="Test Shop", slug=slug, active=True, currency=currency)


class UnmatchedSaleMessageTest(TransactionTestCase):

    def _run_sale(self, company, product_name="chips", quantity=3):
        result = UnifiedMessageResult(
            intent="sale",
            confidence=0.9,
            items=[{
                "product_name": product_name,
                "quantity": quantity,
                "unit_price": None,
                "currency": None,
            }],
        )
        send_mock = AsyncMock(return_value=None)
        with patch("apps.whatsapp.services.sale_handler._send_response", new=send_mock):
            _run(process_sale_message_unified(
                message_id="msg_unmatched_1",
                sender="+263771000198",
                text=f"{quantity} {product_name}",
                company=company,
                result=result,
            ))
        return send_mock

    def test_unmatched_message_includes_quantity_and_product(self):
        company = _make_company()
        send_mock = self._run_sale(company, product_name="chips", quantity=3)

        send_mock.assert_called_once()
        _sender, text = send_mock.call_args[0][:2]
        self.assertIn("3", text)
        self.assertIn("chips", text)

    def test_unmatched_message_gives_a_resend_example_with_shop_currency(self):
        company = _make_company(currency="ZAR")
        send_mock = self._run_sale(company, product_name="bread", quantity=2)

        _sender, text = send_mock.call_args[0][:2]
        self.assertIn("R5", text)

    def test_unmatched_message_defaults_to_usd_example_when_no_company_currency_set(self):
        company = _make_company(currency="USD")
        send_mock = self._run_sale(company, product_name="bread", quantity=2)

        _sender, text = send_mock.call_args[0][:2]
        self.assertIn("$5", text)

    def test_emoji_item_with_no_price_reports_canonicalized_name(self):
        """The Unmatched message should show the canonicalized name ("car"),
        not the raw emoji the LLM might have returned — see #356."""
        company = _make_company()
        send_mock = self._run_sale(company, product_name="🚗🚗🚗", quantity=1)

        _sender, text = send_mock.call_args[0][:2]
        self.assertIn("automobile", text)
        self.assertNotIn("🚗", text)
