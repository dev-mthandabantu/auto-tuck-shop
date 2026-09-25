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
    +-- cron machine (every 15 min: send_daily_reports, send_nudges, auto_cancel_stale_sales;
    |                  daily 08:00 Africa/Harare: pilot_activity_alert)
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

The production deploy is gated on the `unit_tests` check: before `flyctl deploy` runs, the workflow
looks up the most recently completed `unit_tests` check run for the released commit and aborts
unless its conclusion is `success`. A commit whose check is failing, cancelled, skipped, still
running, or absent entirely will not reach production.

The step fails closed. If the API call itself fails, or the response is not a well-formed
`check_runs` array, the deploy is refused and the response body is printed -- an unreadable
answer is never treated as a passing build.

The gate lives in `scripts/check-unit-tests-passed.sh` rather than inline in the workflow, so it
can be tested. `scripts/test-check-unit-tests-passed.sh` runs it against a fake `gh` covering 18
cases (re-runs, every non-success conclusion, malformed and error responses, bad arguments). The
`deploy_gate` job in `staging-tests.yml` runs those tests on every PR and push to `main`, since the
gate itself only ever runs on a release.

This gates the *deploy*, not the merge -- a commit with failing tests can still land on `main` and
auto-deploy to staging. It is the narrower substitute for branch protection described in #373
(option 4), not equivalent protection.

If a release is blocked by a check that is missing rather than failing (CI never ran, or the run
aged out -- from 1 Oct 2026 GitHub expires check runs on the Actions retention setting, default 90
days, where they previously lasted 400), re-run the `CI` workflow on that commit, then re-publish
the release. Do not work around the gate by deleting it.

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
4. Approving reviewers: Brighton, Omar, Kundai, or Munashe -- one approval expected before merge. This is convention, not GitHub-enforced: branch protection is unavailable on this org's plan for a private repo (#373). See AGENTS.md
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
| `ADMIN_PHONE_NUMBER` | Yes | WhatsApp number(s) for admin notifications. Comma-separated for multiple admins (#388); the first is the primary and is the only one that receives Approve/Reject buttons |
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

## Unit Tests in CI

`unit_tests/` runs on every PR and push to `main` (`.github/workflows/staging-tests.yml`, job
`unit_tests`). The job starts a `postgres:16` service container -- SQLite is not usable here,
because `apps/core/migrations/0012_drop_orphan_company_columns.py` uses `ALTER TABLE ... DROP
COLUMN IF EXISTS`, which SQLite rejects at migrate time.

Installs both `requirements.txt` and `requirements-dev.txt`: the former has no `pytest` (imported
by `unit_tests/test_language_business_continuity.py`), the latter has no Django.

The 22 tests in `unit_tests/test_shona_parsing.py` call the live LLM and skip automatically when
`OPENROUTER_API_KEY` is absent, which is the case in CI. They are not enforced there -- run them
locally with a key set when changing parsing behaviour.

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
