# Features

## Sale Recording

Shop owners send a WhatsApp text or voice message describing what they sold. The LLM extracts product names, quantities, and prices from natural language — including Shona, English, and code-switching between both.

Examples:
- `2 Mazoe $3 each` -> 2 x Mazoe @ $3
- `5 chingwa R18 rimwe, 2 shuga R40 rimwe` -> 5 x bread @ R18, 2 x sugar @ R40
- `4munyu umwe R20` -> 4 x salt @ R20 (no space between qty and product name handled)
- `mazai maviri ne chips matatu` -> 2 x eggs, 3 x chips (Shona number words)

Supported currencies: USD ($), ZAR (R), ZWG / ZiG, BWP (P), EUR (euro), GBP (pound). Mixed currencies within one message are supported.

**Emoji product names:** the LLM has no canonicalization step of its own, so the same emoji could previously resolve to a different `product_name` string across calls (e.g. a raw 🚗 on one call, `"car"` on another) — breaking product identity for reporting and price history (#356). `apps/sales/services.py::canonicalize_product_name` runs after the LLM call, before the name ever reaches product matching: any product name composed entirely of emoji characters (after stripping a stray `"emoji"` word the LLM sometimes appends) is resolved to a stable label via the `emoji` package (e.g. 🚗 → `automobile`), so two sales of the same emoji always match the same `Product` row. A normal text name is left untouched.

## Sale Confirmation

After parsing, the bot sends a summary with two buttons:
- **Looks good** — confirms the sale, writes it to the database
- **Fix mistake** — cancels the sale, flags it as a bot mistake for metrics

Sales remain `pending` until confirmed. Cancellations are tracked separately from confirmed sales.

**Unmatched items:** if an item has no price and no stored catalog price to fall back on, it's excluded from the sale and the bot tells the owner what was understood and what to send next — e.g. "Got 3 x car, but I don't know the price. Reply with e.g. '3 car $5 each'" — using the shop's own currency in the example (#356).

## Voice Messages

Owners can send audio notes instead of typing. The system:
1. Downloads audio from Meta
2. Uploads to Cloudflare R2 in the background (permanent archive)
3. Transcribes via ElevenLabs (scribe_v1 model)
4. Processes the transcribed text identically to a typed message

Supported formats: OGG, MP3, M4A, AAC, AMR.

## Sale Recording from Images

Owners can send a photo (e.g. a tally sheet or receipt) instead of typing. Gemini vision extracts sale data from the image. Any caption sent alongside the image is included in the vision prompt, so sale details can come from the caption, the image, or both. If no readable sale data is found, the bot replies with a fallback prompting the owner to type or send a voice note instead.

**Implementation:** `apps/whatsapp/services/webhook_handler.py::_process_media_message_async` → `apps/whatsapp/services/message_parser.py::parse_image_for_sale` → `services/openrouter/prompts.py::build_image_parsing_prompt`.

## Language Support

English and Shona are fully supported. Language is selected at first message via buttons and stored per user. All bot replies, prompts, confirmations, and error messages respect the user's language preference.

Key Shona vocabulary handled by the parser:
- `imwe / rimwe / chimwe / humwe / mumwe / rumwe / kamwe / umwe` — "each" (per-unit price marker, varies by noun class)
- `imwe neimwe / rimwe nerimwe` — "each one"
- `hweiita / hwega` — "each/apiece"
- `maviri / matatu / mana / mashanu` — 2 / 3 / 4 / 5
- `ne` — "and" (joins items)
- `mazai` — eggs, `uswa` — mealie-meal, `munyu` — salt

## Daily Business Summary

Each day, every active shop receives an automatic end-of-day business summary.

**Setup (once, after first sale):** After a shop owner confirms their first sale, the bot asks "What time does your shop usually close?" The owner's reply is saved as their permanent closing time (`normal_closing_time`). The summary fires automatically 1 hour after that time every day — no further action needed.

**If no permanent time is set:** The bot sends a closing time prompt at 5pm each day. If the owner replies (e.g. "6pm"), the summary fires after that time. If there is no reply by 9pm, the summary fires anyway (9pm fallback).

**Changing the permanent time:** Send a message containing "every day", "daily", "mazuva ose", etc. with a time (e.g. "I close at 7pm every day"). The bot updates the permanent time and confirms.

**Summary contents (stat card image + text):**
- Today's revenue and sales count
- Delta vs yesterday (hidden if yesterday had no sales)
- "Best day this week" badge (when applicable)
- Top sellers (minimum 3 units sold to qualify)
- Weekly bar chart (Mon-today) with revenue per day and value labels

## On-Demand Sales Queries

Owners can ask for their sales data at any time:
- "How much did I make today?" -> today's stat card
- "How much did I make yesterday?" -> yesterday's stat card
- "How much did I make this week?" -> weekly text aggregate (Mon-today)
- "How much did I make this month?" -> monthly text aggregate
- Works in Shona too

## Team / Assistants

Shop owners can add assistants by sending: `add assistant +2637812345678`

Assistants can record sales but cannot add other team members. All active members of a company receive the daily summary.

## Help Menu

Owners can ask what the bot can do at any time. Two ways in:
- **Fast-path trigger words:** `help`, `options`, `menu` (EN) / `rubatsiro`, `menu` (SN) — matched as the whole message, case-insensitive, and answered before the LLM is called.
- **Free-form questions:** anything the LLM classifies as `other_sub_intent: "question"` (e.g. "how do I delete a sale?") routes to the same reply, since help-seeking phrasing is too varied for a fixed word list alone.

The reply is plain text (not interactive buttons) listing: record a sale, check sales, add/remove an assistant, switch language, and contact support. The support number comes from the `SUPPORT_PHONE_NUMBER` env var, not hardcoded.

## Greetings

A bare greeting or filler message ("hi", "thanks", "ok", a thumbs-up emoji) always gets a short reply (`other.greeting_acknowledged` locale key) confirming the bot is working and inviting the owner to send a sale — see #382. Originally (#284) these were fully silent for every sender, reasoned as avoiding reply-noise for owners already using the bot daily; #382 reversed that, since silence is indistinguishable from a broken bot from the shop owner's side, and judged that tradeoff worse than a short repeated acknowledgement. A greeting the LLM mis-labels (real text that isn't a recognised filler word/emoji) still falls through to `other.deferral` instead, per the code-level backstop from #284 — see `_is_silent_greeting` in `webhook_handler.py`.

A brand-new, not-yet-approved number is unaffected by any of this — that flow is handled separately by `handle_new_waitlist_entry`, which already replies with language selection regardless.

**Implementation:** `apps/whatsapp/services/webhook_handler.py::_process_message_async`, locale key `help.menu` in `en.json`/`sn.json`.

## Onboarding and Waitlist

New phone numbers automatically enter a waitlist. The flow:
1. Language selection (English / Shona)
2. Shop name collection
3. Admin notified via WhatsApp with Approve/Reject buttons
4. On approval: Company created, welcome message sent
5. On rejection: Owner notified
6. Owner records and confirms their **first sale** — bot asks for closing time (3s delay)
7. Owner replies with closing time — permanent schedule set automatically

## Multi-Currency

Each company has a default currency. Individual sales or items can override it. The system detects currency from the message (symbol, code, or written name) and stores it per sale item. When a new currency is detected, the company default is updated automatically.

## GTM Drip Nudges

The nudge system sends proactive messages to shops that haven't been active recently, to drive re-engagement during the pilot.

**Trigger:** Runs every 15 minutes via `send_nudges`. A shop is eligible if it hasn't recorded a sale in the last 6 hours and is within the active send window.

**LLM selection:** Gemini selects the most contextually appropriate nudge CTA from a set of templates for that shop's onboarding stage. CTAs span onboarding (e.g. daily summary opt-in via `onboarding_reports`), retention, and discovery of features the owner hasn't tried yet — full eligibility logic in `nudge_service.py::_eligible_ctas`, this list is illustrative, not exhaustive.

One discovery CTA of note: `discovery_photo_upload` surfaces the existing photo → batch-sale parsing capability (see "Sale Recording from Images" above) as a deliberate end-of-day option for owners who keep a written tally — "kept a written record today? send us a photo." Decided in #258: the underlying vision parsing already handles multi-item handwritten photos and every parsed item goes through the same PENDING → confirm/reject flow as any other sale, so the accuracy risk raised in #258 is bounded the same way a typed multi-item message is. This is a text nudge, not a new WhatsApp template — no Meta submission needed.

**User response:** Most CTAs (e.g. `discovery_multi_item`, `discovery_photo_upload`, retention CTAs) are plain text — the owner just replies normally, no special handling required. `onboarding_reports` is the one button-type CTA today: tapping "Yes, set it up" enters the closing time setup flow, tapping "Not now" sends a brief acknowledgement and suppresses further nudges for that cycle. See `_eligible_ctas`' `cta_type` per CTA for which is which.

**Never-activated shops (#257):** A shop that completed onboarding but has never recorded a single confirmed sale is a different case from one that's merely quiet today — `_eligible_ctas` treats "zero sales ever" (`last_active_days_ago is None`) as a standing fact, not a phase that expires after the onboarding window. `onboarding_first_sale` remains eligible for as long as this holds, regardless of `_ONBOARDING_DAYS` — it just stops being the *only* one once the shop is inside the onboarding window, where `onboarding_shona_tip` and `onboarding_reports` are also eligible; past the window it's the only CTA left for a never-activated shop. `retention_*` and `discovery_*` CTAs (which describe features of an existing sales workflow) are excluded entirely until the shop's first confirmed sale. Separately, `maybe_send_onboarding_followup` sends the `ats_onboarding_followup` template on a capped day-3/7/14 cadence (`_ONBOARDING_FOLLOWUP_DAYS`) to shops in this state, then stops — same pattern as `maybe_send_zero_contact_followup` below, so a still-silent shop after day 14 gets handed to human outreach (#149) rather than more automated messages.

**Zero-message shops:** A shop that was approved but has never sent any inbound message at all (not even to complete onboarding) gets `ats_zero_contact_followup` via `maybe_send_zero_contact_followup`, on the same capped day-2/5/7 pattern, then also stops in favor of human outreach (#149).

**Tap follow-up (#209):** a tap is the highest-leverage re-engagement signal — the owner is
demonstrably present right then. Any button tap with no dedicated handler (most commonly a
quick-reply on a Meta-approved template like `ats_reengagement`, previously just logged and
dropped) is recorded as `Company.last_unhandled_button_tap_at`. If the shop records no confirmed
sale within 2 hours of the tap, `send_nudges` sends one soft plain-text follow-up ("Ready when you
are — just send a sale like *2 Mazoe $3 each*..."). This runs on every 15-minute tick regardless of
the daily send window and isn't blocked by another mechanism having already messaged the shop that
day — both deliberate, since the tap is a one-off, time-sensitive event, not part of the daily
drip those two gates are meant to pace. A fresh tap resets the follow-up flag so the window can
reopen. Shops still tap-but-no-sale are also flagged in `pilot_audit` for a personal follow-up (see
`docs/Architecture.md`).

**Implementation:** `apps/whatsapp/services/nudge_service.py` + `send_nudges` management command.

## Read Receipts & Typing Indicator

**Blue ticks:** The bot marks every inbound message as read via the Meta Cloud API (`mark_as_read`) immediately on receipt. Controlled by `ENABLE_WHATSAPP_MARK_AS_READ` setting.

**Typing indicator:** While the LLM is processing, the bot sends a typing indicator so the shop owner sees `...typing`. Uses Meta Cloud API v21 format (`type: typing_indicator`) — auto-expires after 25s or when the reply is sent. Fires for every inbound message, including short acknowledgements (greetings, "ok", "thanks", etc.) — see below, these now always get a reply too, so the indicator correctly reflects that one is coming.

## Admin Panel

- Waitlist management (approve/reject with one click)
- Pilot metrics dashboard: activation rate and bot mistake rate (configurable lookback)
- Sales audit log with company filter
- WhatsApp message log with transcription and R2 media URL
- Company management including closing time fields

## Staging and QA

- Mock WhatsApp UI at `/mock-whatsapp/` — send messages and see bot replies without a real WhatsApp number, including image uploads with captions (attach button in the mock chat UI)
- Integration test suite (`tests/`) runs against live staging over HTTP
- Mock media endpoint for testing audio flows without real audio files
