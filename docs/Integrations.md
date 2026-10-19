# Integrations

## Meta WhatsApp Business API

Handles all inbound and outbound WhatsApp messages.

**Inbound:** Meta sends a POST to `/webhook/whatsapp/` for each message. The payload is verified using HMAC-SHA256 against `META_WHATSAPP_APP_SECRET`. Message types handled: text, audio, button replies.

**Outbound:** Text messages, messages with interactive buttons (2-3 buttons), and image messages with captions (for stat cards).

**Required env vars:** `META_WHATSAPP_ACCESS_TOKEN`, `META_WHATSAPP_PHONE_NUMBER_ID`, `META_WHATSAPP_APP_SECRET`, `META_WHATSAPP_VERIFY_TOKEN`

**Mock client:** Set `USE_MOCK_WHATSAPP=True` on staging. All outbound messages are stored in memory and visible in the mock UI. Must be `False` (or unset) on production.

---

## OpenRouter — Gemini 2.5 Flash

Unified intent detection and data extraction in a single LLM call.

**What it does:** Classifies messages as `sale`, `sales_query`, `add_assistant`, or `other`, and extracts structured data (items, quantities, prices, currencies, phone numbers, timeframes).

**Shona support:** The system prompt includes Shona vocabulary, per-unit markers (imwe, rimwe, humwe, umwe, etc.), number words, and example messages with translations.

**Post-LLM rule pass:** A regex-based check corrects per-unit vs total-price ambiguity after the LLM response. If no per-unit marker is present and quantity > 1, `unit_price` is cleared.

**Pre-LLM intercepts:** Closing time replies are handled before the LLM call (regex-based), avoiding unnecessary API calls for simple time messages.

**Retry logic:** 3 attempts with exponential backoff (1s, 2s, 4s) on transient errors.

**Required env vars:** `OPENROUTER_API_KEY`

---

## ElevenLabs — Voice Transcription

Transcribes audio messages to text using the `scribe_v1` model.

**Flow:** Audio bytes are sent directly to ElevenLabs with a filename hint (e.g. `audio.ogg`). The transcribed text is then processed identically to a typed message.

**Supported formats:** OGG, MP3, M4A, AAC, AMR (WhatsApp audio formats).

**Error handling:** If `ELEVENLABS_API_KEY` is not set or the quota is exhausted, the bot sends a localised error message. Errors are logged to Sentry with full context.

**Required env vars:** `ELEVENLABS_API_KEY`

---

## Cloudflare R2

S3-compatible object storage used for two purposes:

**1. Audio archival:** Voice message audio is uploaded in the background (non-blocking) after transcription. Keyed as `media/{phone_number}/{media_id}.{ext}`.

**2. Daily stat card images:** Report card PNGs are uploaded and served via public URL, then sent as WhatsApp image messages. Keyed as `reports/{company_id}/{YYYY-MM-DD}.png`. Re-runs overwrite the same key rather than accumulating files.

**Public URL:** If `R2_PUBLIC_URL` is set, URLs are formed as `{R2_PUBLIC_URL}/{key}`. Otherwise falls back to `{R2_ENDPOINT_URL}/{bucket}/{key}`.

**Required env vars:** `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT_URL`, `R2_BUCKET_NAME`, `R2_PUBLIC_URL` (optional)

**Buckets:** `auto-tuck-shop-production` (prod), `auto-tuck-shop-staging` (staging)

> **Note:** The R2 API token must be created at Cloudflare Dashboard -> R2 -> **Manage R2 API Tokens** (not Account API Tokens). It requires Object Read & Write permissions. Account-scoped tokens will fail with `AccessDenied` on `PutObject`.
>
> The token must also belong to the **same Cloudflare account** as the `R2_ENDPOINT_URL`. Using a token from a different account will also cause `AccessDenied`.

---

## Fly.io

Hosts both production and staging apps on shared machines.

**Process groups:**
- `app` — Gunicorn web server
- `cron` — scheduled machine running `python manage.py send_daily_reports` every 15 minutes

**Cron schedule:** `*/15 * * * *` — checks all active companies and sends closing prompts or summaries as due.

**Release command:** `python manage.py migrate --noinput` runs automatically on every deploy.

**Configs:** `fly.toml` (production), `fly.staging.toml` (staging)

