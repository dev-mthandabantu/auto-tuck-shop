"""What a Sentry event from the WhatsApp webhook carries (#437).

The rule (docs/Integrations.md, "Sentry context"): internal IDs only. These tests
start Sentry with the production privacy options and a fake transport, send a
webhook through the real view, make it fail, and search the captured event for
the sender's phone number, the shop name, the WhatsApp profile name, the
message text and the LLM's notes. No network calls: the transport keeps events
in a list, and the LLM client is a fake.
"""

import asyncio
import base64
import json
import uuid
from unittest.mock import AsyncMock, patch

import sentry_sdk
from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import Transport

from apps.core.models import Company, UserProfile, WaitlistEntry
from apps.whatsapp.models import WhatsAppMessage
from config.sentry import PRIVACY_INTEGRATIONS, PRIVACY_OPTIONS, redact_personal_data, redact_transaction
from services.openrouter.client import ParseJsonResult

PHONE = "+263771234987"
SHOP_NAME = "Zvigarwe Mart"
CONTACT_NAME = "Tendai Moyo"
TEXT = "5 bread mangwanani"
NOTES = "Owner asked if Chipo paid for 5 bread"
# Meta's message IDs embed the sender's number in base64
WAMID = "wamid.HBgM" + base64.b64encode(PHONE[1:].encode()).decode() + "FQIAEhgUM0EB"


