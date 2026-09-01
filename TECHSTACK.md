# Enternly — Tech Stack & System Reference

This document is the technical map of the repository: every layer, every module, every integration, and how they fit together. It reflects the code as it actually stands (checked against `backend/app/main.py`, the routers/services directories, and the `database/` migrations), not the original Phase‑1 plan in `ARCHITECTURE.md`, which the system has since grown well beyond — it is now a multi‑tenant ATS + HRMS platform ("One Click Hire" / Enternly), not a single‑company prototype.

---

## 1. What the system is

A recruitment‑to‑onboarding platform, originally built for EnternsTech, now operated as a **multi‑tenant SaaS** with a platform‑admin control plane on top. It covers the full employee lifecycle:

`Requisition → Sourcing/CV intake → AI screening → Enteri AI voice/avatar interview → Panel interview & scorecards → Offer & approval chain → Documentation → BGV (background verification) → Preboarding → Onboarding → HRMS sync`

Distinct portals exist for staff (recruiters/admins/HR), hiring managers, candidates, vendors (sourcing agencies), and platform admins (the SaaS operator).

---

## 2. Tech stack at a glance

| Layer | Choice |
| --- | --- |
| Backend language/framework | Python 3.12, **FastAPI** 0.110 on **Uvicorn** 0.29 (`workers=8` in prod) |
| Database | **PostgreSQL 16**, accessed via raw `psycopg2` (`ThreadedConnectionPool`) — **no ORM** |
| Auth | **JWT** (`python-jose`, HS256) + `bcrypt` password hashing, hand‑rolled role/tenant middleware |
| Frontend | **Vanilla HTML/CSS/JS**, no framework, no build step, no `package.json` — plain `<script>` tags served as static files by FastAPI |
| File storage | Local disk (`cv_store/`, `jd_store/`, `proctoring_uploads/`) with **Google Cloud Storage** as the durable/production target for CVs, recordings, and avatar videos; proctoring media also lands as Postgres `BYTEA` for replica‑safety |
| AI / LLM | **Groq** (LLM inference) for the Enteri AI conversational interviewer and resume enrichment; **OpenAI** (Whisper) for speech‑to‑text; `edge-tts` / `gTTS` for text‑to‑speech |
| Avatar rendering | **SadTalker** (open‑source lip‑sync) on a separate GPU microservice, orb (canvas/SVG) as the zero‑cost fallback |
| Document processing | `pypdf`, `PyMuPDF`, `python-docx`, `mammoth` (.doc→.docx via LibreOffice headless), `openpyxl`, `reportlab` |
| External integrations | Google Calendar/Meet & Gmail (OAuth2), LinkedIn OIDC, SMTP (via Gmail or generic SMTP), Darwinbox (offer release, stubbed), pluggable BGV vendor, pluggable HRMS providers (SuccessFactors, Workday, Oracle HCM, Zoho People, BambooHR, GreytHR) |
| Containerization | **Docker** — one `backend/Dockerfile` (Python + LibreOffice), Postgres runs natively on the host in prod (not containerized), `docker-compose.dev.yml` for local dev, `docker-compose.prod.yml` for the deployment pipeline |
| Background jobs | In‑process `asyncio` workers started at FastAPI startup (no Celery/cron/external queue) |

---

## 3. Repository layout

```
Enternly/
├── docker-compose.prod.yml     # prod orchestration (backend only; Postgres runs on host)
├── docker-compose.dev.yml      # local dev orchestration (Postgres container + hot-reload backend)
├── .env.prod                   # secrets/config template — never commit real values
├── backend/
│   ├── Dockerfile              # python:3.12-slim + LibreOffice, non-root user, healthcheck
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py             # app assembly, router mounts, auth middleware, startup auto-migrations, bg workers
│   │   ├── db.py                # psycopg2 connection pool + query/query_one/transaction helpers
│   │   ├── auth_utils.py       # JWT issue/verify, bcrypt, role & tenant-tier checks, auth dependencies
│   │   ├── module_access.py    # per-recruiter delegated module access + tenant-wide module gating
│   │   ├── login_rate_limit.py # shared login throttling for staff & platform-admin auth
│   │   ├── routers/            # 39 files — one per domain, HTTP boundary only
│   │   └── services/           # 44 files — business logic, external calls, background workers
│   ├── gpu-services/            # standalone SadTalker avatar microservice (separate GPU host)
│   ├── scripts/                 # one-off seed/import scripts (no-poach, HRBP, market intel)
│   ├── uploads/, media/, proctoring_uploads/  # local runtime storage
├── frontend/                    # static HTML/CSS/JS, no build step, served by FastAPI
├── database/                    # 01–72 numbered SQL migration files (see §7)
├── cv_inbox/                    # drop folder for bulk CV ingestion (watched by scripts/enternly_watcher.py)
└── scripts/enternly_watcher.py  # filesystem watcher for cv_inbox/
```

