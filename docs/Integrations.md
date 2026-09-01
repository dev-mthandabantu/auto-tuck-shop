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

Production and staging databases managed by Fly.io. Accessed via psycopg3 through `DATABASE_URL`.

Migrations are immutable once committed — never edit or delete an existing migration file. Always create a new one.

---

## Sentry

Optional error tracking. Initialised only if `SENTRY_DSN` is set. Tagged with `SENTRY_ENVIRONMENT` (`production` or `staging`).

**User context:** Every webhook request sets `sentry_sdk.set_user` with the sender’s phone number. Once the profile resolves, it is enriched with the shop name — so issues show “Sweetwaters” instead of an IP.

**Release tracking:** `deploy-production.yml` creates a Sentry release after every prod deploy (using `SENTRY_AUTH_TOKEN` secret). Issues auto-resolve when they stop appearing in a new release, and each issue shows which version introduced it.

**Cron monitors:** `send_daily_reports`, `send_nudges`, `auto_cancel_stale_sales`, and `pilot_activity_alert` are wrapped with `sentry_sdk.monitor`. These appear at `aakitech.sentry.io/crons/` as `send-daily-reports`, `send-nudges`, `auto-cancel-stale-sales`, and `pilot-activity-alert`. All alert if a run is missed or takes longer than 5 minutes.

**Pilot activity alert (#273):** `pilot_activity_alert` runs daily and checks whether any real (non-test) pilot shop has recorded a confirmed sale in the last 7 days. If not, it fires a Sentry error-level `capture_message` tagged `failure_type: pilot_activity_silence` (so it shows up as an issue, not just a missed cron) and WhatsApps `ADMIN_PHONE_NUMBER` directly — this is a business-silence signal, not a crash, so it can't rely on the cron-monitor heartbeat alone.

**Required secrets:** `SENTRY_DSN`, `SENTRY_ENVIRONMENT` (set to `production` on prod), `SENTRY_AUTH_TOKEN` (GitHub secret, used by deploy workflow only).

Key signals to watch:
- `ElevenLabsError` — voice transcription failures (often quota or key issues)
- `OpenRouterError` — LLM failures
- `botocore.errorfactory.AccessDenied` — R2 permission issues (wrong token type or wrong Cloudflare account)
- `FieldError` / `AttributeError` — model/query mismatches after migrations
- Cron monitors going red — daily reports or nudges not firing
- `failure_type: report_card_generation` — report card image failed to generate or upload; falls back to text-only, so this is the only signal that visual reports are silently degrading. Tagged with `company_id` and `source` (`send_daily_summary` for the scheduled daily report, `sales_query_on_demand` for the on-demand "send daily summary" chat command) — filter/alert on `source` to distinguish the two.
- `failure_type: pilot_activity_silence` — zero real pilot shops recorded a sale in the last 7 days (see #273). Tagged with `window_days`; also WhatsApps the admin directly, so this Sentry issue is a secondary/durable record rather than the primary alert channel.
