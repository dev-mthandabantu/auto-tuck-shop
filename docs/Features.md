# Features

## Sale Recording

Shop owners send a WhatsApp text or voice message describing what they sold. The LLM extracts product names, quantities, and prices from natural language — including Shona, English, and code-switching between both.

Examples:
- `2 Mazoe $3 each` -> 2 x Mazoe @ $3
- `5 chingwa R18 rimwe, 2 shuga R40 rimwe` -> 5 x bread @ R18, 2 x sugar @ R40
- `4munyu umwe R20` -> 4 x salt @ R20 (no space between qty and product name handled)
- `mazai maviri ne chips matatu` -> 2 x eggs, 3 x chips (Shona number words)

Supported currencies: USD ($), ZAR (R), ZWG / ZiG, BWP (P), EUR (euro), GBP (pound). Mixed currencies within one message are supported.

## Sale Confirmation

After parsing, the bot sends a summary with two buttons:
- **Looks good** — confirms the sale, writes it to the database
- **Fix mistake** — cancels the sale, flags it as a bot mistake for metrics

Sales remain `pending` until confirmed. Cancellations are tracked separately from confirmed sales.

## Voice Messages

Owners can send audio notes instead of typing. The system:
1. Downloads audio from Meta
2. Uploads to Cloudflare R2 in the background (permanent archive)
3. Transcribes via ElevenLabs (scribe_v1 model)
4. Processes the transcribed text identically to a typed message

Supported formats: OGG, MP3, M4A, AAC, AMR.

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

**LLM selection:** Gemini selects the most contextually appropriate nudge CTA from a set of templates for that shop's onboarding stage. Currently available CTAs: daily summary opt-in (`onboarding_reports`).

**User response:** Nudge messages include interactive buttons. Tapping "Yes, set it up" enters the closing time setup flow. Tapping "Not now" sends a brief acknowledgement and suppresses further nudges for that cycle.

**Implementation:** `apps/whatsapp/services/nudge_service.py` + `send_nudges` management command.

## Read Receipts & Typing Indicator

**Blue ticks:** The bot marks every inbound message as read via the Meta Cloud API (`mark_as_read`) immediately on receipt. Controlled by `ENABLE_WHATSAPP_MARK_AS_READ` setting.

**Typing indicator:** While the LLM is processing, the bot sends a typing indicator so the shop owner sees `...typing`. Uses Meta Cloud API v21 format (`type: typing_indicator`) — auto-expires after 25s or when the reply is sent. The indicator is suppressed for short acknowledgements (greetings, "ok", "thanks", etc.) that produce no reply, so users don't see typing appear and disappear with no response.

## Admin Panel

- Waitlist management (approve/reject with one click)
- Pilot metrics dashboard: activation rate and bot mistake rate (configurable lookback)
- Sales audit log with company filter
- WhatsApp message log with transcription and R2 media URL
- Company management including closing time fields

## Staging and QA

- Mock WhatsApp UI at `/mock-whatsapp/` — send messages and see bot replies without a real WhatsApp number
- Integration test suite (`tests/`) runs against live staging over HTTP
- Mock media endpoint for testing audio flows without real audio files
