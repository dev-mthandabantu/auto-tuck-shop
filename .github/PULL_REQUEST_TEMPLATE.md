## What & Why

<!-- Link the issue. Explain the user/product problem and what changed. -->

Fixes #

## Plan / Prediction

<!-- Write this BEFORE reviewing your own diff. For tiny changes, write "Small change; not needed."
     Files matter less here than data: several real ATS bugs were never "touched the wrong file" —
     they were a value silently dropped crossing a function boundary inside the right file
     (e.g. a caption captured at the webhook, then never threaded through the four calls it takes
     to reach the parser — see #284/PR #286). Naming the data flow is what would have caught that. -->

- Data flowing in: what values come in, from where (webhook payload, user reply, LLM response)?
- Where each value needs to still be alive by the end: which function/response actually needs it?
- Main risks:
- Intentionally untouched:

## Implementation Notes

<!-- Briefly explain the chosen approach and any alternatives considered for non-obvious decisions. -->

## PR Readiness

- [ ] I manually reviewed the diff and relevant surrounding code
- [ ] Every changed file is intentionally in scope
- [ ] I can explain the affected user flow, architecture, risks, and validation in my own words
- [ ] The actual diff matches what I predicted above — if it doesn't, I've explained why in Implementation Notes

<!-- High-risk categories, not an exhaustive file list — ask "does this change touch one of these?":
     - Message dispatch/routing (webhook_handler.py, views.py, message_parser.py): a dropped field or unhandled branch here means the bot goes silent (#284)
     - Anything that writes to Sale or product price records, at any stage including pre-confirmation (sale_handler.py, services/sales): a bad write here means a wrong money record, possibly before the owner even confirms (#287)
     - Migrations touching money fields specifically (Sale.status, unit_price, total_amount, or similar) — these have each been altered more than once after creation, and a migration can only be patched forward, never edited or undone once committed. Treat these with the same weight as a live money-write bug, not as a routine schema change.
     - Access control — who a message from a given number is treated as (assistant add/remove, _lookup_sender): (#285)
     - Shona/English locale strings and language routing: (#235, #242)
     - Other migrations, prod env vars / USE_MOCK_WHATSAPP
     If your change touches a risky area not listed here, say so anyway — the category matters more than the list being complete. -->

## Testing / Validation

<!-- List exact commands, manual checks, or explain why a check could not run. -->

- [ ] Unit tests: `python manage.py test unit_tests`
- [ ] Manual/product check: tested via mock WhatsApp UI or a real test shop — describe what you sent and what came back
- [ ] If parsing/prompt behavior changed: added or updated a regression test in `unit_tests/test_shona_parsing.py`

## Risk & Rollback

<!-- What could go wrong after deploy? How would we detect it (Sentry, pilot_audit, a shop owner report)? How would we roll back? For low-risk changes, say so. -->

## Docs Impact

- [ ] This PR does not require a docs update
- [ ] This PR requires a docs update — done in this PR
- [ ] This PR requires a docs update — follow-up issue linked here: #

<!-- See "Docs index" in CLAUDE.md. Update the relevant file in docs/ in the same PR as any behaviour change. -->