class _CaptureTransport(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.events = []
        self.transactions = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.type == "event":
                self.events.append(item.payload.json)
            elif item.type == "transaction":
                self.transactions.append(item.payload.json)


class _FakeOpenRouter:
    """The real parser runs; only the LLM call is faked. Its notes repeat the
    message, as the real model's do."""

    async def parse_json_response(self, messages):
        return ParseJsonResult(
            response={"intent": "other", "other_sub_intent": "question", "confidence": 0.9, "notes": NOTES},
            prompt_tokens=1, completion_tokens=1,
        )


def _start_sentry(test, integrations=(), **options):
    """Start Sentry with the production privacy options and a fake transport.
    Django's test client doesn't get the per-request scope gunicorn gets, so
    each test runs in its own scope, emptied of tags other tests left behind."""
    transport = _CaptureTransport()
    sentry_sdk.init(
        dsn="https://key@o0.ingest.sentry.io/0",
        transport=transport,
        integrations=[*integrations, *PRIVACY_INTEGRATIONS],
        **options,
        **PRIVACY_OPTIONS,
    )
    test.enterContext(sentry_sdk.isolation_scope()).clear()
    test.enterContext(sentry_sdk.new_scope()).clear()
    test.addCleanup(sentry_sdk.init, dsn=None)
    return transport


def _payload(*messages):
    return {
        "entry": [{"changes": [{"field": "messages", "value": {
            "contacts": [{"profile": {"name": CONTACT_NAME}}],
            "messages": list(messages),
        }}]}]
    }


def _text_message(sender=PHONE, wamid=WAMID, text=TEXT):
    return {"id": wamid, "from": sender.lstrip("+"), "type": "text", "text": {"body": text}}


@override_settings(ENABLE_WHATSAPP_MARK_AS_READ=False, USE_MOCK_WHATSAPP=True)
class _WebhookSentryTestCase(TransactionTestCase):
    traces_sample_rate = None

    def setUp(self):
        self.transport = _start_sentry(
            self, integrations=[DjangoIntegration()], traces_sample_rate=self.traces_sample_rate,
        )
        # The real parse path saves through sync_to_async, whose worker thread keeps
        # its own connection; only that thread can close it (unit_tests/async_helpers.py)
        self.addCleanup(lambda: asyncio.run(sync_to_async(connections.close_all)()))

    def _make_profile(self, phone=PHONE):
        company = Company.objects.create(name=SHOP_NAME, slug=f"sentry-{uuid.uuid4().hex[:8]}", active=True)
        user = User.objects.create_user(username=phone, password="x")
        return UserProfile.objects.create(
            user=user, company=company, phone_number=phone, role=UserProfile.Role.OWNER, language="en",
        )

    def _post(self, payload):
        with patch("apps.whatsapp.views.WhatsAppWebhookView._verify_signature", return_value=True):
            Client(raise_request_exception=False).post(
                "/webhook/whatsapp/", json.dumps(payload), content_type="application/json",
            )
        sentry_sdk.flush()
        self.assertEqual(len(self.transport.events), 1)
        return self.transport.events[0]

    def assertNoPersonalData(self, event):
        serialised = json.dumps(event, default=str)
        for value in (PHONE, PHONE[1:], WAMID, base64.b64encode(PHONE[1:].encode()).decode(),
                      SHOP_NAME, CONTACT_NAME, TEXT, NOTES):
            self.assertNotIn(value, serialised)


class SentryContextTest(_WebhookSentryTestCase):
    def test_known_user_event_carries_ids_and_no_personal_data(self):
        """The real parse path: the parser logs the message and the LLM response
        at INFO, and the handler sets an llm_parse context. Sending the reply
        fails, which the handler logs as an error event."""
        profile = self._make_profile()

        with (
            patch("apps.whatsapp.services.message_parser.OpenRouterClient", _FakeOpenRouter),
            patch("apps.whatsapp.services.webhook_handler._send_response", AsyncMock(side_effect=RuntimeError("send failed"))),
        ):
            event = self._post(_payload(_text_message()))

        self.assertNoPersonalData(event)
        inbound = WhatsAppMessage.objects.get(whatsapp_message_id=WAMID)
        self.assertEqual(event["user"], {"id": profile.pk})
        self.assertEqual(str(event["tags"]["company_id"]), str(profile.company_id))
        self.assertEqual(event["tags"]["sender_status"], "known_user")
        self.assertEqual(str(event["tags"]["inbound_message_id"]), str(inbound.pk))
        self.assertEqual(event["tags"]["intent"], "other")
        self.assertNotIn("waitlist_entry_id", event["tags"])
        self.assertEqual(event["contexts"]["llm_parse"], {"intent": "other", "confidence": 0.9})
        log_crumbs = [c for c in event["breadcrumbs"]["values"] if c.get("type") == "log" or c.get("category", "").startswith("apps")]
        self.assertEqual(log_crumbs, [])

    def test_button_response_event_carries_ids_and_no_personal_data(self):
        profile = self._make_profile()
        button = {
            "id": WAMID, "from": PHONE[1:], "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "confirm_1", "title": TEXT}},
        }

        with patch("apps.whatsapp.views.handle_sale_button_action", side_effect=RuntimeError("boom")):
            event = self._post(_payload(button))

        self.assertNoPersonalData(event)
        self.assertEqual(event["user"], {"id": profile.pk})
        self.assertEqual(str(event["tags"]["company_id"]), str(profile.company_id))
        # A crash in a handler still counts as unhandled, for alerts and filters
        self.assertEqual(event["exception"]["values"][-1]["mechanism"], {"type": "django", "handled": False})

    def test_waitlisted_sender_event_carries_waitlist_entry_id(self):
        entry = WaitlistEntry.objects.create(phone_number=PHONE, company_name=SHOP_NAME, first_message=TEXT)

        with patch("apps.whatsapp.views.handle_waitlisted_message", side_effect=RuntimeError("boom")):
            event = self._post(_payload(_text_message()))

        self.assertNoPersonalData(event)
        self.assertNotIn("user", event)
        self.assertEqual(event["tags"]["sender_status"], "waitlisted")
        self.assertEqual(str(event["tags"]["waitlist_entry_id"]), str(entry.pk))

    def test_second_message_in_one_webhook_does_not_keep_first_senders_ids(self):
        """Two messages in one webhook. The first is a known user's, parsed for
        real; the second sender is unknown. The second event must not carry the
        first sender's IDs, intent or llm_parse context."""
        self._make_profile()
        other_phone = "+263772000111"

        with (
            patch("apps.whatsapp.services.message_parser.OpenRouterClient", _FakeOpenRouter),
            patch("apps.whatsapp.services.webhook_handler._send_response", AsyncMock(return_value=True)),
            patch("apps.whatsapp.views.handle_new_waitlist_entry", side_effect=RuntimeError("boom")),
        ):
            event = self._post(_payload(
                _text_message(),
                _text_message(sender=other_phone, wamid="wamid.SECOND", text="hello"),
            ))

        self.assertNoPersonalData(event)
        self.assertNotIn(other_phone[1:], json.dumps(event, default=str))
        self.assertNotIn("user", event)
        self.assertNotIn("company_id", event["tags"])
        self.assertNotIn("intent", event["tags"])
        self.assertNotIn("llm_parse", event.get("contexts", {}))
        self.assertEqual(event["tags"]["sender_status"], "unknown")
        second = WhatsAppMessage.objects.get(whatsapp_message_id="wamid.SECOND")
        self.assertEqual(str(event["tags"]["inbound_message_id"]), str(second.pk))

    def test_failure_before_second_senders_lookup_does_not_carry_first_senders_ids(self):
        """If the second message fails before its sender is looked up (a DB error
        in the lookup, say), the event must not be pinned on the first sender."""
        self._make_profile()
        from apps.whatsapp.views import _lookup_sender
        lookups = [_lookup_sender, RuntimeError("db down")]

        def lookup(sender):
            step = lookups.pop(0)
            if isinstance(step, Exception):
                raise step
            return step(sender)

        with (
            patch("apps.whatsapp.views.handle_incoming_message"),
            patch("apps.whatsapp.views._lookup_sender", side_effect=lookup),
        ):
            event = self._post(_payload(
                _text_message(),
                _text_message(sender="+263772000111", wamid="wamid.SECOND", text="hello"),
            ))

        self.assertNoPersonalData(event)
        self.assertNotIn("user", event)
        for tag in ("company_id", "sender_status", "inbound_message_id"):
            self.assertNotIn(tag, event.get("tags", {}))

    def test_media_failure_before_message_is_saved_carries_sender_ids(self):
        """The media download and R2 upload run before the inbound row is saved,
        so their failures must still say whose message it was."""
        profile = self._make_profile()
        image = {"id": WAMID, "from": PHONE[1:], "type": "image", "image": {"id": "1561234567890123", "caption": TEXT}}

        with patch(
            "apps.whatsapp.views.WhatsAppWebhookView._download_and_upload_media",
            side_effect=RuntimeError("download failed"),
        ):
            event = self._post(_payload(image))

        self.assertNoPersonalData(event)
        self.assertEqual(event["user"], {"id": profile.pk})
        self.assertEqual(event["tags"]["sender_status"], "known_user")
        self.assertNotIn("inbound_message_id", event["tags"])


class WebhookTransactionTest(_WebhookSentryTestCase):
    """About 1 in 10 requests sends a performance transaction, error or not.
    Contexts and tags go on it too, and before_send doesn't run."""

    traces_sample_rate = 1.0

    def test_successful_message_transaction_carries_no_message_text_or_notes(self):
        self._make_profile()

        with (
            patch("apps.whatsapp.services.message_parser.OpenRouterClient", _FakeOpenRouter),
            patch("apps.whatsapp.services.webhook_handler._send_response", AsyncMock(return_value=True)),
            patch("apps.whatsapp.views.WhatsAppWebhookView._verify_signature", return_value=True),
            # gunicorn's WSGI handler starts this transaction in production; the test client doesn't
            sentry_sdk.start_transaction(name="/webhook/whatsapp/", op="http.server"),
        ):
            Client().post("/webhook/whatsapp/", json.dumps(_payload(_text_message())), content_type="application/json")
        sentry_sdk.flush()

        self.assertEqual(self.transport.events, [])
        self.assertEqual(len(self.transport.transactions), 1)
        self.assertNoPersonalData(self.transport.transactions[0])


class RedactPersonalDataTest(TestCase):
    def test_redacts_phones_and_wamids_in_text_parts(self):
        event = {
            "message": f"Send to {PHONE} failed",
            "exception": {"values": [{"type": "ValueError", "value": f"bad number {PHONE[1:]}"}]},
            "contexts": {"llm_parse": {"message_id": WAMID}},
        }

        redact_personal_data(event, {})

        self.assertEqual(event["message"], "Send to [phone] failed")
        self.assertEqual(event["exception"]["values"][0]["value"], "bad number [phone]")
        self.assertEqual(event["contexts"]["llm_parse"]["message_id"], "[wamid]")

    def test_leaves_prices_ids_and_trace_ids_alone(self):
        trace_id = "a1234567890123456789012345678901"
        event = {
            "message": "Sale 4512 for company 17: 5 bread at 1.50, total 7.50",
            "contexts": {"trace": {"trace_id": trace_id}},
        }

        redact_personal_data(event, {})

        self.assertEqual(event["message"], "Sale 4512 for company 17: 5 bread at 1.50, total 7.50")
        self.assertEqual(event["contexts"]["trace"]["trace_id"], trace_id)

    def test_leaves_longer_digit_runs_alone(self):
        event = {"message": "Downloaded media 1561234567890123, size=2048"}

        redact_personal_data(event, {})

        self.assertEqual(event["message"], "Downloaded media 1561234567890123, size=2048")


class TransactionRedactionTest(TestCase):
    """Performance transactions skip before_send. The boto3 integration records
    the R2 URL on its span whatever send_default_pii says, and media keys are
    YYYY/MM/DD/<phone>/<media_id> (services/storage/r2_client.py)."""

    def test_span_url_with_phone_is_redacted(self):
        transport = _start_sentry(self, traces_sample_rate=1.0)
        r2_url = f"https://r2.example.com/media/2026/10/15/{PHONE[1:]}/media1.ogg"

        with sentry_sdk.start_transaction(name="/webhook/whatsapp/", op="http.server"):
            with sentry_sdk.start_span(op="http.client", name=f"PUT {r2_url}") as span:
                span.set_data("aws.request.url", r2_url)
        sentry_sdk.flush()

        self.assertEqual(len(transport.transactions), 1)
        serialised = json.dumps(transport.transactions[0], default=str)
        self.assertNotIn(PHONE[1:], serialised)
        span = transport.transactions[0]["spans"][0]
        self.assertEqual(span["data"]["aws.request.url"], "https://r2.example.com/media/2026/10/15/[phone]/media1.ogg")

    def test_leaves_span_ids_and_other_data_alone(self):
        event = {"spans": [{"span_id": "a123456789012345", "description": "SELECT 1", "data": {"thread.id": "140123456789"}}]}

        redact_transaction(event, {})

        self.assertEqual(event["spans"][0]["span_id"], "a123456789012345")
        self.assertEqual(event["spans"][0]["data"]["thread.id"], "140123456789")


class SettingsUsePrivacyOptionsTest(TestCase):
    def test_base_settings_start_sentry_with_privacy_options(self):
        """The tests above pass PRIVACY_OPTIONS themselves, so this checks that
        the real settings do too."""
        import importlib

        import config.settings.base as base_settings

        with patch("sentry_sdk.init") as init:
            importlib.reload(base_settings)

        for key, value in PRIVACY_OPTIONS.items():
            self.assertEqual(init.call_args.kwargs[key], value)
        logging_integrations = [i for i in init.call_args.kwargs["integrations"] if isinstance(i, LoggingIntegration)]
        self.assertEqual(len(logging_integrations), 1)
        self.assertIsNone(logging_integrations[0]._breadcrumb_handler)
