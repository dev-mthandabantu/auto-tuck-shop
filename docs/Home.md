# Auto Tuck Shop

An AI-powered WhatsApp bot that helps small shop owners (tuckshops) in Southern Africa record sales by sending a text or voice message.

> "Sold 2 cokes $1 each and 3 breads $0.50" → sale recorded, confirmation sent.

## Pages

**For the team**
- [Pilot Launch](Pilot-Launch) — launch sequence, team roles, success metrics, Bradley's field guide

**Technical reference**
- [Architecture](Architecture) — apps, data models, message flow, async execution
- [Features](Features) — full feature list grouped by area
- [Integrations](Integrations) — external services and how they're wired in
- [Deployment](Deployment) — environments, Fly.io config, CI/CD workflow

## Tech Stack

| Layer | Technology |
|---|---|
| Framework | Django 5.0 |
| Database | PostgreSQL (production), SQLite (development) |
| Hosting | Fly.io |
| Messaging | Meta WhatsApp Business API |
| AI / LLM | OpenRouter → Gemini 2.5 Flash |
| Voice transcription | ElevenLabs Scribe |
| Media storage | Cloudflare R2 |
| Error tracking | Sentry |

## Target Market

Zimbabwe, South Africa, Botswana. Supports ZWG, ZAR, BWP, USD, EUR, GBP. UI language: English and Shona.