**Required env vars:** `SECRET_KEY`, `DATABASE_URL`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DJANGO_SETTINGS_MODULE`

---

## PostgreSQL

Hosted on **Supabase**, not on Fly: production in `eu-west-1` (Ireland) through the transaction pooler (port 6543), staging in `eu-north-1` (Stockholm) through the session pooler (port 5432). Accessed via psycopg3 through `DATABASE_URL`, a Fly secret on each app.

The Fly apps run in `iad` (US East), so every query crosses the Atlantic. Keep that in mind when choosing where ATS runs.

Migrations are immutable once committed — never edit or delete an existing migration file. Always create a new one.

---

## Sentry

Optional error tracking. Initialised only if `SENTRY_DSN` is set. Tagged with `SENTRY_ENVIRONMENT` (`production` or `staging`).

**Sentry context (#437):** an event may carry internal IDs only. Phone numbers, shop names, WhatsApp profile names and message text stay out. The IDs are pseudonymous, not anonymous: anyone with Django admin or database access can open the record behind them, which is how you debug. Sentry access alone is no longer enough to identify a pilot shop. This applies to every place the WhatsApp views set context (text, button, audio and media handlers), and to any new Sentry call elsewhere in the app.

What the webhook sets, through `_set_sentry_context` in `apps/whatsapp/views.py`, as soon as the sender is looked up (so a failed media download still says whose message it was), then again once the inbound message is saved:

| Field | Value | Look it up at |
|---|---|---|
| `user.id` | `UserProfile.pk` (known senders only) | `/admin/core/userprofile/<id>/change/` |
| tag `company_id` | `profile.company_id` | `/admin/core/company/<id>/change/` |
| tag `waitlist_entry_id` | `WaitlistEntry.pk` (waitlisted senders) | `/admin/core/waitlistentry/<id>/change/` |
| tag `inbound_message_id` | the inbound `WhatsAppMessage.pk` (every sender) | `/admin/whatsapp/whatsappmessage/<id>/change/` |
| tag `sender_status` | `known_user`, `waitlisted` or `unknown` | |

Each message in a webhook is handled in its own Sentry isolation scope, so one sender's IDs, context and breadcrumbs can't reach the next message's event. An exception is captured inside that scope, while the IDs are still on it, then re-raised. The capture is marked unhandled, the same way Django's integration marks it, so unhandled-error alerts and filters still see webhook crashes; Django's own capture of the same exception is then dropped by Sentry's default dedupe. Performance transactions belong to the whole request, not to one message, so they carry none of these IDs; their spans still nest under the request as before. Meta's message ID (`wamid`) is not used: it embeds the sender's number in base64. The `llm_parse` context set after parsing carries intent and confidence only, since the LLM's `notes` repeat the message.

Sentry also collects data on its own, so `config/sentry.py` sets `PRIVACY_OPTIONS` and `PRIVACY_INTEGRATIONS`, which `config/settings/base.py` passes to `sentry_sdk.init`:

- `LoggingIntegration(level=None)`. Log lines become breadcrumbs, and many carry message text or the LLM's full response (`message_parser.py` logs both at INFO). Log breadcrumbs are off; `logger.error` still creates events, and Fly logs keep the full trail. SQL and HTTP breadcrumbs stay.
- `include_local_variables=False`. Stack frames held `sender`, `body`, `profile` and the raw webhook payload in nearly every frame. Tracebacks no longer show variable values; debug from the IDs above.
- `max_request_body_size="never"`. The webhook body is the Meta payload (phone, profile name, message text). `send_default_pii` does not control this.
- `send_default_pii=False`. Drops cookies, IP and user headers. Requests come from Meta's servers, so nothing useful is lost.
- `before_send=redact_personal_data`. Error messages, tags and the remaining breadcrumbs can still include a number. This replaces phone numbers with `[phone]` and wamids with `[wamid]` in messages, breadcrumbs, tags, extra, contexts (except `trace`) and exception messages. It's a safety net for numbers written without spaces (`077 123 4567` gets through) and it leaves longer digit runs such as Meta media IDs alone. It can't spot a shop name or message text, so don't put those in an error message or a tag.
- `before_send_transaction=redact_transaction`. Performance transactions (`SENTRY_TRACES_SAMPLE_RATE`, 10% by default) skip `before_send`. Their spans record outbound URLs whatever `send_default_pii` says: the boto3 integration sets `aws.request.url` on every R2 call, and R2 media keys are `YYYY/MM/DD/<phone>/<media_id>`. This applies the same redaction plus span descriptions and URL and query fields.

Two things this doesn't use yet. `data_collection` is the SDK's newer per-category replacement for `send_default_pii`, but in sentry-sdk 2.66 it's still under `_experiments`. Sentry's server-side Advanced Data Scrubbing can run a regex over every string (source `$string`, spans included) and has no built-in phone rule; adding one in project settings would be a second layer behind these options.

These options don't replace checking the event. `unit_tests/test_sentry_context.py` sends real-shaped webhooks through the view with a fake Sentry transport and asserts the captured event holds the IDs and none of the personal data. Extend it when you add context.

**Release tracking:** `deploy-production.yml` creates a Sentry release after every prod deploy (using `SENTRY_AUTH_TOKEN` secret). Issues auto-resolve when they stop appearing in a new release, and each issue shows which version introduced it.

**Cron monitors:** `send_daily_reports`, `send_nudges`, `auto_cancel_stale_sales`, `pilot_activity_alert`, and `stale_waitlist_alert` are wrapped with `sentry_sdk.monitor`. These appear at `aakitech.sentry.io/crons/` as `send-daily-reports`, `send-nudges`, `auto-cancel-stale-sales`, `pilot-activity-alert`, and `stale-waitlist-alert`. All alert if a run is missed or takes longer than 5 minutes.

**Pilot activity alert (#273):** `pilot_activity_alert` runs daily and checks whether any real (non-test) pilot shop has recorded a confirmed sale in the last 7 days. If not, it fires a Sentry error-level `capture_message` tagged `failure_type: pilot_activity_silence` (so it shows up as an issue, not just a missed cron) and WhatsApps every admin in `ADMIN_PHONE_NUMBER` (comma-separated, #388) directly — this is a business-silence signal, not a crash, so it can't rely on the cron-monitor heartbeat alone.

**Stale waitlist alert (#387):** `stale_waitlist_alert` runs every 30 minutes and checks for PENDING `WaitlistEntry` rows where `admin_alerted_at` is still null after `NEVER_ALERTED_MINUTES` (30 min, `apps/core/services.py`). This catches two cases the signup-time notification in `waitlist_handler.py::_send_waitlist_admin_notification` misses: a shop owner who never provides a shop name (that function is only ever called once one exists, so no attempt is made — no exception, no Sentry event either), and the rarer case where both send attempts fail. Sentry fires an error-level `capture_message` tagged `failure_type: waitlist_admin_never_alerted` on every run where any such entry exists. The WhatsApp alert only goes to entries not yet pinged by this command (`stale_alert_sent_at`), so a stuck entry is messaged about once, not on every 30-minute run — Sentry stays the durable, every-run record. Separate from the existing `stale_pending_flag` / `StalePendingFilter` in the Django admin (`STALE_WAITLIST_DAYS`, 3 days) — that's a pull-only backstop for entries genuinely stuck in the approval queue; this is a push alert on the narrower question of whether the admin was ever told at all.

**Admin notification send order (#387):** `_send_waitlist_admin_notification` sends the `ats_admin_new_signup` template first, not as a fallback — interactive-button messages are free-form and only deliver within Meta's 24h customer-service window, which is closed by default for an admin who doesn't message the bot daily. Buttons are still attempted afterward regardless of the template's outcome, since they're a real improvement (inline Approve/Reject) when the window happens to be open, but the admin alert no longer depends on it. `admin_alerted_at` is set on whichever attempt succeeds. **Open:** the template send has produced zero `WhatsAppMessage` rows in production, ever — it's failing before the HTTP request, not being rejected by Meta. Root cause unconfirmed (needs Sentry access); see the `TODO(#387)` on `_send_admin_notification_template`'s `language_code` default.

**Required secrets:** `SENTRY_DSN`, `SENTRY_ENVIRONMENT` (set to `production` on prod), `SENTRY_AUTH_TOKEN` (GitHub secret, used by deploy workflow only).

Key signals to watch:
- `ElevenLabsError` — voice transcription failures (often quota or key issues)
- `OpenRouterError` — LLM failures
- `botocore.errorfactory.AccessDenied` — R2 permission issues (wrong token type or wrong Cloudflare account)
- `FieldError` / `AttributeError` — model/query mismatches after migrations
- Cron monitors going red — daily reports or nudges not firing
- `failure_type: report_card_generation` — report card image failed to generate or upload; falls back to text-only, so this is the only signal that visual reports are silently degrading. Tagged with `company_id` and `source` (`send_daily_summary` for the scheduled daily report, `sales_query_on_demand` for the on-demand "send daily summary" chat command) — filter/alert on `source` to distinguish the two.
- `failure_type: pilot_activity_silence` — zero real pilot shops recorded a sale in the last 7 days (see #273). Tagged with `window_days`; also WhatsApps the admin directly, so this Sentry issue is a secondary/durable record rather than the primary alert channel.
- `failure_type: waitlist_admin_never_alerted` — one or more pending waitlist entries never got a successful admin notification (see #387). Tagged with `window_minutes`; also WhatsApps the admin directly, same secondary/durable-record role as the pilot silence alert above.
