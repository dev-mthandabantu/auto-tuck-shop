# Deployment

## Infrastructure

```
Shop owner (WhatsApp)
        |
        v
Meta WhatsApp Business API
        |
        v
Fly.io (auto-tuck-shop / auto-tuck-shop-staging)
    +-- app machines (Gunicorn)
    +-- cron machine (every 15 min: send_daily_reports, send_nudges, auto_cancel_stale_sales)
    +-- PostgreSQL (Fly managed)
        |
        +-- OpenRouter (Gemini 2.5 Flash) -- LLM parsing
        +-- ElevenLabs -- voice transcription
        +-- Cloudflare R2 -- media + report card storage
        +-- Sentry -- error tracking
```

## Environments

| | Development | Staging | Production |
|-|-------------|---------|------------|
| Database | SQLite | PostgreSQL | PostgreSQL |
| WhatsApp | Disabled | Mock (in-memory) | Real Meta API |
| Fly config | -- | `fly.staging.toml` | `fly.toml` |
| App name | -- | `auto-tuck-shop-staging` | `auto-tuck-shop` |
| Django settings | `config.settings.base` | `config.settings.staging` | `config.settings.production` |
| Sentry env | -- | `staging` | `production` |
| Test APIs | -- | Enabled | Disabled |
| R2 bucket | -- | `auto-tuck-shop-staging` | `auto-tuck-shop-production` |

## Deploy Workflow

**Staging** deploys automatically on every push to `main` (GitHub Actions).

**Production** deploys automatically when a GitHub Release is published (GitHub Actions `deploy-production.yml`).

To create a production release:
```bash
# Check what's in main since last release
git log vX.X.X..origin/main --oneline

# Publish release (triggers prod deploy)
gh release create vX.X.X \
  --title "vX.X.X -- short description" \
  --notes "## What's in this release
- Fix: description (#issue)"
```

Releases follow semver patch bumps (v0.1.0 -> v0.1.1). Only Brighton publishes releases.

> **Never deploy to production without explicit sign-off from Brighton.**

## PR and Branch Workflow

1. Branch off `main` -- one branch per issue (`fix/issue-name`, `feat/feature-name`)
2. Open a PR -- title includes the issue number
3. Post in GitHub Discussions -> Pull Requests, tag a reviewer
4. Approving reviewers: Brighton, Omar, Kundai, or Munashe (branch protection requires at least one approval) -- see AGENTS.md
5. Squash merge -> branch deleted -> staging auto-deploys

## Key Environment Variables

| Variable | Required | Notes |
|----------|----------|-------|
| `SECRET_KEY` | Yes | Django secret key |
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `ALLOWED_HOSTS` | Yes | Comma-separated hostnames |
| `CSRF_TRUSTED_ORIGINS` | Yes | Comma-separated origins |
| `META_WHATSAPP_ACCESS_TOKEN` | Yes | Never rotates (system user token) |
| `META_WHATSAPP_PHONE_NUMBER_ID` | Yes | |
| `META_WHATSAPP_APP_SECRET` | Yes | For HMAC verification |
| `META_WHATSAPP_VERIFY_TOKEN` | Yes | For webhook verification |
| `OPENROUTER_API_KEY` | Yes | |
| `ELEVENLABS_API_KEY` | Yes | Free tier has 5 credits -- monitor usage |
| `R2_ACCESS_KEY_ID` | Yes | Must be R2-specific token (not Account token) |
| `R2_SECRET_ACCESS_KEY` | Yes | |
| `R2_ENDPOINT_URL` | Yes | `https://{account-id}.r2.cloudflarestorage.com` -- must match the account the token belongs to |
| `R2_BUCKET_NAME` | Yes | `auto-tuck-shop-production` or `auto-tuck-shop-staging` |
| `R2_PUBLIC_URL` | No | Public URL prefix for serving report card images |
| `ADMIN_PHONE_NUMBER` | Yes | WhatsApp number for admin notifications |
| `SENTRY_DSN` | No | Error tracking |
| `USE_MOCK_WHATSAPP` | Staging only | Must be `False` or unset on production |
| `GEMINI_API_KEY` | No | Direct Gemini fallback if OpenRouter returns 402 (out of credits) — live, see `services/openrouter/client.py`. Optional but should be set on both staging and prod |

## Migrations

Migration files are immutable once committed. Never edit or delete an existing migration.

```bash
python manage.py makemigrations
python manage.py migrate
```

New migrations must be committed in the same PR as the schema change. Migrations run automatically on deploy via `release_command` in `fly.toml`.

## Health Check

`GET /healthz/` -- returns 200 when the app is up. Fly.io polls this every 30 seconds.

## Integration Tests

Tests in `tests/` run against the live staging app over HTTP -- no Django imports, no mocks. Require `.env.staging` with staging URL and API credentials.

```bash
python -m pytest tests/ -x
```

Not run in CI -- manual only. See issue #82 for context.

## First Deploy to a New App

The first-ever deploy to a new Fly app must be done manually -- `fly secrets set` alone won't create machines:
```bash
fly deploy --config fly.staging.toml --app auto-tuck-shop-staging
```
Subsequent deploys happen automatically via GitHub Actions.