---

## 4. Backend architecture

### 4.1 Request flow
`routers/` are the HTTP boundary only — they validate input and call into `services/`, which hold all business logic and never touch FastAPI `Request`/`Response` objects. All SQL lives in routers/services directly (no repository layer, no ORM); `db.py` just exposes `query`, `query_one`, `transaction`, `tx_exec` over a lazily‑initialized `ThreadedConnectionPool`.

### 4.2 Auth & multi-tenancy
- JWTs (`python-jose`, HS256, one shared secret) carry an **audience claim** — `AUD_STAFF`, `AUD_VENDOR`, or `AUD_CANDIDATE` — so a candidate or vendor token can never be replayed as a staff token even though the signing key is shared.
- A global `@app.middleware("http")` enforces auth on every route except an explicit public allowlist (candidate/vendor public links, OAuth callbacks, and HMAC‑signed vendor/HRMS/BGV webhooks).
- Role checks (`is_company_tier`, `is_platform_tier`, `require_company_admin`, `require_platform_admin`, etc.) live in `auth_utils.py` alongside the JWT helpers.
- **Multi‑tenancy was retrofitted**, not designed in from day one: migration `57_platform_admin_and_tenancy.sql` adds a `tenant` table and backfills a `tenant_id` column onto every pre‑existing table (seeded to one fixed legacy tenant UUID), then tightens it to `NOT NULL`. Every table created afterward has `tenant_id` from birth. Isolation is enforced **in application code** (`WHERE tenant_id = %s` on every query) — there is no Postgres Row‑Level Security.
- `module_access.py` implements two independent gating mechanisms:
  - **Per‑recruiter delegation** — a Company Admin can hand one specific recruiter access to an otherwise admin‑only module (vendors, form fields, SLA settings, email templates, etc.), off by default, never including user/role management itself.
  - **Tenant‑wide module gating** — 14 newer feature modules (campus hiring, Enteri AI tracker, KPI dashboard, gamification, proctoring review, hiring plan, CV repository, AI scorecard, no‑poach, documents, BGV, preboarding, onboarding, HRMS) are switched on/off per tenant via `require_tenant_module`, tied to the tenant's subscription plan (`module_catalog`, `tenant_module_config`, `subscription_plan_config`).
- A dedicated **Platform Admin console** (`platform_auth_api.py`, `platform_admin_api.py`) sits outside normal tenant scoping — it can create/manage tenants, toggle their modules, manage subscriptions, and impersonate for support.

### 4.3 Schema migrations
There is no formal migration tool (no Alembic). `database/*.sql` files are the historical, numbered record of schema evolution, but the **live source of truth is `main.py`'s `_auto_migrate()`**, run on every FastAPI startup: an idempotent list of `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` statements so a fresh pull never requires a manual `psql` step. Some tables (e.g. `nexai_session`, `proctoring_session`) exist only in this in‑code list, not in a matching numbered `.sql` file — the `.sql` files are kept as documentation of intent but are not what actually runs against a live database.

### 4.4 Background workers
No Celery, no cron, no external queue. Every long‑running job is a plain `asyncio` task launched at FastAPI startup and tracked via `_track_bg_task` (visible through a `bg_task_status:<name>` key and a watchdog) so a crashed worker doesn't silently disappear:

| Worker | Purpose |
| --- | --- |
| `campus_email_worker.py` | Sends queued campus‑drive Enteri AI invite emails in throttled batches |
| `cv_enricher.py` | Background LLM enrichment of CV Repository entries (skills/experience) |
| `email_ingest.py` | Gmail API OAuth poller that pulls CVs into the repository |
| `recruiter_email_worker.py` | Per‑recruiter Gmail IMAP mailbox scan for incoming CVs |
| `enteri_ai_render_worker.py` | Retries stuck/failed Enteri AI avatar pre‑renders |
| `hm_feedback_reminder_worker.py` | Nags hiring managers until overdue interview feedback is submitted |
| `linkedin_reminder_worker.py` | 6‑monthly LinkedIn profile refresh reminder to candidates |
| `preboarding_proposer_worker.py` | Daily sweep auto‑proposing preboarding cases near a candidate's joining date |

