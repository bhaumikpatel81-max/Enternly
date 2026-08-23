# One Click Hire — Architecture & Build Guide (Phase 1)

This document explains the system we are building, the technology choices, and what has been delivered in Phase 1. It is written so a non-technical product owner and a developer can both follow it.

## What we are building

A standalone recruitment platform for EnternsTech that automates the hiring pipeline from application to offer, targeting a 3–4 day time-to-hire. It runs the routine work automatically and asks a recruiter to tap "approve" at three genuine decision points. When a candidate is finally selected and approved, the system pushes the offer into Darwinbox for release. The platform replaces the 20+ Google Sheets currently used for tracking with a single database that is the source of truth.

## The one principle that shapes everything: configuration over code

Bands, business units, group companies, approval chains, email templates, feedback forms, and interview rounds change over time. Anything that changes is stored as editable data in the database, not written into the program. When EnternsTech revises its band structure, someone adds new band rows and marks old ones inactive through the admin screen — no developer, no redeployment. This is why the band table has an `is_active` flag and the approval chains store their steps as flexible JSON.

## Technology stack

The backend is Python with FastAPI, chosen because the resume-screening and AI-bot work is far better supported in Python and it pairs cleanly with Postgres and Google Cloud. The database is PostgreSQL, matching the company environment. Resume files are stored in Google Cloud Storage rather than the database. Interview scheduling uses the Google Calendar and Google Meet APIs since the team is on Google Workspace; candidate and panel emails go through the Gmail API. The AI screening model is kept swappable behind a single internal interface, so production can use either a managed API with a no-data-retention agreement or a model hosted on a company GCP VM — that decision is left to the dev team and legal, and the rest of the system does not depend on which is chosen. The whole application is containerized with Docker in the layout the deployment pipeline expects.

## Repository layout

The repository follows the deployment pipeline's required structure: a `docker-compose.prod.yml` at the root, with `backend/`, `frontend/`, and `database/` folders. Services bind to `0.0.0.0` and read the host port from the `PORT` environment variable rather than hard-coding it. Each container runs as a non-root user with a healthcheck, and no secrets are committed to git, per the deployment prerequisites.

## Folder structure

Every file in the repository, annotated with its purpose.

```text
Enternly/
│
├── docker-compose.prod.yml         # Production orchestration — wires database + backend, reads .env.prod
├── docker-compose.dev.yml          # Development orchestration — hot-reload, root user for file watcher
├── .env.prod                       # Production env-var template (DB password, Google OAuth keys, JWT secret)
├── .gitignore                      # Excludes .env files, __pycache__, node_modules, OS noise
├── README.md                       # Non-technical user guide (how to run, how to use)
├── ARCHITECTURE.md                 # This file — tech design, folder map, phase roadmap
├── DESIGN_SPEC.md                  # Frontend rebuild spec — multi-screen ATS layout (future phase)
├── HANDOFF.md                      # Step-by-step build guide for non-technical owner
│
├── backend/
│   ├── Dockerfile                  # python:3.12-slim, non-root appuser, healthcheck on /api/health
│   ├── requirements.txt            # All Python dependencies with pinned versions
│   └── app/
│       ├── __init__.py             # Empty — marks app/ as a Python package
│       ├── main.py                 # FastAPI app entry point — routes, middleware, static file serving
│       ├── db.py                   # PostgreSQL connection layer (query / query_one helpers)
│       ├── auth_utils.py           # JWT creation/verification + bcrypt password hashing
│       │
│       ├── routers/                # Route handlers — each file owns one domain
│       │   ├── __init__.py
│       │   ├── auth.py             # POST /api/auth/login, GET /api/auth/me, POST /api/auth/change-password
│       │   ├── admin_users.py      # CRUD for users — admin-only (create, edit, deactivate, reset password)
│       │   └── google_oauth.py     # Google OAuth flow — connect, callback, status, disconnect
│       │
│       └── services/               # Business logic — isolated from HTTP layer
│           ├── __init__.py
│           ├── pipeline.py         # Application state machine — intake, scoring, advancement, stage_event logging
│           ├── screening.py        # Resume scoring — keyword match (50%) + experience (30%) + AI stub (20%)
│           ├── connectors.py       # External integrations — Google Calendar (live), Gmail / Bot / Darwin (stubs)
│           └── notetaker.py        # Recording consent gate + recording / transcription / summarisation pipeline
│
├── frontend/
│   ├── index.html                  # Main dashboard — all UI in one file (prototype, single-page vanilla JS)
│   ├── login.html                  # Login screen — email + password form, redirects to dashboard on success
│   └── assets/
│       └── enternly-logo.svg          # Brand logo
│
└── database/
    ├── 01_schema.sql               # Core tables — org structure, users, requisitions, candidates, interviews, offers, audit
    ├── 02_seed.sql                 # Real EnternsTech data — 6 companies, 8 BUs, 13 bands, sample users, approval chains
    ├── 03_auth_migration.sql       # Adds password_hash + reset_token columns to app_user
    ├── 03_reports.sql              # 7 reporting views — TAT, recruiter load, gender, positions, budget, BU, roll
    ├── 04_notetaker.sql            # Recording consent, meeting_recording, meeting_transcript, meeting_notes tables
    └── 05_google_oauth.sql         # recruiter_google_token table — stores per-recruiter OAuth access/refresh tokens
```

