# Project Guidelines

## Keeping this file current

This file is the source of truth for any AI assistant working on this repo. **Keep it up to date as the project evolves** — when the phase changes, when new conventions are established, or when the team changes. At the end of any session where meaningful progress was made, update the relevant sections here. Don't let it drift.

## Start of every session

Before doing anything else, ask the user these two questions:

1. **Who is working?** — Brighton or Madrena (this changes how you respond: Brighton gets concise technical responses; Madrena gets more explanation and junior-friendly framing, especially around PR workflow and Git)
2. **What are we working on?** — ask for the GitHub issue number if there is one, or a short description if it's exploratory. Then read that issue before starting.

If the user's first message already makes it obvious (e.g. "fix issue #18"), skip the question it answers and only ask what's still unclear.

---

Auto Tuck Shop is a Django app. Shop owners in Zimbabwe send WhatsApp text or voice messages to record sales. The bot parses them with an LLM (Gemini 2.5 Flash via OpenRouter), creates sale records, and replies with a confirmation. Voice messages are transcribed by ElevenLabs first.

## Team

| Person | Role | GitHub |
|---|---|---|
| Brighton | Tech lead, repo owner | @dev-thandabantu |
| Madrena | Junior developer | @dev-mthandabantu |
| Jasper | Intern — generalist developer | @dev-jaspher |
| Bradley | Field/business, pilot shop relationships | @bradleychibuwe105-coder |

## Current phase

**Pilot active — production is live as of 2026-05-25.** The bot is receiving and replying to real WhatsApp messages on production.

Open items are tracked on the GitHub project board — check there for current status, not here.

When an AI assistant is helping with a task, check which issue it maps to and work within that scope. If a task doesn't map to an open issue, check with the user before starting.

**When creating a new GitHub issue, immediately add it to the project — this is required, not optional:**
```bash
gh issue edit <number> --add-project "Auto Tuck Shop — Backlog"
```

## Where to find context

Before starting any task, read the relevant context:

- **Architecture, data model, flows, async model:** `docs/Architecture.md`
- **Pilot launch sequence and success metrics:** `docs/Pilot-Launch.md`
- **All external service integrations:** `docs/Integrations.md`
- **Key service entry points:** `apps/whatsapp/services/` — webhook_handler (main entry), sale_handler, waitlist_handler, business_reports, nudge_service, media_handler, message_parser, report_card, whatsapp_client
- **LLM system prompt (tune for parsing improvements):** `services/openrouter/prompts.py`
- **User-facing strings (EN + Shona):** `apps/whatsapp/locales/en.json`, `sn.json`
- **Unit tests:** `python manage.py test unit_tests`
- **Staging integration tests (manual only):** `python -m pytest tests/ -x` (requires `.env.staging`) — not run in CI, see #82

**Confidential/strategic content** (partner trust assessments, funding, team structure, corporate/legal, commercial negotiations): goes in `aakitech/internal` (private, no code — discussions/issues only), not here — this repo is visible to pilot contacts and future interns. See discussion #14 there for rationale.

## Docs index

Documentation lives in `docs/` in this repo. Read the relevant file before starting any task. Update docs in the same PR as any behaviour change.

| File | Content |
|------|---------|
| `docs/Architecture.md` | Django apps, data model, URL structure, message pipeline, flows, async model |
| `docs/Features.md` | User-facing features: sale recording, daily reports, onboarding, queries |
| `docs/Integrations.md` | Meta WhatsApp, OpenRouter, ElevenLabs, R2, Fly.io, Sentry |
| `docs/Deployment.md` | Environments, deploy workflow, env vars, migrations, health check |
| `docs/Pilot-Launch.md` | Pilot success metrics and review cadence |
| `docs/Home.md` | Overview and quick links |

## Scope rule

Production is live. If a change doesn't directly support the 20-shop pilot or fix a real issue, it belongs in the backlog.

## Keeping the backlog current

