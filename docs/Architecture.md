# Architecture

## Django Apps

| App | Purpose |
|-----|---------|
| `apps/core` | Company, UserProfile, WaitlistEntry models |
| `apps/sales` | Sale, SaleItem, Product models and sale creation service |
| `apps/whatsapp` | Webhook views, message handlers, report card, locales |
| `services/openrouter` | LLM client (Gemini 2.5 Flash via OpenRouter) |
| `services/storage` | Cloudflare R2 upload client |
| `utils/` | Timing helpers |

## Key Models

### Company
- `name`, `phone_number`, `currency` (default currency)
- `normal_closing_time` (time) — permanent daily schedule; summary fires 1 hour after this
- `daily_closing_time` (time), `daily_closing_date` (date) — today's override from owner reply
- `last_closing_prompt_date` (date) — when the 5pm prompt was last sent
- `last_summary_date` (date) — prevents duplicate summaries

### UserProfile
- `user` (FK to Django User), `phone_number`, `language` (en/sn), `role` (owner/assistant)
- `company` (FK to Company)

### Sale
- `company`, `user_profile`, `message_id`, `status` (pending/confirmed/cancelled)
- `total_amount`, `currency`, `sale_timestamp`
- `was_bot_mistake` (bool) — set when owner presses "Fix mistake"

### SaleItem
- `sale`, `product` (FK), `quantity`, `unit_price`, `total_price`, `currency`

### WaitlistEntry
- `phone_number`, `shop_name`, `language`, `status` (pending/approved/rejected)

## URL Structure

```
/webhook/whatsapp/          POST  inbound WhatsApp messages (webhook_view)
/webhook/whatsapp/          GET   webhook verification (Meta challenge)
/healthz/                   GET   health check (200 OK)
/admin/                           Django admin
/mock-whatsapp/                   Mock WhatsApp UI (staging only)
/mock-whatsapp/send/        POST  send test message via mock
/mock-media/<media_id>/     GET   mock audio endpoint (staging only)
```

## Message Processing Pipeline

```
Meta webhook POST
    |
    v
views.py (HMAC verification, dedup by message_id)
    |
    +-- audio message --> media_handler.py
    |       |
    |       v
    |   ElevenLabs transcription
    |       |
    |       v (transcribed text, same path as below)
    |
    +-- text/button message
            |
            v
    webhook_handler.py: handle_incoming_message()
            |
            v
    _process_message_async()
            |
            +-- button action? --> sale_handler / waitlist_handler
            |
            +-- company has normal_closing_time + keywords "every day"?
            |       --> set_company_normal_closing_time(), confirm, return
            |
            +-- owner, no normal_closing_time, short message?
            |       --> try parse_closing_time_text()
            |       --> if time found: set_company_normal_closing_time(), confirm, return
            |
            +-- closing prompt sent today, no time set yet?
            |       --> try parse_closing_time_text()
            |       --> if time found: set_company_daily_closing_time(), ack, return
            |       --> if short + digits but unparseable: invalid_time hint, return
            |
            v
    parse_message_unified() [OpenRouter LLM call]
            |
            +-- intent: add_assistant --> waitlist_handler.handle_add_assistant()
            +-- intent: sales_query   --> _handle_sales_query()
            +-- intent: sale / other  --> sale_handler.process_sale_message_unified()
```

Pre-LLM intercepts avoid unnecessary API calls for simple time replies.

## Sale Confirmation Flow

```
Owner sends sale message
    |
    v
LLM extracts items (product, qty, unit_price, currency)
    |
    v
Post-LLM rule pass: if no per-unit marker and qty > 1, clear unit_price
    |
    v
Sale created with status=pending
    |
    v
Bot sends confirmation summary + buttons ("Looks good" / "Fix mistake")
    |
    +-- "Looks good" -> sale.status = confirmed
    +-- "Fix mistake" -> sale.status = cancelled, was_bot_mistake = True
```

## Daily Reports Flow

