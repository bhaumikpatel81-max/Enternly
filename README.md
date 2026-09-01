# Enternly (One Click Hire)

A multi-tenant recruitment-to-onboarding platform: requisition → sourcing → AI resume screening → Enteri AI voice/avatar interview → panel scorecards → offer approval chain → background verification → preboarding → onboarding → HRMS sync. Built for EnternsTech, now operated as SaaS with a platform-admin control plane serving multiple tenant companies.

For the full technical breakdown — every backend router and service, the database migration history, auth/multi-tenancy design, background workers, and all external integrations — see **[TECHSTACK.md](TECHSTACK.md)**.

Other docs in this repo:

- [ARCHITECTURE.md](ARCHITECTURE.md) — the original Phase 1 design doc (schema + roadmap); superseded in detail by TECHSTACK.md but kept for history.
- [DESIGN_SPEC.md](DESIGN_SPEC.md) — frontend layout spec.
- [AVATAR_PROCTORING_SPEC.md](AVATAR_PROCTORING_SPEC.md) — AI avatar + consent-gated proctoring design.
- [HANDOFF.md](HANDOFF.md) — build handoff notes.
- `PLATFORM_ADMIN_*.md`, `COMPANY_SUPERADMIN_MAPPING.md` — platform-admin/multi-tenancy audit and mapping notes.

## Tech stack

- **Backend:** Python 3.12, FastAPI, Uvicorn
- **Database:** PostgreSQL 16 via raw `psycopg2` (no ORM)
- **Auth:** JWT (`python-jose`) + `bcrypt`, audience-separated tokens for staff/vendor/candidate
- **Frontend:** Plain HTML/CSS/JS, no build step, served as static files by FastAPI
- **AI:** Groq (conversational interviewer + CV enrichment), OpenAI Whisper (speech-to-text), edge-tts/gTTS (text-to-speech), SadTalker (avatar lip-sync, GPU microservice)
- **Integrations:** Google Calendar/Meet/Gmail, LinkedIn OIDC, Darwinbox, pluggable BGV vendor, pluggable HRMS providers (SuccessFactors, Workday, Oracle HCM, Zoho People, BambooHR, GreytHR)
- **Infra:** Docker (backend container), PostgreSQL runs natively on the host in production

See [TECHSTACK.md](TECHSTACK.md) for the complete picture.

## Getting started (local dev)

```bash
docker compose -f docker-compose.dev.yml up
```

This starts Postgres 16 (seeded automatically on first boot) and the FastAPI backend with hot-reload at `http://localhost:8080`. The app applies any newer schema changes automatically on startup — no manual SQL step required beyond the initial container boot.

Production uses `docker-compose.prod.yml`, which runs only the backend container against a PostgreSQL instance already running on the host — see `.env.prod` for the required configuration keys (documented in [TECHSTACK.md §12](TECHSTACK.md#12-environment-variables-envprod)).

## Repository layout

```text
backend/    FastAPI app — routers/ (HTTP layer), services/ (business logic), gpu-services/ (SadTalker avatar microservice)
frontend/   Static HTML/JS pages — staff, hiring-manager, candidate, vendor, and platform-admin portals
database/   Numbered SQL migration history (01–72)
scripts/    CV inbox watcher + one-off seed/import scripts
cv_inbox/   Drop folder for bulk CV ingestion
```

## Test and Deploy

CI/CD runs on GitHub Actions — add workflow files under `.github/workflows/` when ready.