### What each layer does

| Layer | Role |
| --- | --- |
| `database/` | Source of truth — all tables, seed data, reporting views. Run once in order (01 → 02 → 03 → 04 → 05). |
| `backend/app/db.py` | Thin wrapper around psycopg2. All SQL lives in the routers and services, not here. |
| `backend/app/auth_utils.py` | Stateless — creates and verifies JWTs; hashes and checks passwords. No DB calls. |
| `backend/app/routers/` | HTTP boundary — validates inputs, calls services, returns JSON. No business logic. |
| `backend/app/services/` | All logic — scoring, state transitions, external API calls. Never touches HTTP objects. |
| `backend/app/main.py` | Assembles the app — mounts routers, adds JWT middleware, serves the frontend HTML. |
| `frontend/` | Browser UI — fetches the backend API, stores JWT in localStorage, renders views. No build step. |

## What Phase 1 delivers

Phase 1 is the foundation: the complete database. Three SQL files in the `database/` folder, all tested against PostgreSQL 16 and confirmed to run without errors.

The first file, `01_schema.sql`, creates every table: organisation structure (group companies, business units, bands), people, requisitions with multi-recruiter assignment, fully configurable interview rounds, candidates and applications, interviews and scorecards, per-band approval chains, offers, email templates, and a stage-event log that timestamps every status change. That stage-event log is deliberately central, because it is what makes all the time-tracking reports possible.

The second file, `02_seed.sql`, loads the real EnternsTech structure: the six group companies, the eight business units under EnternsTech, all thirteen bands ranked from 5 up to 1A, sample users, two approval-chain patterns (junior roles need only the BU head; senior roles need BU head then director), a default panel scorecard, two email templates, and one sample requisition with three rounds including the AI screening bot as the automated first round.

The third file, `03_reports.sql`, builds a reporting view for each report requested: TAT per application and time-to-fill per requisition, recruiter workload, gender split at every stage, total versus actual open positions by fiscal year, budgeted versus offered CTC, business-unit-wise rollup, and on-roll versus off-roll split. Each is a query that runs directly on the schema and becomes a dashboard panel or scheduled email in Phase 5.

## How the meeting notetaker fits

Every interview round, including the AI bot round, can be recorded, transcribed, and summarised, with the notes and recording shared to the recruiter and panel — similar to Read.ai or Fireflies. Recordings and transcripts are stored on company GCP. The notetaker uses one common interface with two interchangeable providers behind it: a native pipeline (an Enternly bot joins the meeting, captures the stream to GCP, then transcribes and summarises) and a third-party service (Fireflies or Read.ai) as a backup. If the preferred provider fails, the system automatically falls back to the other, so there is always a working path.

Crucially, recording is consent-gated. No recording can start until the candidate has been shown a recording notice and granted consent, and that consent is logged with the region for region-specific rules (India's DPDP Act, GDPR for EU candidates). The system enforces this at the recording function itself, not by asking the caller to remember. The exact consent wording and the regions you operate in are decisions for EnternsTech's legal team; the capability is built to respect whatever they specify. The suggested score the notes produce is assistive only — the panel makes the decision.

## How the AI interview bot fits When a recruiter has screened a large applicant pool down to a shortlist, the bot conducts a structured first-round interview with each shortlisted candidate and produces a score. That bot score is combined with the screening match score into a single ranked chart. The recruiter reviews the top of the chart and decides who advances. The bot never silently rejects anyone. This keeps the time savings while keeping a human accountable for every advance/reject decision, which matters for fairness and for the gender-diversity reporting EnternsTech tracks. Because the first round is just a configurable round type, the bot can later be swapped for an assessment test, or both can run, per the panel's preference — no schema change required.

## Build roadmap

Phase 1 (this delivery) is the database foundation. Phase 2 adds the backend API and the screening engine. Phase 3 adds Google Calendar and Meet auto-scheduling. Phase 4 adds the in-app scorecards and feedback capture. Phase 5 adds the reporting dashboards and the scheduled report emails. Phase 6 adds the Darwinbox integration that pushes approved offers across for release. Each phase is delivered as a runnable artifact that builds on the last.

## How to run the database locally

With PostgreSQL installed, create a database and run the three files in order: first `01_schema.sql`, then `02_seed.sql`, then `03_reports.sql`. The schema uses the `pgcrypto` extension for UUID generation, which the schema file enables automatically. After loading, the reporting views can be queried immediately.