```
Fly.io cron (every 15 min): python manage.py send_daily_reports
    |
    v
maybe_send_daily_notifications() iterates active companies
    |
    +-- company.normal_closing_time set?
    |       YES: send summary 1 hour after normal_closing_time (no daily prompt)
    |
    +-- no normal_closing_time:
    |       +-- current_time >= 17:00 and prompt not sent today?
    |       |       --> send closing time prompt ("What time are you closing today?")
    |       |
    |       +-- closing time was set today and current_time >= set_time + 1hr?
    |       |       --> send daily summary
    |       |
    |       +-- prompt sent today but no reply and current_time >= 21:00?
    |               --> send summary anyway (9pm fallback)
```

Closing time replies are intercepted before the LLM in `_process_message_async`.

## Onboarding Flow

```
Unknown number messages bot
    |
    v
WaitlistEntry created, language selection buttons sent (English / Shona)
    |
    v
Owner selects language, sends shop name
    |
    v
Admin notified via WhatsApp with Approve/Reject buttons
    |
    +-- Reject: owner notified
    |
    +-- Approve:
            Company + User + UserProfile created
            |
            v
        Welcome message sent
            |
            v
        Closing time setup prompt sent
        ("What time does your shop usually close?")
            |
            v
        Owner replies with time -> normal_closing_time set permanently
        (intercepted pre-LLM; short message + parseable time)
```

## Pilot Operations Visibility

`CompanyAdmin` (`apps/core/admin.py`) surfaces per-shop pilot health directly in Django admin
(`/admin/core/company/`) — no SSH/DB access needed for day-to-day checks:

- **Last confirmed sale** / **last inbound message** — computed via `Subquery` annotations in
  `get_queryset`, not stored fields. Only `Sale.Status.CONFIRMED` sales count (sales start
  `PENDING` and only become real activity once confirmed). A `PENDING` sale the owner never
  confirms or rejects is auto-cancelled after 24h (`auto_cancel_stale_sales` management command,
  `apps/sales/services.py::auto_cancel_stale_pending_sales`) — fast-follow to #298, which made
  `PENDING` sales safe to have around but didn't add anything to clean them up once stale.
- **Nudge status** — reads `Company.nudge_stage` / `last_nudge_date` / `onboarding_followup_sent` /
  `zero_contact_followup_count` directly. These only advance on a *confirmed* WhatsApp send success
  (see `nudge_service.py`), so a stalled value here means either the shop isn't yet eligible or the
  last attempted automated send failed.
