# Auto Tuckshop — Copilot Instructions

## Start of every chat session

Before doing anything else, ask:

1. **Who is working?** — Brighton (tech lead) or Madrena (junior developer). Brighton gets concise technical responses. Madrena gets more explanation, especially around Git, PRs, and Django patterns she may not have seen before.
2. **What are we working on?** — ask for the GitHub issue number if there is one. Read the issue before starting work.

If the user's first message already makes it clear (e.g. "help me with issue #18"), skip what's already answered.

---

Django app. Shop owners in Zimbabwe send WhatsApp text or voice messages to record sales. The bot parses them with an LLM, creates sale records, and replies with a receipt.

## Stack

- Django 5 — backend
- WhatsApp Business API (Meta) — message intake at `apps/whatsapp/views.py`
- OpenRouter (Gemini 2.5 Flash) — LLM parsing at `services/openrouter/`
- ElevenLabs — voice transcription at `services/elevenlabs/`
- Cloudflare R2 — media storage at `services/storage/`
- Fly.io — hosting (staging + production)
- PostgreSQL — production database

## Key entry points

| File | Purpose |
|---|---|
| `apps/whatsapp/views.py` | Webhook entry point |
| `apps/whatsapp/services/sale_handler.py` | Sale creation and button processing |
| `apps/whatsapp/services/waitlist_handler.py` | Onboarding and language selection |
| `apps/whatsapp/services/media_handler.py` | Audio download, transcription, R2 upload |
| `apps/whatsapp/services/message_parser.py` | LLM call and result parsing |
| `services/openrouter/prompts.py` | System prompt — tune this for parsing improvements |
| `apps/whatsapp/locales/` | `en.json` and `sn.json` — all user-facing strings |

## Current phase

Pilot prep. Code is ready. Not yet deployed to production. When suggesting tasks, prioritise items that directly enable the first live deploy — accounts, secrets, staging deploy, webhook registration.

## Team

- Brighton (@dev-thandabantu) — tech lead
- Madrena (@dev-mthandabantu) — junior developer
- Bradley (@bradleychibuwe105-coder) — field/business

## Rules

- Never edit existing migration files. Always create a new migration.
- Never commit `.env` files. Use environment variables.
- All changes go through a PR. No direct pushes to `main`.
- Staging deploy before production deploy, always.
- Production deploy requires Brighton's explicit sign-off.

## Language handling

Messages arrive in English, Shona, or mixed. The LLM handles parsing. Key Shona:
- `imwe` / `imwe neimwe` = "each" (per-unit price marker)
- `maviri`=2, `matatu`=3, `mana`=4, `mashanu`=5
- `ne` = "and" (joins items in a list)

Regression tests for Shona parsing: `unit_tests/test_shona_parsing.py`

## Testing

```bash
# Unit tests
python manage.py test unit_tests

# Staging integration tests (requires .env.staging with STAGING_URL)
python -m pytest tests/ -x
```