When closing out a task, update the GitHub issue and backlog as part of the same session:

- **Close issues** when the fix is merged: `gh issue close <number> --comment "Fixed in #<PR>"`
- **Add new issues** for anything discovered mid-work that's out of scope for the current PR. Add them to the project immediately: `gh issue edit <number> --add-project "Auto Tuck Shop — Backlog"`
- **Set a Status immediately** when adding any issue to the project board — GitHub does not auto-assign one. An issue with no Status shows up in a "No Status" column and clutters the board. Always set Todo, In Progress, or Done at the time of adding.
- **Update priority labels** if the significance of an open issue changes (e.g. a "nice to have" becomes a production bug)
- **Update CLAUDE.md phase status** when the open items above change

## Shipping flow

1. **Branch off main** — one branch per issue, named after it (e.g. `fix/duplicate-language-prompt`)
2. **Open a PR** when ready — title includes the issue number
3. **Post in GitHub Discussions → Pull Requests** — tag `@dev-mthandabantu` so she sees it
4. **Madrena reviews and approves** on GitHub — branch protection requires at least one approval before merge
5. **Merge** once approved — squash merge, branch gets deleted
6. **Staging auto-deploys** — GitHub Actions deploys to staging on every push to main (no manual step needed)
7. **Production deploy** — Brighton publishes a GitHub Release → GitHub Actions auto-deploys to prod. Never deploy to production without explicit sign-off from Brighton.

### How to create a production release (step-by-step)

Releases follow semver. Only Brighton publishes releases.

- **Patch** (`v0.1.x → v0.1.x+1`): bug fixes, copy changes, infra/ops fixes, parsing improvements
- **Minor** (`v0.x.0 → v0.x+1.0`): new user-facing capability (new interaction flow, new report type, new media handling, etc.)
- **Major** (`v1.0.0`): product is stable and being scaled beyond the pilot

When in doubt: if a shop owner would notice a new thing they can do → minor. If they'd never know → patch.

**Prerequisites:** branch is merged to main, staging looks healthy.

```bash
# 1. Check what's in main since the last release (replace vX.X.X with the last tag)
git log vX.X.X..origin/main --oneline

# 2. Find the last release tag so you know what to bump to
gh release list --limit 5

# 3. Publish the release — this triggers the prod deploy workflow automatically
gh release create vX.X.X \
  --title "vX.X.X — <short description>" \
  --notes "## What's in this release

- Fix: <description> (#issue)
- Fix: <description> (#issue)"
```

The `deploy-production.yml` workflow triggers on `release: published` — no manual `fly deploy` needed. Monitor the deploy at: https://github.com/aakitech/auto-tuck-shop/actions

## Deployment

`USE_MOCK_WHATSAPP` must **not** be set (or set to `False`) on production — if it's `True` on prod, the bot silently swallows all outbound messages instead of sending them via WhatsApp. Confirm this before every prod deploy.

## Sentry

- `SENTRY_ENVIRONMENT=production` must be set as a Fly secret on prod — without it events are tagged `development` and environment filters break
- `SENTRY_AUTH_TOKEN` is a GitHub Actions secret (deploy workflow only) — creates a release on every prod deploy so issues auto-resolve and show which version introduced them
- Cron monitors: `send-daily-reports` and `send-nudges` at `aakitech.sentry.io/crons/`

## Migrations

Migration files are immutable once committed. Never edit or delete an existing migration. Always create a new one:

```bash
python manage.py makemigrations
python manage.py migrate
```

New migrations must be committed immediately and included in the same PR as the schema change.

## Pilot audit

When Brighton says "audit the pilot", "check shop activity", or similar, run:

```bash
fly ssh console --app auto-tuck-shop -C "python manage.py pilot_audit"
```

Pass `--hours 48` (or any number) for a wider window.

Known test accounts excluded automatically: +27644178150, +27610869293

When making changes to parsing behavior, add or update regression tests in `unit_tests/test_shona_parsing.py`.