Pattern used throughout: infinite loop + idle sleep, `FOR UPDATE SKIP LOCKED` claim‑then‑send to avoid double work across replicas/restarts, batch size + backoff constants, and a blanket `try/except` so one worker's failure never takes down the app.

---

## 5. Backend routers (`backend/app/routers/`)

| File | Route prefix | Purpose |
| --- | --- | --- |
| `auth.py` | `/api/auth` | Staff login, `/me`, change password, rate‑limited |
| `admin_users.py` | `/api/admin` | Company Admin: user CRUD, module‑access grants, form‑field config, system settings |
| `password_api.py` | `/api/auth` | Self‑service set/forgot/reset password across staff, vendor, and candidate accounts |
| `pipeline_api.py` | `/api` | Core pipeline: dashboard, requisition CRUD, kanban, candidates, interviews, HM review |
| `reports_api.py` | `/api/reports2` | 8 legacy management report pivots (TA/recruiter/HM) + Excel export |
| `custom_reports_api.py` | `/api/custom-reports` | Ad‑hoc report builder over a fixed, allowlisted semantic layer (no raw SQL from clients) |
| `enteri_ai_api.py` | `/api/enteri-ai` | Enteri AI voice/avatar interview bot: sessions, question generation, scoring, invites, transcripts |
| `proctoring_api.py` | `/api/proctoring` | Consent, identity snapshot, webcam/screen chunk upload, AI flags, human review |
| `tickets_api.py` | `/api` | Support ticket raise/list/resolve + admin system‑health |
| `scorecard_api.py` | `/api` | Panel interview scorecards, PDF export, aggregated panel feedback |
| `email_template_api.py` | `/api/email-templates`, `/api/applications/...` | Email template CRUD + manual/bulk send |
| `offers_api.py` | `/api` | Offer creation + sequential multi‑step approval chain workflow |
| `sla_api.py` | `/api/sla` | SLA / RAG (red‑amber‑green) config + breach dashboard |
| `chain_templates_api.py` | `/api/offer-chain-templates` | Reusable named offer‑approval chain templates |
| `documentation_api.py` | `/api/applications` | Offer‑document collection + salary negotiation log |
| `kpi_api.py` | `/api/kpi` | Role‑scoped KPI dashboard (funnels, cards, charts) |
| `hiring_plan_api.py` | `/api/hiring-plan` | Budget‑sheet import, manual rows, requisition linking, demand‑vs‑fulfilled view |
| `cv_api.py` | `/api/cv` | CV Repository: bulk ingest, search, file serving, email‑scan, API‑token access |
| `cv_match_api.py` | `/api/cv/{id}/scorecard` | On‑demand AI resume‑vs‑JD scorecard for repository entries |
| `no_poach_api.py` | `/api/no-poach` | No‑poach company list + matching‑applications view |
| `hm_api.py` | `/hm/...` | Hiring Manager dashboard + TA approval/reject workflow for requisitions |
| `campus_bulk_api.py` | `/api/campus` | Campus/fresher bulk‑invite Excel upload, batch tracking, public resume upload |
| `vendor_api.py` | `/api/vendors` | Vendor CRUD (internal) + vendor portal (CV submission, requisition visibility) |
| `candidate_portal_api.py` | `/api/candidate` | Candidate login, applications, apply, profile, feedback (score fields hard‑blocked) |
| `gamification_api.py` | `/api/gamification` | Points, tiers, badges, leaderboard, daily HR trivia question |
| `bands_api.py` | `/api/bands` | Criticality "band" CRUD mapped to Group Companies |
| `org_api.py` | `/api/org` | Group Company / Business Unit CRUD |
| `client_api.py` | `/api/clients` | CRUD for external clients an RPO/staffing tenant hires on behalf of |
| `hrbp_api.py` | `/api/hrbp` | HRBP lookup + read‑only scoped requisition/candidate visibility |
| `scheduling_api.py` | `/api/scheduling` | Calendly‑style HM self‑scheduling: slot proposal, public confirm link, ICS invites |
| `activity_log_api.py` | `/api/activity-log` | Read‑only chronological activity timeline per entity |
| `notifications_api.py` | `/api/notifications` | Recipient‑scoped notification bell/dropdown |
| `google_calendar_api.py` | `/api/google` | Admin connect/disconnect/status for shared Google Calendar OAuth; public OAuth callback |
| `platform_auth_api.py` | `/api/platform` | Dedicated platform‑admin login |
| `platform_admin_api.py` | `/api/platform` | Cross‑tenant control plane: tenant CRUD, module toggles, subscriptions, impersonation |
| `document_api.py` | `/api/documents` | Document Collection & Verification (staff request/verify, candidate upload) |
| `bgv_api.py` | `/api/bgv` | Background Verification case management + HMAC‑authenticated vendor webhook |
| `preboarding_api.py` | `/api/preboarding` | Preboarding case lifecycle, policy content/ack, asset‑allocation requests |
| `onboarding_api.py` | `/api/onboarding` | Day‑1 conversion to `employee_master` + employee record read |
| `hrms_api.py` | `/api/integrations/hrms` | Multi‑provider HRMS sync + vendor webhook |

