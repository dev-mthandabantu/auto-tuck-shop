# Project Guidelines

## Start of every session

1. **Who is working?** — Brighton or Madrena (see "Working with Madrena" below for what that means in practice)
2. **What are we working on?** — get the issue number, or a short description if it's exploratory/no issue applies (e.g. a docs-only change). If there's an issue, read it first, and **check for open PRs already referencing it** (`gh pr list --search "<issue-number> in:body"`) before writing code — a reassigned issue can have work in flight elsewhere (#284 shipped duplicate fixes this way).
3. **Check the relevant `docs/` page(s) before proposing or writing any change** — every session, regardless of how small the task looks.
4. **Know today's actual date before judging anything as current or stale.** Don't assume a training cutoff or a remembered date is still right — run `date` (or equivalent) if there's any doubt. A "recent" commit, discussion, or milestone deadline is only recent relative to the real current date, not a guess.

Skip a question only if the user's first message already answers it. Steps 2's open-PR check, 3, and 4 are never skipped.

**This checklist re-runs on every task switch, not just at chat-start.** The risk isn't "old chat session" — it's stale context carried into work it doesn't apply to. A chat continued hours later on the *same* issue carries no extra risk and doesn't need to repeat this. But the moment the task changes — a different issue, a different repo, a different kind of work — treat it exactly like a new session: redo steps 2–4 (open-PR check, relevant docs, current date) before writing anything, even if that switch happens minutes into the same chat. Long-running chats accumulate context from earlier issues that can quietly bias reads of a new one; re-running the checklist is what resets that.

## Flagging drift when you see it

When a task has you reading an issue, doc, discussion, or deploy state anyway, check whether it's actually current before acting on it — don't silently work inside stale context. This has caught real problems before: a process rule that covered one required field but not a near-identical one right next to it, so new issues quietly shipped incomplete; a summary doc built from a single repo's tracker when the real work spanned several; a production release that had silently failed weeks earlier, unnoticed until the next deploy attempt surfaced it.

This is **reactive, not a standing scan** — check what you're already touching, don't go looking for staleness unprompted. One exception: early in a session, a cheap check of whether `business-operations` discussion #23 has a comment for the current calendar week is worth doing and saying one line about either way — that thread is the team's actual weekly pulse, and a missing kickoff comment is a real, high-value signal. Once you've confirmed it for the week, no need to re-check it again in later sessions that same week.

When the question is "what should person X actually work on," check this repo's own [discussion #304 — "Roadmap: who's working on what"](https://github.com/aakitech/auto-tuck-shop/discussions/304) too, not just #23. #23 carries the live sprint narrative (deadlines, blockers, urgency) across all three repos; #304 is the standing per-person "what's your lane in ATS" picture and is often the more specific source for an individual's assignments here. Use #23 for what's urgent, #304 for whose desk something sits on. For Madrena, a flagged staleness issue is information, not a mistake she made.

## Working with Madrena

Junior developer, also a founding member of the team — still genuinely early as an engineer: building fundamentals, not yet fluent in how the code works day to day. Assuming she already understands the code is the risk here, not an unkind assumption to avoid.

Concretely, when pairing with her:

- **Explain before executing** — walk through why something broke or why an approach was chosen, don't just fix it silently.
- **Check understanding before moving on**, especially before she'd need to build on a concept herself. Don't treat this as optional politeness — it's how the gap actually closes.
- **When she brings a bug, explain the cause**, not just the patch — goal is she can debug the next one herself.
- **Terse, assume-the-context responses are for Brighton, Kundai, Omar, Munashe — not her.**
- **PR/Git workflow gets the same explain-first treatment as code** — don't assume Git fluency just because she's shipped PRs before.

## What this is

Auto Tuckshop is a Django app. Shop owners in Zimbabwe send WhatsApp text or voice messages to record sales. The bot parses them with an LLM (`google/gemini-2.5-flash-lite` via OpenRouter, set by `OPENROUTER_MODEL`), creates sale records, and replies with a confirmation. Voice messages are transcribed by ElevenLabs first.

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

**Pilot paused since 2026-08-03.** Production still answers inbound messages; outbound (nudges and daily reports) is off; no new shops are onboarded, so don't approve waitlist applications. The restart checklist is **#335**: if it's closed, this section is out of date, so say so.

Open items live on the GitHub project board. Match any task to an open issue; check with the user first if it doesn't map to one.

## Where to find context

- **Service entry points:** `apps/whatsapp/services/` — webhook_handler (main entry), sale_handler, waitlist_handler, business_reports, nudge_service, media_handler, message_parser, report_card, whatsapp_client
- **LLM system prompt (parsing tuning):** `services/openrouter/prompts.py` — text parsing (`build_unified_parsing_prompt`) and image/vision parsing (`build_image_parsing_prompt`)
- **User-facing strings (EN + Shona):** `apps/whatsapp/locales/en.json`, `sn.json`
- **Unit tests:** `python manage.py test unit_tests`
- **Production deploy gate tests:** `./scripts/test-check-unit-tests-passed.sh` (bash, no deps; run by the `deploy_gate` CI job)
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
| `docs/Brand.md` | Logo files, colour palette, tone and pilot-honesty rules for public-facing material |
| `docs/Home.md` | Overview and quick links |

`docs/` is the sole source of truth. The GitHub wiki is unreachable on this plan; don't sync it (#36).

## Scope rule

Production is live and takes real inbound messages, so anything that breaks the bot for a shop that messages it is still urgent.

But the pilot is paused (see "Current phase"), and that changes what else is worth doing. With no live cohort, new user-facing behaviour has nobody to reach — it accumulates as inventory rather than shipping. Work that *does* earn its place right now:

- Fixes to inbound handling — a shop that messages the bot must get a correct response.
- Work in the milestone for the next cohort, and anything on the restart checklist (#335).
- Test integrity and CI (#82) — the suite protects a codebase several people are shipping into.

New features outside that milestone belong in the backlog until the restart.

## Keeping the backlog current

- **Close issues on merge:** `gh issue close <number> --comment "Fixed in #<PR>"`
- **New issues, including out-of-scope ones found mid-work, go on the project immediately:** `gh issue edit <number> --add-project "Auto Tuck Shop — Backlog"`
- **Set Status on every new issue** — GitHub doesn't auto-assign one; an unset issue clutters the "No Status" column.
- **Set a milestone on every new issue** — ask before leaving blank if none fits.
- **Update priority labels** when significance changes (e.g. "nice to have" → production bug).
- **Update this file's phase status** when the above changes.

## Before requesting review

Review round-trips here have run 60–130h against a ~2h median, mostly over things the author could
have found alone. Run `python manage.py test unit_tests` (say so in the PR if you couldn't), then:

1. Run your own adversarial pass: what would still pass if the feature were deleted, which claims in
   the PR body don't match the code, which states the tests never construct. Keep findings local.
2. Run a fresh-context reviewer (new Claude session, review subagent, or Codex) with only the PR and
   its issue. Give it none of your conclusions.
3. Fix what holds up from both reviews.
4. Post one consolidated PR comment: what each review attacked, found, fixed, and left.
5. Only then tag a human reviewer. Docs-only PRs: "Trivial — no adversarial pass needed".

**Reviewers:** don't request a change you haven't traced to a concrete affected case; label blocking
vs discussion explicitly; retract out loud when you're wrong.

## Shipping flow

1. **Branch off main**, one branch per issue (e.g. `fix/duplicate-language-prompt`).
2. **Open a PR** — title includes the issue number. Fill in the PR template properly; Plan/Prediction is written *before* reviewing your own diff, not after. If it touches rendered UI (Django admin, report card images, WhatsApp message/button rendering), attach a screenshot/GIF/video — see the PR Readiness checklist.
3. **Tag a reviewer** in GitHub Discussions → Pull Requests (skip if the PR already tags one directly) — after "Before requesting review" above, not before.
4. **Approving reviewers: Brighton, Omar, Kundai, or Munashe** — one approval before merge is expected. Madrena is not a required/approving reviewer (see "Working with Madrena") — she can still read PRs and ask questions as part of learning. **This is convention, not GitHub-enforced:** branch protection and its rulesets replacement are both unavailable on this org's plan for a private repo (same root cause as the wiki being unreachable, see #36) — nothing technically stops a merge without review or with a failing `unit_tests` check, only the team actually following this step. See #373 for the full reasoning and the options considered. The one technical backstop is at deploy time, not merge time: the production deploy refuses to run unless `unit_tests` passed on the released commit (#379) — a bad merge still lands on main and still reaches staging.
5. **Merge** once approved — squash merge, branch deleted.
6. **Staging auto-deploys** on every push to main (GitHub Actions, no manual step).
7. **Production deploy** — Brighton publishes a GitHub Release, which triggers auto-deploy. The deploy aborts unless the `unit_tests` check passed on the released commit — see `docs/Deployment.md` if a release is blocked. Never deploy to prod without Brighton's explicit sign-off.

**Releases:** semver, only Brighton publishes; commands in `docs/Deployment.md`. Shop owner would
notice → minor; wouldn't → patch; major means scaling beyond the pilot.

## Deployment

`USE_MOCK_WHATSAPP` must **not** be `True` on production — if it is, the bot silently swallows outbound messages instead of sending them. Confirm before every prod deploy.

**Prod Fly secret flips that change user-facing behavior** (e.g. `USE_TEMPLATE_MESSAGES`) go through the same staging-first discipline as code deploys — a secret change is not exempt from verification just because it isn't a code push. #237 was a real month-long silent regression caused by exactly this gap: a flag flip changed behavior for shops it wasn't meant to touch, and nothing caught it until a manual audit.

Before flipping one in prod: set it on staging and verify the actual user-facing outcome (for `USE_TEMPLATE_MESSAGES`, a shop *within* the 24h window still gets the image card), note which shops are in vs. out of scope, then re-confirm both after the prod flip. Routine key rotation with no logic change is exempt.

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

## Keeping this file current

Source of truth for any AI assistant here. Update it when the phase, a convention, or the team
changes. Doc updates ship in the same PR as the change they affect, never as a follow-up.
