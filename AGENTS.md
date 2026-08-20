# Project Guidelines

## Keeping this file current

This is the source of truth for any AI assistant working on this repo. Update it at the end of any session with meaningful progress — phase changes, new conventions, team changes. Don't let it drift.

**Doc updates ship in the same PR as the change they affect**, not as a follow-up — see "Docs index" below. This applies even to small changes; a one-line fix can still contradict a documented invariant.

## Start of every session

1. **Who is working?** — Brighton or Madrena (see "Working with Madrena" below for what that means in practice)
2. **What are we working on?** — get the issue number, or a short description if it's exploratory/no issue applies (e.g. a docs-only change). If there's an issue, read it first, and **check for open PRs already referencing it** (`gh pr list --search "<issue-number> in:body"`) before writing code — a reassigned issue can have work in flight elsewhere (#284 shipped duplicate fixes this way).
3. **Check the relevant `docs/` page(s) before proposing or writing any change** — every session, regardless of how small the task looks.
4. **Know today's actual date before judging anything as current or stale.** Don't assume a training cutoff or a remembered date is still right — run `date` (or equivalent) if there's any doubt. A "recent" commit, discussion, or milestone deadline is only recent relative to the real current date, not a guess.

Skip a question only if the user's first message already answers it. Steps 2's open-PR check, 3, and 4 are never skipped.

## Flagging drift when you see it

When a task has you reading an issue, doc, discussion, or deploy state anyway, check whether it's actually current before acting on it — don't silently work inside stale context. This has caught real problems before: a process rule that covered one required field but not a near-identical one right next to it, so new issues quietly shipped incomplete; a summary doc built from a single repo's tracker when the real work spanned several; a production release that had silently failed weeks earlier, unnoticed until the next deploy attempt surfaced it.

This is **reactive, not a standing scan** — check what you're already touching, don't go looking for staleness unprompted. One exception: early in a session, a cheap check of whether `business-operations` discussion #23 has a comment for the current calendar week is worth doing and saying one line about either way — that thread is the team's actual weekly pulse, and a missing kickoff comment is a real, high-value signal. Once you've confirmed it for the week, no need to re-check it again in later sessions that same week.

When the question is "what should person X actually work on," check this repo's own [discussion #304 — "Roadmap: who's working on what"](https://github.com/aakitech/auto-tuck-shop/discussions/304) too, not just #23. #23 carries the live sprint narrative (deadlines, blockers, urgency) across all three repos; #304 is the standing per-person "what's your lane in ATS" picture and is often the more specific source for an individual's assignments here. Use #23 for what's urgent, #304 for whose desk something sits on.

Applies to everyone, not just Brighton — but tune the delivery per the same split as "Working with Madrena" below: terse and direct for Brighton/Kundai/Omar/Munashe, framed without alarm for Madrena (a flagged staleness issue is information, not a mistake she made).

## Working with Madrena

Junior developer, also a founding member of the team — still genuinely early as an engineer: building fundamentals, not yet fluent in how the code works day to day. Assuming she already understands the code is the risk here, not an unkind assumption to avoid.

Concretely, when pairing with her:

- **Explain before executing** — walk through why something broke or why an approach was chosen, don't just fix it silently.
- **Check understanding before moving on**, especially before she'd need to build on a concept herself. Don't treat this as optional politeness — it's how the gap actually closes.
- **When she brings a bug, explain the cause**, not just the patch — goal is she can debug the next one herself.
- **Terse, assume-the-context responses are for Brighton, Kundai, Omar, Munashe — not her.**
- **PR/Git workflow gets the same explain-first treatment as code** — don't assume Git fluency just because she's shipped PRs before.

---

Auto Tuck Shop is a Django app. Shop owners in Zimbabwe send WhatsApp text or voice messages to record sales. The bot parses them with an LLM (`google/gemini-2.5-flash-lite` via OpenRouter, set by `OPENROUTER_MODEL`), creates sale records, and replies with a confirmation. Voice messages are transcribed by ElevenLabs first.

**Gemini direct fallback:** if OpenRouter returns 402 (out of credits), `services/openrouter/client.py` retries against Gemini's own API directly via `GEMINI_API_KEY`, bypassing OpenRouter for that call. Optional but should be set on staging and prod — see `.env.example`.

## Team

| Person | Role | GitHub |
|---|---|---|
| Brighton | Tech lead, repo owner | @dev-thandabantu |
| Madrena | Junior developer, founding member | @dev-mthandabantu |
| Jasper | Intern — generalist developer | @dev-jaspher |
| Bradley | Field/business, pilot shop relationships | @bradleychibuwe105-coder |
| Kundai | Tech lead / co-owner | @KundaiClayton |
| Law | QA & Product Quality Lead; partner relationships; funding pipeline (across ATS, BOQ, AgriData) | @law001-mugah |
| Tafadzwa | QA Intern (ATS audit), reports to Law | @TafadzwaGwangwaya |
| Omar | Intern — cross-product generalist, reports to Brighton (QA tasks to Law) | @O-keita |
| Munashe | Full-stack engineer & AakiTech co-founder — primarily on other projects (AgriData AI, BOQ Generator), has repo access here as an approving reviewer | @dev-munashe |

## Current phase

**Pilot active — production live since 2026-05-25.** Bot is receiving/replying to real WhatsApp messages on production.

Open items: GitHub project board, not here. Match any task to an open issue; check with the user first if it doesn't map to one.

**New issues go on the project immediately:** `gh issue edit <number> --add-project "Auto Tuck Shop — Backlog"`

## Where to find context

- **Service entry points:** `apps/whatsapp/services/` — webhook_handler (main entry), sale_handler, waitlist_handler, business_reports, nudge_service, media_handler, message_parser, report_card, whatsapp_client
- **LLM system prompt (parsing tuning):** `services/openrouter/prompts.py` — text parsing (`build_unified_parsing_prompt`) and image/vision parsing (`build_image_parsing_prompt`)
- **User-facing strings (EN + Shona):** `apps/whatsapp/locales/en.json`, `sn.json`
- **Unit tests:** `python manage.py test unit_tests`
- **Staging integration tests (manual only):** `python -m pytest tests/ -x` (requires `.env.staging`) — not in CI, see #82

**Confidential/strategic content** (partner trust, funding, team structure, legal, commercial negotiations) goes in `aakitech/internal` instead of here (both private repos, discussions/issues only) — this repo's access isn't limited to core eng (it includes field/business and intern roles, see Team above), so treat it as the wider-audience one of the two. See `aakitech/internal` #14 for rationale.

## Docs index

Read the relevant page before starting any task (session step 3). Update docs in the same PR as any behaviour change.

| File | Content |
|------|---------|
| `docs/Architecture.md` | Django apps, data model, URL structure, message pipeline, flows, async model |
| `docs/Features.md` | User-facing features: sale recording, daily reports, onboarding, queries |
| `docs/Integrations.md` | Meta WhatsApp, OpenRouter, ElevenLabs, R2, Fly.io, Sentry |
| `docs/Deployment.md` | Environments, deploy workflow, env vars, migrations, health check |
| `docs/Pilot-Launch.md` | Pilot success metrics and review cadence |
| `docs/Home.md` | Overview and quick links |

## Scope rule

Production is live. A change that doesn't support the 20-shop pilot or fix a real issue belongs in the backlog.

## Keeping the backlog current

- **Close issues on merge:** `gh issue close <number> --comment "Fixed in #<PR>"`
- **New out-of-scope issues found mid-work** get added to the project immediately, same command as above.
- **Set Status on every new issue** — GitHub doesn't auto-assign one; an unset issue clutters the "No Status" column.
- **Set a milestone on every new issue** — ask before leaving blank if none fits.
- **Update priority labels** when significance changes (e.g. "nice to have" → production bug).
- **Update this file's phase status** when the above changes.

## Shipping flow

1. **Branch off main**, one branch per issue (e.g. `fix/duplicate-language-prompt`).
2. **Open a PR** — title includes the issue number. Fill in the PR template properly; Plan/Prediction is written *before* reviewing your own diff, not after. If it touches rendered UI (Django admin, report card images, WhatsApp message/button rendering), attach a screenshot/GIF/video — see the PR Readiness checklist.
3. **Tag a reviewer** in GitHub Discussions → Pull Requests (skip if the PR already tags one directly).
4. **Approving reviewers: Brighton, Omar, Kundai, or Munashe** — branch protection requires one approval before merge. Madrena is not a required/approving reviewer (see "Working with Madrena") — she can still read PRs and ask questions as part of learning. **Action item, not yet done:** the actual GitHub branch protection rule on `main` still needs manual updating to match this — check Settings → Branches → main → required reviewers.
5. **Merge** once approved — squash merge, branch deleted.
6. **Staging auto-deploys** on every push to main (GitHub Actions, no manual step).
7. **Production deploy** — Brighton publishes a GitHub Release, which triggers auto-deploy. Never deploy to prod without Brighton's explicit sign-off.

### Production release steps

Semver. Only Brighton publishes releases.

- **Patch:** bug fixes, copy changes, infra/ops fixes, parsing improvements.
- **Minor:** new user-facing capability.
- **Major:** product stable, scaling beyond the pilot.
- Rule of thumb: shop owner would notice → minor; wouldn't → patch.

Prerequisites: branch merged to main, staging healthy.

```bash
git log vX.X.X..origin/main --oneline   # what's shipped since last tag
gh release list --limit 5               # find the last tag
gh release create vX.X.X \
  --title "vX.X.X — <short description>" \
  --notes "## What's in this release

- Fix: <description> (#issue)"
```

`deploy-production.yml` triggers on `release: published` — no manual `fly deploy`. Monitor: https://github.com/aakitech/auto-tuck-shop/actions

## Deployment

`USE_MOCK_WHATSAPP` must **not** be `True` on production — if it is, the bot silently swallows outbound messages instead of sending them. Confirm before every prod deploy.

## Sentry

- `SENTRY_ENVIRONMENT` defaults to `production` in `config/settings/base.py` — **staging** must set it explicitly (`SENTRY_ENVIRONMENT=staging` as a Fly secret), or staging events get tagged production and pollute prod error filters.
- `SENTRY_AUTH_TOKEN` (GitHub Actions secret, deploy workflow only) creates a release on every prod deploy for auto-resolve and version tracking.
- Cron monitors: `send-daily-reports`, `send-nudges` at `aakitech.sentry.io/crons/`.

## Migrations

Migration files are immutable once committed — never edit or delete one, always create a new one:

```bash
python manage.py makemigrations
python manage.py migrate
```

Commit new migrations immediately, in the same PR as the schema change.

## Pilot audit

```bash
fly ssh console --app auto-tuck-shop -C "python manage.py pilot_audit"
```

`--hours 48` (or any number) for a wider window. Known test accounts excluded automatically: +27644178150, +27610869293.

When changing parsing behavior, add/update regression tests in `unit_tests/test_shona_parsing.py`.
