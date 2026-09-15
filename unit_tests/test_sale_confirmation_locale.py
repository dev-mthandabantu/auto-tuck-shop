"""Regression tests for #290: sale confirmation messages/buttons must respect
the shop owner's stored language (UserProfile.language), not always render
in English.

process_sale_message_unified already threads `lang` through every t(...) call
and button title — these tests exercise the real function end-to-end (not
mocked) to prove that plumbing actually produces localized output, since #290
was filed as "confirmations stay English regardless of message language"
without confirming whether the root cause was here or was purely a symptom
of the (now-fixed, #286) language-switching bug leaving profiles stuck on
the English default.

Run with:
    python manage.py test unit_tests.test_sale_confirmation_locale
"""

import asyncio

from unit_tests.async_helpers import run_async_test as _run
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.test import TransactionTestCase

from apps.catalog.models import Product
from apps.core.models import Company
from apps.whatsapp.services.sale_handler import process_sale_message_unified
from apps.whatsapp.services.message_parser import UnifiedMessageResult




def _make_company(slug="test-shop-confirm-locale"):
    return Company.objects.create(name="Test Shop", slug=slug, active=True)


class SaleConfirmationLocaleTest(TransactionTestCase):
    def setUp(self):
        self.company = _make_company()
        self.product = Product.objects.create(name="Mazoe", company=self.company)
        self.result = UnifiedMessageResult(
            intent="sale",
            confidence=0.9,
            items=[{
                "product_name": "Mazoe",
                "quantity": 2,
                "unit_price": Decimal("3.00"),
                "currency": "USD",
            }],
            currency="USD",
        )

    def _run_confirmation(self, lang):
        send_mock = AsyncMock(return_value="wamid.fake_sid")
        with patch("apps.whatsapp.services.sale_handler._send_response_with_buttons", new=send_mock):
            _run(process_sale_message_unified(
                message_id="msg_locale_1",
                sender="+263771000199",
                text="2 mazoe $3 rimwe",
                company=self.company,
                result=self.result,
                lang=lang,
            ))
        return send_mock

    def test_shona_profile_gets_shona_confirmation_buttons(self):
        send_mock = self._run_confirmation(lang="sn")
        send_mock.assert_called_once()
        _sender, _text, buttons = send_mock.call_args[0]
        titles = [b["title"] for b in buttons]
        self.assertEqual(titles, ["Ndizvo", "Gadzirisa"])

    def test_shona_profile_gets_shona_total_line(self):
        send_mock = self._run_confirmation(lang="sn")
        _sender, text, _buttons = send_mock.call_args[0]
        self.assertIn("Zvese pamwe", text)
        self.assertNotIn("Total:", text)

    def test_english_profile_still_gets_english_confirmation_buttons(self):
        """Regression guard — the default/English path must not have broken."""
        send_mock = self._run_confirmation(lang="en")
        _sender, _text, buttons = send_mock.call_args[0]
        titles = [b["title"] for b in buttons]
        self.assertEqual(titles, ["Looks good", "Fix mistake"])