---

## 6. Backend services (`backend/app/services/`)

| File | Purpose |
| --- | --- |
| `pipeline.py` | Core application state machine — advances candidates through stages, writes `stage_event` rows |
| `screening.py` | AI + rule‑based candidate scoring (keyword/experience/AI‑fit/stability; separate fresher model) |
| `cv_parser.py` / `resume_parser.py` | Synchronous resume text extraction (PDF/DOCX/DOC) and contact‑info normalization |
| `cv_enricher.py` | Background Groq‑LLM enrichment of CV Repository entries (skills/experience) |
| `cv_ingest.py` | Shared ingest helper (hash dedup, extract, auto‑map, store) used by bulk upload & email pollers |
| `candidate_dedup.py` | Shared candidate dedup by email/normalized phone across every intake path |
| `candidate_profile_parser.py` | LLM resume‑to‑structured‑profile parser for candidate‑portal prefill |
| `connectors.py` | Google Calendar/Meet real integration, ICS‑over‑SMTP invites, CV attachment reader |
| `google_calendar.py` | Per‑tenant Google Calendar OAuth + Meet link creation, soft‑fails to a static link |
| `cv_email_scan.py` / `email_ingest.py` / `recruiter_email_worker.py` | Two parallel CV‑from‑email paths: Gmail IMAP scan and Gmail API OAuth poll |
| `email_templates.py` / `email_layout.py` / `jd_email.py` | Template rendering, shared branded HTML layout, JD placeholder resolution |
| `email_validation.py` | Shared email format + disposable/test‑domain rejection |
| `interviewer_llm.py` | LLM‑driven conversational Enteri AI interviewer (Groq) — next‑turn generation + transcript scoring |
| `stt.py` | Speech‑to‑text via OpenAI Whisper for conversational interviews |
| `tts.py` | Text‑to‑speech via `edge-tts` (primary) / `gTTS` (fallback), gendered voice config |
| `avatar.py` | Swappable avatar provider interface: `orb` / `sadtalker` / `wav2lip` / `vendor` |
| `prerender.py` | Full avatar pipeline orchestration: TTS → ffmpeg → SadTalker GPU service → GCS/local storage, cached by hash |
| `enteri_ai_render_worker.py` | Background retry sweep for stuck/failed avatar renders |
| `proctoring_scorer.py` | Server‑side replay of the browser's proctoring strike‑scoring logic |
| `proctoring_storage.py` | Proctoring media stored as Postgres `BYTEA` (replica‑safe, not local disk) |
| `proctoring_alerts.py` | Best‑effort integrity‑flag digest emails to recruiters |
| `bgv_connectors.py` | BGV vendor connector: initiate case + parse HMAC‑verified webhook, stub/real switch |
| `hrms_connectors.py` | HRMS adapters for SuccessFactors / Workday / Oracle HCM / Darwinbox / Zoho People / BambooHR / GreytHR |
| `linkedin_oauth.py` | "Sign in with LinkedIn" OIDC identity verification (no scraping) |
| `linkedin_reminder_worker.py` | Periodic LinkedIn profile‑refresh reminder |
| `gamification.py` | Append‑only points ledger — award/score/tier/badge derivation, tenant‑scoped |
| `notifications.py` | Recipient‑scoped live notification writer (separate from audit‑only activity log) |
| `activity_log.py` | Generic best‑effort audit‑log writer |
| `report_catalog.py` / `report_query_builder.py` / `report_scope.py` | Allowlisted semantic layer + safe parameterized query builder + role‑based row scoping for all reporting |
| `excel_export.py` / `pdf_export.py` | Shared openpyxl/reportlab export helpers reused by every reporting endpoint |
| `sla.py` | SLA/RAG threshold computation, single source of truth for pipeline stage constants |
| `period.py` | Period‑string (weekly/monthly/quarterly) → SQL date‑range helper |
| `source_labels.py` | Human‑readable channel labels for candidate/application source |
| `campus_email_worker.py` | Throttled batch sender for campus‑drive Enteri AI invites |
| `hm_feedback_reminder_worker.py` | Reminder loop for overdue hiring‑manager interview feedback |
| `preboarding_proposer_worker.py` | Daily auto‑proposal of preboarding cases nearing joining date |

