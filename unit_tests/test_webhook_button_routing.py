"""Unit tests for message_type routing in WhatsAppWebhookView._handle_message — #209.

Regression coverage for the routing gap Brighton's review caught: a Meta
template's quick-reply button tap (e.g. ats_reengagement) arrives as
message_type "button" with the payload under button.payload — a different
shape from the interactive/button_reply payload used by buttons we send
ourselves. The tap-recording tests in test_unhandled_button_tap.py call
_handle_unhandled_button_tap_async directly, which proves the handler works
once invoked but does not prove the webhook ever invokes it — these tests go
through _handle_message itself so a routing regression here would actually
fail a test, not just the handler tests.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase

from apps.core.models import Company, UserProfile
from apps.whatsapp.views import WhatsAppWebhookView


def _make_company(name="Test Shop"):
    return Company.objects.create(
        name=name,
        slug=name.lower().replace(" ", "-"),
        active=True,
    )


def _make_profile(company, phone="+263771000001"):
    user = User.objects.create_user(username=phone, password="x")
    return UserProfile.objects.create(
        user=user,
        company=company,
        phone_number=phone,
        role=UserProfile.Role.OWNER,
        language="en",
    )


def _template_button_message(payload="unknown_reengagement_payload", wamid="wamid.button_test_1"):
    """A realistic Meta webhook payload for a template quick-reply tap —
    message_type "button", not "interactive"."""
    return {
        "id": wamid,
        "from": "263771000001",
        "type": "button",
        "button": {
            "payload": payload,
            "text": "Yes please",
        },
    }


def _interactive_button_message(button_id="confirm_1", wamid="wamid.interactive_test_1"):
    """The shape used by buttons we send ourselves via send_message_with_buttons."""
    return {
        "id": wamid,
        "from": "263771000001",
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": button_id, "title": "Confirm"},
        },
    }


class TemplateButtonRoutingTest(TransactionTestCase):
    """message_type "button" (Meta template quick-reply) must reach
    _handle_button_response, not fall through to the "Ignoring message
    type" catch-all — this is the exact case that motivated #209."""

    def setUp(self):
        self.company = _make_company()
        self.profile = _make_profile(self.company)
        self.view = WhatsAppWebhookView()

    def test_template_button_tap_reaches_button_response_handler(self):
        message = _template_button_message()
        with patch.object(self.view, "_handle_button_response") as mock_handle:
            self.view._handle_message(message)
        mock_handle.assert_called_once()
        args = mock_handle.call_args[0]
        self.assertEqual(args[1], "unknown_reengagement_payload")

    def test_template_button_tap_records_unhandled_tap(self):
        """End-to-end: an unrecognized template button payload, routed
        through the real webhook entry point, actually records the tap on
        Company — not just that the handler is invoked, but that it does
        what #209 needs."""
        self.assertIsNone(self.company.last_unhandled_button_tap_at)
        message = _template_button_message(payload="reengagement_yes_unrecognized")
        mock_client = MagicMock()
        mock_client.mark_as_read = AsyncMock(return_value=None)
        with patch("apps.whatsapp.services.whatsapp_client.get_whatsapp_client", return_value=mock_client):
            self.view._handle_message(message)
        self.company.refresh_from_db()
        self.assertIsNotNone(self.company.last_unhandled_button_tap_at)

    def test_template_button_with_empty_payload_is_ignored_not_crashed(self):
        message = _template_button_message(payload="")
        with patch.object(self.view, "_handle_button_response") as mock_handle:
            self.view._handle_message(message)
        mock_handle.assert_not_called()

    def test_interactive_button_still_routes_correctly(self):
        """Regression guard: adding the new "button" branch must not change
        routing for the existing interactive/button_reply shape."""
        message = _interactive_button_message(button_id="closing_early")
        with patch.object(self.view, "_handle_button_response") as mock_handle:
            self.view._handle_message(message)
        mock_handle.assert_called_once()
        args = mock_handle.call_args[0]
        self.assertEqual(args[1], "closing_early")

    def test_unknown_message_type_still_falls_through_harmlessly(self):
        message = {"id": "wamid.x", "from": "263771000001", "type": "sticker"}
        with patch.object(self.view, "_handle_button_response") as mock_handle:
            self.view._handle_message(message)
        mock_handle.assert_not_called()
