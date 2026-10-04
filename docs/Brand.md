# Brand

One place for the logo, colours and tone, so the LinkedIn page, the public site and any deck draw from the same set. First version, agreed in #396. The LinkedIn plan lives in discussion #399.

## Logo files

Everything is in `static/auto_tuck_shop/img/brand/`.

| File | Use |
|---|---|
| `lockup-on-cream.png` | Shop icon next to the name, on cream |
| `wordmark-green.png` | Name only, dark green, transparent background |
| `wordmark-cream.png` | Name only, cream, transparent background |

`static/auto_tuck_shop/img/app-icon.png` is the app icon and stays as it is.

The wordmarks have transparent backgrounds: use the green one on light backgrounds and the cream one on dark ones. The lockup has a cream background. All three are rendered from text in Inter ExtraBold, the site's font, so ask Brighton for a re-render rather than editing the images.

Spelling: the name is **Auto Tuckshop**, with "Tuckshop" as one word, matching the WhatsApp display name. Use it everywhere: logos, the site, the bot's messages and posts. Older text that says "Auto Tuck Shop" is being updated (#396).

## Colours

Already in use on the public site (`static/auto_tuck_shop/css/public.css`, `:root`).

| Colour | Hex | CSS variable | Use |
|---|---|---|---|
| Green | `#123c2d` | `--ats-green` | Dark panels and backgrounds |
| Cream | `#fff3df` | `--ats-cream` | Light base, and text on dark |
| Terracotta | `#bd5f3d` | `--ats-terracotta` | Accent |
| Maize | `#f1b84a` | `--ats-maize` | Highlights and labels |

If a new design needs a colour that isn't here, check `public.css` first. Don't invent a fifth.

## What Auto Tuckshop is

A WhatsApp bot that records sales for small shops. The owner sends a message the way they would say it, as text, a voice note or a photo of a tally sheet. The bot works out what was sold, sends back a summary, and saves the sale only after the owner taps Looks good.

The idea behind it: software for informal retail has to fit how shops already work, instead of asking owners to learn a new tool. WhatsApp, Shona and mixed-language messages, and confirm-before-save are the proof of that idea. Show them as evidence, not as the headline.

## What it isn't

- Not an advert. Posts should be about the problem, not a pitch.
- Not a dashboard-first bookkeeping app.
- Not "AI on WhatsApp for small businesses" as a slogan. That pitch is crowded and doesn't say what's different here.
- Not a tool that lets AI write money records on its own. The owner confirms every sale.

## Who we write for

Funders, African tech founders and operators, and engineers and future hires. Not shop owners. After a handful of posts, a reader should think: these people understand small retailers, they test their assumptions, and I want to see what they build next.

## Tone

- Write like a person explaining it to a colleague. No "thrilled to announce", "revolutionising" or "empowering".
- One idea per post. Short paragraphs, one to three lines each.
- Use a real message, a screenshot or a decision the team made as evidence. Say what didn't work, because it builds more trust than a highlight reel.
- Show messy input on purpose: Shona and English mixed, voice notes, several currencies in one message.

## Staying honest about the pilot

Production has been live since 25 May 2026. The pilot has been paused since 3 August, and no new shops are being onboarded.

- Talk about Cohort 1 in the past tense. Never "we're testing" or "we're piloting".
- No pilot numbers.
- Name roles by function, not by title or headcount.
- Nothing that implies the pilot is running.
- Describe the evening summary card as a feature. Daily summaries are off while the pilot is paused, so don't say owners are getting them now.

## Avoid

Stock photos, AI robot graphics, motivational quotes, and "Africa rising" imagery that has nothing to do with the product.

## Approval

For the first month, every LinkedIn post is approved by Brighton or Kundai before it goes out. Drafts go in discussion #399.