---

## 7. Frontend (`frontend/*.html`)

No build tooling — plain HTML files with inline/linked vanilla JS, served directly as static files by FastAPI. Auth token stored in `localStorage`.

| Page | Audience | Purpose |
| --- | --- | --- |
| `index.html` | Internal staff (recruiter, TA manager, admin, HM, HRBP, etc.) | Main SPA shell: dashboard, pipeline kanban, admin, reports |
| `login.html` | Internal staff | Staff sign‑in |
| `platform-admin.html` | Platform admin (Enternly operator) | Cross‑tenant console: tenants, modules, subscriptions |
| `platform-login.html` | Platform admin | Dedicated platform‑admin sign‑in |
| `candidate-portal.html` | Candidate | Self‑service portal — applications, profile, feedback |
| `vendor-portal.html` | Vendor / sourcing agency | Submit CVs, track submissions |
| `interview.html` | Candidate | Enteri AI voice/avatar interview UI with proctoring hooks |
| `schedule.html` | Candidate (public, tokenized link) | Confirm an HM‑proposed interview slot |
| `reschedule.html` | Candidate (public, tokenized link) | Reschedule a confirmed interview |
| `set-password.html` | Staff / vendor / candidate | Shared first‑time‑set / reset‑password page |

---

## 8. Database (`database/*.sql`, 01–72)

There is no ORM and no formal migration runner — these numbered files are the historical record; the same statements are re‑applied idempotently at startup (see §4.3). Grouped by era:

- **01–02** — Core schema & seed: group companies, business units, bands, `app_user`, `requisition`, `candidate`, `application`, `interview`, `scorecard`, `approval_chain`, `offer`, `email_template`, `stage_event`.
- **03–05** — Early auth columns, reporting views, meeting notetaker (recordings/transcripts), Google OAuth token storage.
- **06–11** — HM seed data & feedback, support tickets + login log, CTC‑split fields.
- **12–19** — Enteri AI buildout: invites, question customization, dedup, conversational mode, email‑sent tracking, candidate‑email uniqueness, proctoring completion flags, avatar pre‑render cache.
- **20–24** — Proctoring hardening (termination, appeals), real AI screening columns, extended application fields, scorecard draft/submit status.
- **25–30** — Offer approval chains, custom email templates, hiring plan, HM requisition approval, per‑requisition scoring weights, CV/application source tracking.
- **31–34** — Vendor management, candidate portal, gamification, screening questions.
- **53–56** — Enteri AI attempt guard, HM self‑scheduling, login rate limiting, payroll/capex fields.
- **57–63** — **Multi‑tenancy retrofit**: `tenant` table + `tenant_id` backfill across every table, platform‑admin flag, session freshness & role labels, stricter isolation pass, system status, platform superadmin flag.
- **64–68** — Module catalog & subscription plans, ticket replies, platform settings, document collection module.
- **69–72** — Newest modules: **BGV** (background verification), **Preboarding** (assets/policies), **Onboarding** (employee master), **HRMS** multi‑provider sync.

Postgres extensions used: `pgcrypto` (UUID generation).

---

## 9. External integrations

| Integration | Used for | Notes |
| --- | --- | --- |
| **Google Calendar / Meet** | Interview scheduling, Meet links | Per‑tenant OAuth; soft‑fails to a static meeting link if not connected |
| **Gmail (API + IMAP)** | Sending candidate/panel emails, scanning recruiter inboxes for incoming CVs | Two parallel ingestion paths (`email_ingest.py` OAuth poll, `cv_email_scan.py`/`recruiter_email_worker.py` IMAP scan) |
| **Groq** | LLM backend for the Enteri AI conversational interviewer and CV enrichment | `GROQ_API_KEY`, `GROQ_BASE_URL`, `LLM_MODEL` |
| **OpenAI (Whisper)** | Speech‑to‑text for interview answers | `WHISPER_MODEL` |
| **edge-tts / gTTS** | Text‑to‑speech for interview questions | Free/open, gendered voice config |
| **SadTalker (self‑hosted GPU service)** | Talking‑face avatar rendering | `backend/gpu-services/`; falls back to a client‑side animated "orb" when unavailable |
| **LinkedIn OIDC** | "Sign in with LinkedIn" identity verification for candidates | No profile scraping |
| **Darwinbox** | Offer release push (final HR system of record) | Referenced as the release target for approved offers; integration status per `offer.darwin_ref` |
| **BGV vendor(s)** | Background verification case initiation & results | Pluggable connector + HMAC‑signed webhook, stub/real switch per tenant |
| **HRMS providers** | Employee record sync post‑onboarding | Adapters for SuccessFactors, Workday, Oracle HCM, Zoho People, BambooHR, GreytHR |
| **Google Cloud Storage** | Durable storage for resumes, recordings, avatar videos | `google-cloud-storage` SDK |

