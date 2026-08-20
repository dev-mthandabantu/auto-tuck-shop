# Pilot Methodology

**This page is not live pilot status — it's the reusable measurement framework from Cohort 1, kept for the next cohort.** For what's actually happening now, see:

- **Current status / who's working on what:** [discussion #304](https://github.com/aakitech/auto-tuck-shop/discussions/304)
- **This week's priorities:** [business-operations discussion #23](https://github.com/aakitech/business-operations/discussions/23)
- **What Cohort 1 actually concluded:** [discussion #167 — Pilot snapshots](https://github.com/aakitech/auto-tuck-shop/discussions/167), specifically "Snapshot 5 + Retro — Day 31" (26 Jun 2026)

## Cohort 1 — where the record lives

Cohort 1 ran 25 May – late Jun 2026. It was a feasibility test, not a fully resourced pilot — no founder or trusted coordinator had ground presence in any shop's geography for most of it. The record is spread across several discussions rather than one place:

| Discussion | Content |
|---|---|
| [#167](https://github.com/aakitech/auto-tuck-shop/discussions/167) | Live snapshots (5 of them) + the final retro. **Start here** — it's the conclusion. |
| [#97](https://github.com/aakitech/auto-tuck-shop/discussions/97) | Original onboarding brief for Madrena/Bradley — metrics framework and recruiting guidance (points into issue #27) |
| [#155](https://github.com/aakitech/auto-tuck-shop/discussions/155) | Outreach scripts for Madrena's 24 cold Harare contacts — what worked, what didn't (mismatch: mostly fashion/boutique, wrong segment) |
| [#182](https://github.com/aakitech/auto-tuck-shop/discussions/182) | Field comms debrief — per-shop actionables for Bradley, Munyaradzi currency audit |
| [#163–166](https://github.com/aakitech/auto-tuck-shop/discussions/163) | Madrena's field journal — first-person notes from outreach |

**The retro's headline conclusions** (full detail in #167):
- The product works where it fits: FMCG/tuck/butchery/eatery shops with daily high-frequency sales. Not fashion, not services, not friend-of-a-friend networks — Madrena's 24 Harare contacts were mostly fashion boutiques and never really activated.
- Ground presence is the real constraint, not the product. Bradley's Rimbi/Chipinge shops (Mtetwa, Dhibi) outperformed because he has real relationships there.
- **Next cohort plan:** a structured cohort of ~5 vetted FMCG shops in Bradley's Rimbi/Chipinge network, onboarded in the same week — tracked under the [🌾 Next Cohort (Chipinge/Rimbi) milestone](https://github.com/aakitech/auto-tuck-shop/milestones). Currently paused — team is prioritizing product stabilization and fundraising instead (as of Aug 2026).

## Success metrics framework (reusable)

These definitions held up in Cohort 1 and are worth reusing as-is for the next cohort, rather than re-deriving them.

### 1. Activation rate

**Definition:** % of onboarded shops that record a second sale within 7 days of their first one.

One sale could be curiosity. Two means the user found value and came back — the first real signal the product is working.

### 2. Weekly retention

**Definition:** % of originally onboarded shops that sent at least one sale message this week.

| Checkpoint | Target | Interpretation |
|---|---|---|
| Week 1 | ≥70% | Expected — most people try something new in the first week |
| Week 2 | ≥50% | The real test — novelty has worn off |
| Week 4 | ≥40% | Habit is forming — this is the PMF signal |

If week 2 drops below 30%, something is broken — investigate immediately, don't wait for a scheduled retro.

*PMF = Product-Market Fit: when a product is solving a real problem for real people in a way that makes them want to keep using it.*

### 3. Bot accuracy (mistake rate)

**Definition:** `"Fix mistake" clicks ÷ total confirmed sales × 100`

**Target: <15%.** Above 20%, shop owners lose trust in the bot's replies and stop using it — not because it's hard, but because they don't believe it's recording correctly.

### 4. Core action frequency

**Definition:** Average sale messages sent per *active* user per week.

No fixed target — establish a baseline per cohort. A tuck shop makes many sales per day; 2 messages/week means the bot isn't their real record. 15–20 messages/week is genuine adoption — worth understanding everything about those users.

### 5. Pull signal (qualitative)

**Definition:** Did any shop owner, unprompted, ask about a feature, report a problem they want fixed, or mention the bot to another shop owner?

**Target: at least one pull signal per cohort by end of week 2.** Its absence after two weeks is a concern — it's the strongest early signal of genuine demand versus polite compliance.

## Review cadence (template for next time)

**Daily:** Check `/admin/pilot-metrics/`. Any shop onboarded >3 days with zero messages gets flagged to whoever has the ground relationship for a light-touch check-in.

**End of week 1:** Compare activation numbers. What % hit the second-sale threshold?

**End of week 2 — formal retro.** Whoever has ground relationships checks in with every active user and asks:

1. **"When do you normally send the message — right after the sale, or at the end of the day?"** Identifies what triggers the behaviour.
2. **"Has the bot ever gotten your sale wrong? What happened?"** Surfaces accuracy problems that weren't flagged in-app — some users accept wrong records rather than tapping "Fix mistake."
3. **"If the bot stopped working tomorrow, what would you do?"** The adapted Sean Ellis test. "I'd go back to my notebook" is informative. "I'd be stuck" is the pull signal in its strongest form.

## Lessons for next time (from the Cohort 1 retro)

- **Segment discipline matters more than volume.** Madrena's 24 cold Harare contacts were mostly the wrong segment (fashion/boutique) and largely didn't convert — not a product failure, a targeting one. Vet for daily-high-frequency-sales businesses before outreach, not after.
- **Ground presence is the actual bottleneck**, not engineering capacity. The shops that worked (Mtetwa, Dhibi) worked because someone with a real relationship was physically near them. The next cohort should only expand into a geography where that's true.
- **Bugs hit during onboarding are retention killers, not minor issues.** A bug that goes unfixed for days while a newly-onboarded shop is hitting it (e.g. #190, which lost Mai Ati) does outsized damage relative to the same bug found by an established user. Prioritize accordingly during any onboarding window.
- **Meta template messaging is load-bearing infrastructure**, not a nice-to-have — without it, shops that go quiet stay quiet (no way to reach them outside the 24h window).