- **"Silent N+ days" flag/filter** (`NeedsAttentionFilter`, `NEEDS_ATTENTION_DAYS = 7`) — flags
  active, already-onboarded shops with no confirmed sale *and* no inbound message in the window,
  excluding known non-pilot numbers (`apps/core/pilot_test_phones.py`, shared with the
  `pilot_audit` management command so the exclusion list can't drift between the two).

`WaitlistEntryAdmin` (`apps/core/admin.py`, `/admin/core/waitlistentry/`) has the same kind of
backstop for a different failure mode:

- **"Stale N+ days" flag/filter** (`StalePendingFilter`, `STALE_WAITLIST_DAYS = 3`) — flags any
  `WaitlistEntry` still `PENDING` after the window. Backstop for #339: the admin notification for
  a new signup (`_send_waitlist_admin_notification`) can fail even after its own template fallback
  (network error, WhatsApp API error), leaving only a Sentry log to show for it. This flag surfaces
  the stuck entry itself even if that log is never noticed.

This is passive visibility only — it doesn't page or notify anyone. Proactive alerting when the
whole pilot goes quiet is a separate, independent mechanism (see #273).

`pilot_audit` (`apps/whatsapp/management/commands/pilot_audit.py`) separately flags two distinct
per-shop failure modes in its "Issues" output, run manually or via `fly ssh console` (see
`AGENTS.md`):

- **SILENT** — approved, zero confirmed sales, never messaged. Never had traction.
- **CHURNED** (#210) — had real traction (`CHURN_MIN_SALES = 2`+ confirmed sales) and then went
  quiet: no inbound message *and* no confirmed sale for more than `CHURN_SILENCE_DAYS = 5` days
  (whichever of the two is more recent counts as "last active"; a shop silent for exactly 5 days
  is not yet flagged — the comparison is strict). Deliberately shorter than
  `NEEDS_ATTENTION_DAYS`/`PILOT_SILENCE_WINDOW_DAYS` (7 days) — a shop that already proved it can
  sell going quiet is a higher-signal event than a shop that was never active, worth a personal
  check-in sooner. `CHURN_SILENCE_DAYS` and `CHURN_MIN_SALES` are co-located in
  `apps/core/services.py` alongside `PILOT_SILENCE_WINDOW_DAYS` for discoverability, not because
  it prevents drift — `NEEDS_ATTENTION_DAYS` (the admin's own "gone quiet" threshold) lives
  separately in `apps/core/admin.py` and is a different value (7 days) already. `pilot_audit`
  computes CHURNED directly in a Python loop; `CompanyAdmin` doesn't (yet) surface CHURNED itself
  (its Subquery-annotation approach is a different shape, kept out of scope here).

SILENT and CHURNED are mutually exclusive by construction (SILENT requires zero sales, CHURNED
requires `CHURN_MIN_SALES`+). A shop with exactly one confirmed sale that goes quiet trips neither
flag — it can still surface via NO SUMMARY / NO CLOSE TIME, so it isn't invisible to the audit,
but it is invisible to churn detection specifically. That's the deliberate `CHURN_MIN_SALES`
tradeoff; worth watching, since a 1-sale shop going quiet is arguably the most fragile pilot state.

CHURNED reads inbound messages and confirmed sales, not outbound nudges, so a flag firing now
during the outbound-nudge pause (#278) is a real signal about shop activity, not an artifact of
the pause — #278 stopped outbound only.

One divergence from `NeedsAttentionFilter` worth knowing: that filter treats a shop with no
activity timestamp at all (`NULL`) as stale (`Q(__isnull=True) | Q(__lt=cutoff)`), while CHURNED
requires `last_active_at is not None` and treats a shop with no timestamp as not-churned. In
practice this doesn't matter for CHURNED specifically — `CHURN_MIN_SALES = 2`+ confirmed sales
guarantees `last_active_at` is never `None` (a confirmed sale always sets it) — but it's a
deliberate difference in how the two functions treat the edge case, not an oversight.

## Async Execution Model

Gunicorn workers are synchronous. `handle_incoming_message()` calls `run_async()` which:
- Detects if a running event loop exists (e.g. in tests) and submits to a ThreadPoolExecutor
- Otherwise calls `asyncio.run()` directly

`db_sync_to_async` wraps DB calls with `close_old_connections()` to avoid stale connection errors across threads.

## Retry and Resilience

- **OpenRouter**: 3 attempts, exponential backoff (1s, 2s, 4s)
- **DB connection errors**: caught in `handle_incoming_message`, connections closed, single retry
- **R2 upload failures**: non-blocking (fire and forget), errors logged to Sentry
- **ElevenLabs failures**: graceful error message sent to owner in their language
- **Daily summary failures**: logged, company skipped, other companies continue

## Localisation

All user-facing strings are in `apps/whatsapp/locales/en.json` and `sn.json`. Accessed via:

```python
t("key.subkey", lang=lang, **format_args)
```

`lang` is stored per `UserProfile`. Default is `"sn"` (Shona).

## Per-User Message Serialisation

`apps/whatsapp/services/message_lock.py` provides a `threading.Lock` per phone number, held in `handle_incoming_message` / `handle_incoming_audio_message` for the full duration of `run_async()`. This ensures concurrent webhooks from the same sender are processed one at a time — preventing duplicate sales and closing-time race conditions from rapid messages.

**Limitation: single-machine only.** `threading.Lock` is in-process. If Fly.io scales to multiple app machines, messages from the same user could land on different machines and the lock would not be shared. At pilot scale (1 active app machine), this is fine.

**Multi-machine path if needed:** replace with a Redis distributed lock (e.g. `redis-py` with `SET NX PX`) keyed on phone number. No code changes needed elsewhere — only `message_lock.py` changes.

**The DB unique constraint on `whatsapp_message_id` is the cross-machine safety net.** It prevents duplicate records from Meta webhook retries regardless of machine count. The threading lock handles rapid in-session messages; the DB constraint handles network-level retries.