---

## 10. Enteri AI interview & avatar pipeline

1. `interviewer_llm.py` (Groq) generates the next interview question/turn and later scores the full transcript.
2. `tts.py` converts the question text to speech (`edge-tts`, `gTTS` fallback).
3. `prerender.py` orchestrates: audio → `ffmpeg` → POST to the SadTalker GPU microservice → resulting MP4 uploaded to GCS (or local disk) → cached by content hash in `avatar_video_cache`.
4. `avatar.py` exposes one swappable interface (`orb` / `sadtalker` / `wav2lip` / `vendor`) so the interview flow **never hard‑depends on GPU infrastructure** — it always degrades to a client‑side animated orb reacting to audio amplitude.
5. `enteri_ai_render_worker.py` runs as a background retry backstop for renders that got stuck.
6. `stt.py` (Whisper) transcribes the candidate's spoken answer back into text for scoring.

Combined with `screening.py`'s resume‑match score, the two feed a single ranked shortlist chart — the bot never auto‑rejects; a human recruiter always makes the advance/reject call.

## 11. Proctoring

Consent‑gated interview proctoring: identity snapshot, webcam/screen recording, and AI behaviour flags (multi‑face, face‑absent, forbidden‑object detection), all surfaced to a human reviewer — never auto‑rejecting. Media is stored as Postgres `BYTEA` (`proctoring_storage.py`) rather than local disk so it stays consistent across multiple backend replicas. See `AVATAR_PROCTORING_SPEC.md` for the full legal/consent design and what was deliberately scoped out (lockdown browser, VM detection, government‑ID biometric matching — all flagged as specialist‑vendor territory, not natively built).

---

## 12. Environment variables (`.env.prod`)

| Variable | Purpose |
| --- | --- |
| `APP_BASE_URL` | Public base URL, used to build OAuth redirect/callback URLs |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | PostgreSQL connection |
| `JWT_SECRET` | HMAC signing secret for all JWTs |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` | Google OAuth (Calendar/Meet/Gmail) |
| `GROQ_API_KEY`, `GROQ_BASE_URL`, `LLM_MODEL` | Groq LLM for Enteri AI |
| `OPENAI_BASE_URL`, `WHISPER_MODEL` | Speech‑to‑text |
| `ENTERI_AI_MODE` | `scripted` \| `conversational` interview mode |
| `ENTERI_AI_VOICE_GENDER` | TTS voice selection |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_NAME` | Outbound email |
| `ENV` | Environment name |

Dev‑only extras set directly in `docker-compose.dev.yml` (not `.env.prod`): `AVATAR_PROVIDER` (`orb`\|`sadtalker`\|`wav2lip`\|`vendor`), `CV_INBOX_DIR`, `CV_STORE_DIR`, `JD_STORE_DIR`, `FRONTEND_DIR`, `OAUTHLIB_INSECURE_TRANSPORT`.

---

## 13. Running it locally

```bash
docker compose -f docker-compose.dev.yml up
```

This starts a Postgres 16 container (auto‑running `database/01_schema.sql`, `02_seed.sql`, `03_auth_migration.sql` on first boot via `docker-entrypoint-initdb.d`) plus the FastAPI backend with `--reload`, source‑mounted for hot reload, on `http://localhost:8080`. `main.py`'s `_auto_migrate()` then brings the schema the rest of the way up to date on every startup — no manual SQL step needed beyond the initial three files.

Production (`docker-compose.prod.yml`) runs only the backend container — PostgreSQL runs natively on the host, reached via `host.docker.internal`. The container is non‑root, binds `0.0.0.0:${PORT:-8080}`, and exposes `/api/health` for the platform's healthcheck.
