# Auto Tuck Shop

AI-powered WhatsApp assistant for tuckshops in Zimbabwe. Shop owners text or voice message what they sold (like "2 cokes $1 each"), and the bot automatically records the sale and sends a confirmation.

**Planning or contributing?** See the [wiki](https://github.com/aakitech/auto-tuck-shop/wiki) — architecture, flows, integrations, and pilot launch plan.

**New here?** See [ONBOARDING.md](ONBOARDING.md) for local setup and deployment.

**Contributing?** See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR workflow.

## Tech stack

- **Django** — backend framework
- **WhatsApp Business API** (Meta) — messaging
- **OpenRouter** (Gemini 2.5 Flash) — AI parsing
- **ElevenLabs** — voice-to-text transcription
- **Cloudflare R2** — media storage
- **Fly.io** — hosting (staging + production)
- **PostgreSQL** — database

## Development workflow

All code changes require a pull request. Direct pushes to `main` are blocked.

```bash
git checkout -b your-branch-name
# make changes
git push origin your-branch-name
# open PR on GitHub
```

## Deploying

Always deploy to staging first, run tests, then production — per AGENTS.md. **Production deploys only via a Brighton-published GitHub Release — never `fly deploy` directly to prod.**

```bash
fly deploy -c fly.staging.toml
python -m pytest tests/ -x
```

## Testing on staging

Staging replaces the real WhatsApp API with a mock that captures all outbound messages.

**Manual — Mock WhatsApp UI**

1. Deploy to staging
2. Go to `https://auto-tuck-shop-staging.fly.dev/mock-whatsapp/`
3. Log in with your Django admin credentials

The UI lets you send messages, click buttons, and see responses as any phone number. Messages go through the full pipeline — only WhatsApp delivery is mocked.

**Automated — integration tests**

```bash
python -m pytest tests/ -x
```

Tests are HTTP clients that send webhook payloads to the staging app and poll the mock outbox for responses. Required in `.env.staging`: `STAGING_URL`, `TEST_API_KEY`, `META_WHATSAPP_APP_SECRET`.

**Django unit tests**

```bash
python manage.py test unit_tests
```
