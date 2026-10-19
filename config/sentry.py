"""
What Sentry is allowed to carry (#437). The written decision is the
"Sentry context" section of docs/Integrations.md; keep the two in step.

Events carry internal IDs only (profile, company, waitlist entry, inbound
WhatsAppMessage row). Phone numbers, shop names, WhatsApp profile names and
message text stay out. These options close the routes Sentry fills on its own:

- LoggingIntegration(level=None): log lines become breadcrumbs, and many carry
  message text or the LLM's response. Breadcrumbs from logging are off;
  logger.error still creates events. Fly logs keep the full trail.
- include_local_variables: stack frames otherwise hold `sender`, `body`,
  `profile` and the raw webhook payload in nearly every frame.
- max_request_body_size: the webhook body is the Meta payload (phone, profile
  name, message text). send_default_pii does not control this.
- before_send: error messages, tags and other breadcrumbs can still include the
  sender. This redacts phone numbers and Meta message IDs (a wamid embeds the
  sender's number in base64) from the text parts of an event. A safety net for
  numbers written without spaces only.
- before_send_transaction: the same for performance transactions, plus span
  descriptions and URLs.
"""

import re

from sentry_sdk.integrations.logging import LoggingIntegration

# Not inside a longer digit run, so 16-digit Meta media IDs stay readable
PHONE_PATTERN = re.compile(r"(?<!\d)\+?\d{9,15}(?!\d)")
WAMID_PATTERN = re.compile(r"wamid\.[A-Za-z0-9+/=_-]+")

# Event sections holding free text: log messages, breadcrumbs, tags, extra.
# Stack frames and request data are already kept out by the options below.
_TEXT_SECTIONS = ("message", "logentry", "breadcrumbs", "tags", "extra")


def _redact(value):
    if isinstance(value, str):
        return PHONE_PATTERN.sub("[phone]", WAMID_PATTERN.sub("[wamid]", value))
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def redact_personal_data(event, hint):
    for section in _TEXT_SECTIONS:
        if section in event:
            event[section] = _redact(event[section])
    # Contexts are redacted one by one, skipping "trace": its hex IDs often
    # contain nine digits in a row and would be mangled.
    for name, context in event.get("contexts", {}).items():
        if name != "trace":
            event["contexts"][name] = _redact(context)
    for exception in event.get("exception", {}).get("values", []):
        if isinstance(exception.get("value"), str):
            exception["value"] = _redact(exception["value"])
    return event


def redact_transaction(event, hint):
    """Performance transactions skip before_send. Their spans record outbound
    URLs whatever send_default_pii says (boto3 sets aws.request.url), and R2
    media keys contain the sender's number."""
    event = redact_personal_data(event, hint)
    if isinstance(event.get("transaction"), str):
        event["transaction"] = _redact(event["transaction"])
    for span in event.get("spans", []):
        if isinstance(span.get("description"), str):
            span["description"] = _redact(span["description"])
        for key, value in span.get("data", {}).items():
            if "url" in key or "query" in key:
                span["data"][key] = _redact(value)
    return event


PRIVACY_INTEGRATIONS = [LoggingIntegration(level=None)]

PRIVACY_OPTIONS = {
    "send_default_pii": False,
    "include_local_variables": False,
    "max_request_body_size": "never",
    "before_send": redact_personal_data,
    "before_send_transaction": redact_transaction,
}
