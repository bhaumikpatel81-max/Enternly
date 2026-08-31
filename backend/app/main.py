"""
One Click Hire -- FastAPI backend (prototype).

Binds to 0.0.0.0 and reads PORT from the environment, per the deployment
prerequisites. Serves a JSON API plus a simple bundled frontend so the whole
pipeline can be demonstrated end to end.
"""
import os
import re as _re
import uuid as _uuid
import json
from datetime import datetime, timedelta
from pathlib import Path

# Load .env.prod at startup — set all credentials there, never commit real passwords
_ROOT = Path(__file__).resolve().parents[2]   # Enternly/
_env_file = _ROOT / ".env.prod"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=False)
    print(f"[config] Loaded env from {_env_file.name}")

from fastapi import Depends, FastAPI, HTTPException, Request, File, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import psycopg2

from .db import query, query_one
from .services import pipeline, connectors
from .services.resume_parser import extract_text as _parse_resume
from .routers.auth import router as _auth_router
from .routers.admin_users import router as _admin_router
from .routers.pipeline_api import router as _pipeline_router
from .routers.reports_api import router as _reports2_router
from .routers.custom_reports_api import router as _custom_reports_router
from .routers.enteri_ai_api import router as _enteri_ai_router
from .routers.proctoring_api import router as _proctoring_router
from .routers.tickets_api import router as _tickets_router
from .routers.scorecard_api import router as _scorecard_router
from .routers.email_template_api import router as _email_template_router
from .routers.offers_api import router as _offers_router
from .routers.sla_api import router as _sla_router
from .routers.chain_templates_api import router as _chain_templates_router
from .routers.documentation_api import router as _documentation_router
from .routers.kpi_api import router as _kpi_router
from .routers.hiring_plan_api import router as _hiring_plan_router
from .routers.cv_api import router as _cv_router
from .routers.cv_match_api import router as _cv_match_router
from .routers.no_poach_api import router as _no_poach_router
from .routers.hm_api import router as _hm_router
from .routers.campus_bulk_api import router as _campus_router
from .routers.password_api import router as _password_router
from .routers.vendor_api import router as _vendor_router
from .routers.candidate_portal_api import router as _candidate_portal_router
from .routers.gamification_api import router as _gamification_router
from .routers.bands_api import router as _bands_router
from .routers.org_api import router as _org_router
from .routers.client_api import router as _client_router
from .routers.hrbp_api import router as _hrbp_router
from .routers.scheduling_api import router as _scheduling_router
from .routers.activity_log_api import router as _activity_log_router
from .routers.notifications_api import router as _notifications_router
from .routers.google_calendar_api import router as _google_calendar_router
from .routers.platform_auth_api import router as _platform_auth_router
from .routers.platform_admin_api import router as _platform_admin_router
from .routers.cv_api import ingest_and_link as _cv_ingest_and_link
from .auth_utils import _decode, assert_staff, require_company_admin

app = FastAPI(title="Enternly API", version="0.1.0")
app.include_router(_auth_router)
app.include_router(_admin_router)
app.include_router(_password_router)
app.include_router(_pipeline_router)
app.include_router(_reports2_router)
app.include_router(_custom_reports_router)
app.include_router(_enteri_ai_router)
app.include_router(_proctoring_router)
app.include_router(_tickets_router)
app.include_router(_scorecard_router)
app.include_router(_email_template_router)
app.include_router(_offers_router)
app.include_router(_sla_router)
app.include_router(_chain_templates_router)
app.include_router(_documentation_router)
app.include_router(_kpi_router)
app.include_router(_hiring_plan_router)
app.include_router(_cv_router)
app.include_router(_cv_match_router)
app.include_router(_no_poach_router)
app.include_router(_hm_router)
app.include_router(_campus_router)
app.include_router(_vendor_router)
app.include_router(_candidate_portal_router)
app.include_router(_gamification_router)
app.include_router(_bands_router)
app.include_router(_org_router)
app.include_router(_client_router)
app.include_router(_hrbp_router)
app.include_router(_scheduling_router)
app.include_router(_activity_log_router)
app.include_router(_notifications_router)
app.include_router(_google_calendar_router)
app.include_router(_platform_auth_router)
app.include_router(_platform_admin_router)


@app.get("/api/subscription/status")
def subscription_status(user: dict = Depends(require_company_admin)):
    """Tenant-facing read of the caller's OWN subscription -- deliberately
    NOT under /api/platform (that prefix is reserved for the platform
    console's deliberate cross-tenant reach). A company admin (or a
    platform superadmin acting on behalf of a tenant) can see their plan,
    dates, and days remaining, but nothing about any other tenant."""
    row = query_one(
        "SELECT plan, subscription_start_date, subscription_end_date, grace_period_days, status "
        "FROM tenant WHERE id = %s",
        [user.get("tenant_id")],
    )
    if not row:
        raise HTTPException(404, "No tenant on this account")
    end_date = row.get("subscription_end_date")
    days_remaining = (end_date - datetime.utcnow().date()).days if end_date else None
    return {
        "subscription_plan": row["plan"],
        "subscription_start_date": row["subscription_start_date"],
        "subscription_end_date": end_date,
        "grace_period_days": row["grace_period_days"],
        "status": row["status"],
        "days_remaining": days_remaining,
    }


@app.on_event("startup")
def _auto_migrate():
    """
    Idempotent migrations — run on every startup so developers never need
    to manually execute SQL files after pulling new code.
    Each statement is safe to re-run (uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
    """
    from .db import query
    migrations = [
        # ── Migration 0a: app_user auth columns (password_hash/reset_token) —
        # prepended ahead of Migration 16 (2026-07 audit fix). Nothing earlier in
        # this list created these; only database/03_auth_migration.sql did, and
        # that file is documentation-only (never auto-run against a live DB). ──
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS password_hash        TEXT",
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS reset_token          TEXT",
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS reset_token_expires  TIMESTAMPTZ",
        # ── Migration 0b: nexai_session table + indexes — prepended ahead of
        # Migration 16 (2026-07 audit fix). Everything from here through the rest
        # of this list assumes nexai_session already exists (many later
        # migrations ALTER it), but only database/08_report_fields.sql created
        # it, and that file is likewise documentation-only. ──
        """CREATE TABLE IF NOT EXISTS nexai_session (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            application_id  UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
            requisition_id  UUID NOT NULL REFERENCES requisition(id),
            questions       JSONB,
            transcript      JSONB,
            raw_score       NUMERIC,
            score_detail    JSONB,
            status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','in_progress','completed','failed')),
            started_at      TIMESTAMPTZ,
            completed_at    TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_nexai_app     ON nexai_session(application_id)",
        "CREATE INDEX        IF NOT EXISTS idx_nexai_req     ON nexai_session(requisition_id)",
        "CREATE INDEX        IF NOT EXISTS idx_nexai_status  ON nexai_session(status)",
        # NexAI candidate invite tokens (added 2026-06)
        """CREATE TABLE IF NOT EXISTS nexai_invite (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            application_id UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
            token          TEXT NOT NULL UNIQUE,
            invited_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at     TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '7 days',
            used_at        TIMESTAMPTZ,
            created_by     UUID REFERENCES app_user(id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_nexai_invite_token ON nexai_invite (token)",
        # CTC split columns (added 2026-06)
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS budgeted_fixed    NUMERIC",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS budgeted_variable NUMERIC",
        # System settings — admin-configurable key/value store (added 2026-06)
        """CREATE TABLE IF NOT EXISTS system_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by UUID REFERENCES app_user(id)
        )""",
        # Avatar pre-render pipeline — per-question video tracking (added 2026-06 Step 4)
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS question_videos JSONB NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS render_status TEXT NOT NULL DEFAULT 'pending' CHECK (render_status IN ('pending','rendering','ready','partial','failed'))",
        """CREATE TABLE IF NOT EXISTS avatar_video_cache (
            cache_key   TEXT        PRIMARY KEY,
            gcs_url     TEXT        NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        # Per-requisition NexAI question editor (added 2026-06 Step 7)
        """CREATE TABLE IF NOT EXISTS requisition_questions (
            requisition_id  UUID        PRIMARY KEY
                                        REFERENCES requisition(id) ON DELETE CASCADE,
            questions       JSONB       NOT NULL DEFAULT '[]'::jsonb,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by      UUID        REFERENCES app_user(id)
        )""",
        # De-duplicate nexai_invite — keep only the latest invite per application (added 2026-06)
        """DELETE FROM nexai_invite
           WHERE id NOT IN (
               SELECT DISTINCT ON (application_id) id
               FROM nexai_invite
               ORDER BY application_id, invited_at DESC
           )""",
        # Migration 16: conversational interview turn history (added 2026-06)
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS conversation JSONB",
        # Migration 17: unique index on candidate email (added 2026-06)
        "CREATE UNIQUE INDEX IF NOT EXISTS uidx_candidate_email ON candidate (LOWER(email))",
        # Migration 18: proctoring completion flag (added 2026-06)
        # proctoring_session table itself (database/09_proctoring.sql) was never
        # mirrored here — created just-in-time before this ALTER (2026-07 audit fix).
        """CREATE TABLE IF NOT EXISTS proctoring_session (
            id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            nexai_session_id          UUID REFERENCES nexai_session(id) ON DELETE SET NULL,
            application_id            UUID NOT NULL REFERENCES application(id),
            consent_granted           BOOLEAN NOT NULL DEFAULT FALSE,
            consent_text              TEXT,
            consented_at              TIMESTAMPTZ,
            proctoring_declined       BOOLEAN NOT NULL DEFAULT FALSE,
            identity_snapshot_path    TEXT,
            identity_match_status     TEXT NOT NULL DEFAULT 'not_attempted'
                                      CHECK (identity_match_status IN
                                             ('not_attempted','pending','matched','mismatch','vendor_error')),
            webcam_video_path         TEXT,
            screen_video_path         TEXT,
            screen_recording_declined BOOLEAN NOT NULL DEFAULT FALSE,
            flags                     JSONB NOT NULL DEFAULT '[]'::jsonb,
            flag_count                INT   NOT NULL DEFAULT 0,
            reviewer_notes            TEXT,
            reviewed_by               UUID REFERENCES app_user(id),
            reviewed_at               TIMESTAMPTZ,
            human_decision            TEXT CHECK (human_decision IN
                                                  ('cleared','flagged_minor','flagged_major','voided')),
            retention_until           TIMESTAMPTZ,
            created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_proc_nexai   ON proctoring_session(nexai_session_id)",
        "CREATE INDEX IF NOT EXISTS idx_proc_app     ON proctoring_session(application_id)",
        """CREATE INDEX IF NOT EXISTS idx_proc_review  ON proctoring_session(reviewed_at)
           WHERE human_decision IS NULL""",
        "ALTER TABLE proctoring_session ADD COLUMN IF NOT EXISTS proctoring_complete BOOLEAN NOT NULL DEFAULT FALSE",
        # Migration 19: email-sent guard to prevent duplicate completion emails (added 2026-06)
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS email_sent BOOLEAN NOT NULL DEFAULT FALSE",
        # Migration 20: nexai_session terminated_proctoring status + termination_reason (added 2026-06)
        # Drop + recreate the status CHECK so it includes 'terminated_proctoring'
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'nexai_session'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE nexai_session DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE nexai_session ADD CONSTRAINT nexai_session_status_check
           CHECK (status IN ('pending','in_progress','completed','failed','terminated_proctoring'))""",
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS termination_reason TEXT",
        # Migration 21: proctoring appeal workflow (added 2026-06)
        """CREATE TABLE IF NOT EXISTS proctoring_appeal (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            application_id        UUID NOT NULL REFERENCES application(id),
            nexai_session_id      UUID NOT NULL REFERENCES nexai_session(id),
            candidate_explanation TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','reviewed','relink_sent','rejected')),
            recruiter_notes       TEXT,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            reviewed_by           UUID REFERENCES app_user(id),
            reviewed_at           TIMESTAMPTZ,
            UNIQUE (nexai_session_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_proctoring_appeal_application ON proctoring_appeal(application_id)",
        "CREATE INDEX IF NOT EXISTS idx_proctoring_appeal_status ON proctoring_appeal(status)",
        # Migration 22: real AI screening columns + stability dimension (added 2026-06)
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS ai_fit_score      NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS ai_screen_detail  JSONB",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS avg_tenure_months NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS stability_score   NUMERIC",
        """ALTER TABLE application ADD COLUMN IF NOT EXISTS stability_status TEXT
           CHECK (stability_status IS NULL
               OR stability_status IN ('computed','pending_manual','not_applicable'))""",
        # Migration 23: scorecard draft/submit workflow (added 2026-06)
        # NOTE: comment numbering was 24 before 23 historically; SQL is idempotent so order is irrelevant.
        "ALTER TABLE scorecard ALTER COLUMN submitted_at DROP NOT NULL",
        "ALTER TABLE scorecard ADD COLUMN IF NOT EXISTS status     TEXT        NOT NULL DEFAULT 'draft'",
        "ALTER TABLE scorecard ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "UPDATE scorecard SET status = 'submitted' WHERE submitted_at IS NOT NULL AND status = 'draft'",
        # Migration 24: extended application fields — employment snapshot + CTC (added 2026-06)
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_company       TEXT",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_designation   TEXT",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_location      TEXT",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_fixed     NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_variable  NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_bonus     NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_total     NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_fixed    NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_variable NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_bonus    NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_total    NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS notice_period_days    INTEGER",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS willing_to_relocate   BOOLEAN",
        # Migration 25: email template key + placeholder + editor columns (added 2026-06)
        "ALTER TABLE email_template ADD COLUMN IF NOT EXISTS template_key       TEXT",
        "ALTER TABLE email_template ADD COLUMN IF NOT EXISTS valid_placeholders JSONB",
        "ALTER TABLE email_template ADD COLUMN IF NOT EXISTS updated_by         UUID REFERENCES app_user(id)",
        # Migration 26: Offers & Approvals — per-requisition approval chains (added 2026-06)
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS bonus_ctc    NUMERIC",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS designation  TEXT",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS joining_date DATE",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS notes        TEXT",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS revise_note  TEXT",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS darwin_ref   TEXT",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS current_step INT  NOT NULL DEFAULT 1",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS submitted_by UUID REFERENCES app_user(id)",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMPTZ",
        # Widen offer.status — drop old CHECK and replace (name varies by Postgres)
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'offer'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE offer DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE offer ADD CONSTRAINT offer_status_check
           CHECK (status IN (
               'draft','pending_approval','approved','rejected',
               'revising','on_hold','cancelled','sent_to_darwinbox',
               'released','accepted','declined'
           ))""",
        # Widen application.status to include offer hold/cancel states
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'application'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE application ADD CONSTRAINT application_status_check
           CHECK (status IN (
               'applied','screening','screen_passed','screen_rejected',
               'interviewing','selected','rejected',
               'offer_stage','offered','offer_on_hold','offer_cancelled',
               'joined','dropped'
           ))""",
        # Per-requisition offer approval chain (user-specific ordered steps)
        """CREATE TABLE IF NOT EXISTS req_offer_approver (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            requisition_id  UUID NOT NULL REFERENCES requisition(id) ON DELETE CASCADE,
            approver_id     UUID NOT NULL REFERENCES app_user(id),
            sequence        INT  NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (requisition_id, sequence)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_req_offer_approver_req ON req_offer_approver(requisition_id)",
        # Offer approval step log (one row per step per offer)
        """CREATE TABLE IF NOT EXISTS offer_approval_step (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            offer_id    UUID NOT NULL REFERENCES offer(id) ON DELETE CASCADE,
            approver_id UUID NOT NULL REFERENCES app_user(id),
            sequence    INT  NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','approved','rejected','skipped')),
            notes       TEXT,
            acted_at    TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_offer_step_offer    ON offer_approval_step(offer_id)",
        "CREATE INDEX IF NOT EXISTS idx_offer_step_approver ON offer_approval_step(approver_id)",
        # Migration 27: Meeting Notetaker — interview transcript notes (added 2026-06)
        # Stores Drive file info, raw transcript, and Groq summary for each interview.
        """CREATE TABLE IF NOT EXISTS interview_notes (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            interview_id     UUID NOT NULL UNIQUE REFERENCES interview(id) ON DELETE CASCADE,
            application_id   UUID REFERENCES application(id) ON DELETE CASCADE,
            drive_file_id    TEXT,
            drive_file_name  TEXT,
            transcript_text  TEXT,
            summary          JSONB,
            fetch_status     TEXT NOT NULL DEFAULT 'none',
            fetch_error      TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_interview_notes_interview ON interview_notes(interview_id)",
        # Migration 28: SLA / RAG deadline tracking (added 2026-06)
        # Stores per-key SLA target in days; missing keys fall back to service-layer defaults.
        """CREATE TABLE IF NOT EXISTS sla_config (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            config_key  TEXT NOT NULL UNIQUE,
            days        INTEGER NOT NULL DEFAULT 5 CHECK (days >= 1),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by  UUID REFERENCES app_user(id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_sla_config_key ON sla_config (config_key)",
        # Migration 29: Named reusable approval chain templates + per-step SLA (added 2026-06)
        """CREATE TABLE IF NOT EXISTS offer_chain_template (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT NOT NULL,
            description TEXT,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            created_by  UUID REFERENCES app_user(id),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS offer_chain_template_step (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            template_id UUID NOT NULL REFERENCES offer_chain_template(id) ON DELETE CASCADE,
            sequence    INT NOT NULL,
            approver_id UUID NOT NULL REFERENCES app_user(id),
            sla_days    INT NOT NULL DEFAULT 2 CHECK (sla_days >= 1),
            UNIQUE (template_id, sequence)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_oct_template ON offer_chain_template_step(template_id)",
        # Per-step SLA on existing approval tables
        "ALTER TABLE req_offer_approver   ADD COLUMN IF NOT EXISTS sla_days INT NOT NULL DEFAULT 2",
        "ALTER TABLE offer_approval_step  ADD COLUMN IF NOT EXISTS sla_days INT NOT NULL DEFAULT 2",
        # Migration 30: Recruitment Bifurcation — new pipeline stages + rich req fields (2026-06)
        # Step 1: Widen application.status CHECK to include all old + new values
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'application'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE application ADD CONSTRAINT application_status_check
           CHECK (status IN (
               'applied','ai_screening','nexai_bot','shortlisted','hm_screening',
               'panel_interview','hr_round','offer_approval','offered',
               'hired','rejected','on_hold',
               'screening','screen_passed','screen_rejected','interviewing','selected',
               'offer_stage','offer_on_hold','offer_cancelled','joined','dropped'
           ))""",
        # Step 2: Rename existing statuses to new pipeline stage names
        "UPDATE application SET status='ai_screening'    WHERE status='screening'",
        "UPDATE application SET status='shortlisted'     WHERE status='screen_passed'",
        "UPDATE application SET status='panel_interview' WHERE status='interviewing'",
        "UPDATE application SET status='hm_screening'    WHERE status='selected'",
        "UPDATE application SET status='offer_approval'  WHERE status='offer_stage'",
        "UPDATE application SET status='on_hold'         WHERE status='offer_on_hold'",
        "UPDATE application SET status='hired'           WHERE status='joined'",
        "UPDATE application SET status='rejected'        WHERE status IN ('screen_rejected','dropped','offer_cancelled')",
        # Step 3: Tighten CHECK to new names only (old names all migrated away)
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'application'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE application ADD CONSTRAINT application_status_check
           CHECK (status IN (
               'applied','ai_screening','nexai_bot','shortlisted','hm_screening',
               'panel_interview','hr_round','offer_approval','offered',
               'hired','rejected','on_hold'
           ))""",
        # Step 4: Rich requisition fields
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS req_code       TEXT UNIQUE",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS project        TEXT",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS grade_level    TEXT",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS max_experience NUMERIC",
        """ALTER TABLE requisition ADD COLUMN IF NOT EXISTS priority TEXT
           CHECK (priority IS NULL OR priority IN ('critical','high','medium','low'))""",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS source_channels TEXT[] NOT NULL DEFAULT '{}'",
        # Step 5: Auto-generate req_codes for existing requisitions without one
        """WITH numbered AS (
               SELECT id, 'REQ-' || LPAD(ROW_NUMBER() OVER (ORDER BY created_at)::text, 4, '0') AS code
               FROM requisition WHERE req_code IS NULL
           )
           UPDATE requisition SET req_code = numbered.code
           FROM numbered WHERE requisition.id = numbered.id""",
        # Step 6: Rename sla_config keys to match new stage names
        "UPDATE sla_config SET config_key='stage_ai_screening'    WHERE config_key='stage_screening'",
        "UPDATE sla_config SET config_key='stage_shortlisted'     WHERE config_key='stage_screen_passed'",
        "UPDATE sla_config SET config_key='stage_panel_interview' WHERE config_key='stage_interviewing'",
        "UPDATE sla_config SET config_key='stage_hm_screening'    WHERE config_key='stage_selected'",
        "UPDATE sla_config SET config_key='stage_offer_approval'  WHERE config_key='stage_offer_stage'",
        # ── Migration 31: Correct pipeline names to EnternsTech real flow ────────────
        # Step 1: Widen CHECK constraint to include both old and new stage names
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'application'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE application ADD CONSTRAINT application_status_check
           CHECK (status IN (
               'applied','screen','nexai_bot','shortlisted','interview','documentation','offered',
               'hired','rejected','on_hold',
               'ai_screening','hm_screening','panel_interview','hr_round','offer_approval'
           ))""",
        # Step 2: Rename statuses to corrected pipeline stage names
        "UPDATE application SET status='screen'        WHERE status='ai_screening'",
        "UPDATE application SET status='interview'     WHERE status IN ('hm_screening','panel_interview','hr_round')",
        "UPDATE application SET status='documentation' WHERE status='offer_approval'",
        # Step 3: Tighten CHECK to final stage names only
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'application'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE application ADD CONSTRAINT application_status_check
           CHECK (status IN (
               'applied','screen','nexai_bot','shortlisted','interview','documentation','offered',
               'hired','rejected','on_hold'
           ))""",
        # Step 4: Screening decision fields on application
        """ALTER TABLE application ADD COLUMN IF NOT EXISTS screening_decision TEXT
           CHECK (screening_decision IS NULL OR screening_decision IN ('pass','hold','reject'))""",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS screening_notes TEXT",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS screened_by UUID REFERENCES app_user(id)",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS screened_at TIMESTAMPTZ",
        # Step 5: Document collection table
        """CREATE TABLE IF NOT EXISTS application_document (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            application_id UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
            file_name      TEXT NOT NULL,
            file_path      TEXT NOT NULL,
            doc_type       TEXT NOT NULL DEFAULT 'general',
            uploaded_by    UUID REFERENCES app_user(id),
            uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            notes          TEXT
        )""",
        # Step 6: Negotiation log table
        """CREATE TABLE IF NOT EXISTS negotiation_log (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            application_id UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
            note           TEXT NOT NULL,
            stage_detail   TEXT,
            logged_by      UUID REFERENCES app_user(id),
            logged_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        # Step 7: Reseed sla_config with corrected key names
        "DELETE FROM sla_config WHERE config_key IN ('stage_ai_screening','stage_hm_screening','stage_panel_interview','stage_hr_round','stage_offer_approval')",
        "INSERT INTO sla_config (config_key, days) VALUES ('stage_screen',3),('stage_interview',5),('stage_documentation',5) ON CONFLICT (tenant_id, config_key) DO NOTHING",
        # ── Migration 32: Hiring Plan rows table ─────────────────────────────────
        """CREATE TABLE IF NOT EXISTS hiring_plan_rows (
            id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            fiscal_year             TEXT,
            quarter                 TEXT,
            company_entity          TEXT,
            finance_onboarding_date DATE,
            planned_onboarding_date DATE,
            requisition_id          UUID REFERENCES requisition(id) ON DELETE SET NULL,
            link_status             TEXT NOT NULL DEFAULT 'unlinked'
                                    CHECK (link_status IN ('unlinked','suggested','confirmed')),
            role_name               TEXT,
            bu                      TEXT,
            function                TEXT,
            sub_bu                  TEXT,
            project_name            TEXT,
            employment_type         TEXT,
            billable                TEXT,
            sow_received            TEXT,
            capex_opex              TEXT,
            capex_opex_on_track     TEXT,
            on_off_roll             TEXT,
            headcount               INT  NOT NULL DEFAULT 1,
            priority                TEXT,
            band                    TEXT,
            experience              TEXT,
            market_salary_range     TEXT,
            location                TEXT,
            budgeted_fixed          NUMERIC NOT NULL DEFAULT 0,
            budgeted_variable       NUMERIC NOT NULL DEFAULT 0,
            asset                   TEXT,
            salary_budgeted_till    DATE,
            hiring_status           TEXT NOT NULL DEFAULT 'Open Position'
                                    CHECK (hiring_status IN (
                                        'Open Position','Offered','Joined','Hold','Internal Employee'
                                    )),
            replacement_for         TEXT,
            aipl_code               TEXT,
            employee_name           TEXT,
            offered_fixed           NUMERIC NOT NULL DEFAULT 0,
            offered_variable        NUMERIC NOT NULL DEFAULT 0,
            ta_owner                TEXT,
            source_of_hire          TEXT,
            candidate_email         TEXT,
            offer_date              DATE,
            tentative_doj           DATE,
            remarks                 TEXT,
            created_by              UUID REFERENCES app_user(id),
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_hp_rows_fy     ON hiring_plan_rows(fiscal_year)",
        "CREATE INDEX IF NOT EXISTS idx_hp_rows_bu     ON hiring_plan_rows(bu)",
        "CREATE INDEX IF NOT EXISTS idx_hp_rows_req    ON hiring_plan_rows(requisition_id)",
        "CREATE INDEX IF NOT EXISTS idx_hp_rows_status ON hiring_plan_rows(hiring_status)",
        # ── Migration 33: CV Repository ──────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS cv_repository (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            file_name        TEXT NOT NULL,
            file_path        TEXT,
            file_hash        TEXT UNIQUE,
            file_ext         TEXT,
            candidate_name   TEXT,
            candidate_id     UUID REFERENCES candidate(id) ON DELETE SET NULL,
            requisition_id   UUID REFERENCES requisition(id) ON DELETE SET NULL,
            map_status       TEXT NOT NULL DEFAULT 'pool'
                             CHECK (map_status IN ('pool','mapped')),
            raw_text         TEXT,
            text_vector      tsvector,
            skills           TEXT[],
            enrich_status    TEXT NOT NULL DEFAULT 'pending'
                             CHECK (enrich_status IN ('pending','done','failed')),
            experience_years NUMERIC,
            current_position TEXT,
            location         TEXT,
            ai_summary       TEXT,
            source           TEXT NOT NULL DEFAULT 'upload'
                             CHECK (source IN ('bulk_folder','upload','watcher','email')),
            uploaded_by      UUID REFERENCES app_user(id) ON DELETE SET NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            enriched_at      TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cv_text_vector ON cv_repository USING GIN(text_vector)",
        "CREATE INDEX IF NOT EXISTS idx_cv_skills      ON cv_repository USING GIN(skills)",
        "CREATE INDEX IF NOT EXISTS idx_cv_candidate   ON cv_repository(candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_cv_hash        ON cv_repository(file_hash)",
        """CREATE TABLE IF NOT EXISTS cv_ingest_jobs (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            status      TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running','done','failed')),
            total       INT NOT NULL DEFAULT 0,
            processed   INT NOT NULL DEFAULT 0,
            mapped      INT NOT NULL DEFAULT 0,
            pooled      INT NOT NULL DEFAULT 0,
            duplicates  INT NOT NULL DEFAULT 0,
            errors      JSONB NOT NULL DEFAULT '[]',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS api_token TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_api_token ON app_user(api_token) WHERE api_token IS NOT NULL",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS cv_repository_id UUID REFERENCES cv_repository(id) ON DELETE SET NULL",
        # ── Migration 34: HM Requisition Approval Workflow (added 2026-06) ───────
        # Adds approval_status + created_by_role to requisition.
        # Existing rows default to 'approved' — zero behaviour change.
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS approval_status TEXT DEFAULT 'approved'",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS created_by_role TEXT",
        """DO $$
DECLARE r RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'requisition'::regclass
          AND conname  = 'requisition_approval_status_check'
    ) THEN
        EXECUTE $sql$
            ALTER TABLE requisition ADD CONSTRAINT requisition_approval_status_check
            CHECK (approval_status IN ('approved','pending_ta_approval','rejected'))
        $sql$;
    END IF;
END $$""",
        "CREATE INDEX IF NOT EXISTS idx_req_approval_status ON requisition (approval_status)",
        # ── Migration 35: is_builtin flag on email_template (added 2026-06) ─────
        "ALTER TABLE email_template ADD COLUMN IF NOT EXISTS is_builtin BOOLEAN NOT NULL DEFAULT FALSE",
        # ── Migration 36: widen cv_repository.source CHECK to add 'application' ─
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'cv_repository'::regclass
               AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%source%'
    LOOP
        EXECUTE 'ALTER TABLE cv_repository DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'cv_repository'::regclass
          AND conname = 'cv_repository_source_check'
    ) THEN
        EXECUTE $sql$
            ALTER TABLE cv_repository ADD CONSTRAINT cv_repository_source_check
            CHECK (source IN ('bulk_folder','upload','watcher','email','application'))
        $sql$;
    END IF;
END $$""",
        # Seed new settings defaults (idempotent — ON CONFLICT preserves existing values)
        """INSERT INTO system_settings (key, value)
           VALUES
             ('about_company_text', 'About EnternsTech: [Configure in Settings]'),
             ('auto_jd_email', 'true')
           ON CONFLICT (tenant_id, key) DO NOTHING""",

        # ── Migration 37: seed company_name + ta_default_signature settings ───────
        """INSERT INTO system_settings (key, value)
           VALUES
             ('company_name',          'EnternsTech Pvt. Ltd.'),
             ('ta_default_signature',  'Talent Acquisition Team')
           ON CONFLICT (tenant_id, key) DO NOTHING""",

        # ── Migration 38: guarantee final application.status constraint ──────────
        # Drops the old constraint first (safe with IF EXISTS), migrates any
        # remaining rows that still carry legacy status names, then re-adds the
        # final constraint. Runs atomically so it cannot be left half-applied.
        """DO $$
BEGIN
    ALTER TABLE application DROP CONSTRAINT IF EXISTS application_status_check;
    UPDATE application SET status='screen'        WHERE status IN ('screening','ai_screening');
    UPDATE application SET status='shortlisted'   WHERE status='screen_passed';
    UPDATE application SET status='interview'     WHERE status IN ('interviewing','hm_screening','panel_interview','hr_round');
    UPDATE application SET status='documentation' WHERE status='offer_approval';
    UPDATE application SET status='rejected'      WHERE status IN ('screen_rejected','dropped','offer_cancelled');
    UPDATE application SET status='on_hold'       WHERE status='offer_on_hold';
    UPDATE application SET status='hired'         WHERE status='joined';
    ALTER TABLE application ADD CONSTRAINT application_status_check
        CHECK (status IN (
            'applied','screen','nexai_bot','shortlisted','interview',
            'documentation','offered','hired','rejected','on_hold'
        ));
END $$""",

        # ── Migration 39: per-requisition scoring weights + fresher role flag ───
        # resume_weight + interview_weight control the combined-score blend.
        # is_fresher_role forces the fresher scoring model for campus roles.
        # panel_consensus stores the computed verdict badge directly on application
        # so list queries can surface it without parsing score_breakdown JSONB.
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS resume_weight    NUMERIC(4,2) DEFAULT 0.40",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS interview_weight NUMERIC(4,2) DEFAULT 0.60",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS is_fresher_role  BOOLEAN      DEFAULT FALSE",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS panel_consensus  TEXT",
        """DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'application'::regclass
          AND conname  = 'application_panel_consensus_check'
    ) THEN
        EXECUTE $sql$
            ALTER TABLE application ADD CONSTRAINT application_panel_consensus_check
            CHECK (panel_consensus IS NULL OR panel_consensus IN ('advance','reject','split'))
        $sql$;
    END IF;
END $$""",

        # ── Migration 40: Campus Bulk Upload — batch invite for freshers / campus drives ──
        """CREATE TABLE IF NOT EXISTS campus_upload_batch (
            id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            requisition_id  UUID        REFERENCES requisition(id),
            uploaded_by     UUID        REFERENCES app_user(id),
            file_name       TEXT,
            total_rows      INTEGER,
            selected_count  INTEGER     NOT NULL DEFAULT 0,
            invited_count   INTEGER     NOT NULL DEFAULT 0,
            status          TEXT        NOT NULL DEFAULT 'draft'
                            CHECK (status IN ('draft','invites_sent','completed')),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_campus_batch_req ON campus_upload_batch(requisition_id)",
        """CREATE TABLE IF NOT EXISTS campus_candidate (
            id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            batch_id         UUID        REFERENCES campus_upload_batch(id),
            requisition_id   UUID        REFERENCES requisition(id),
            name             TEXT,
            email            TEXT,
            phone            TEXT,
            college          TEXT,
            branch           TEXT,
            cgpa             NUMERIC(4,2),
            graduation_year  INTEGER,
            extra_data       JSONB       NOT NULL DEFAULT '{}'::jsonb,
            invite_status    TEXT        NOT NULL DEFAULT 'pending'
                             CHECK (invite_status IN ('pending','invite_queued','invited','interview_started','completed')),
            invite_sent_at   TIMESTAMPTZ,
            nexai_session_id TEXT,
            application_id   UUID        REFERENCES application(id),
            resume_uploaded  BOOLEAN     NOT NULL DEFAULT FALSE,
            resume_url       TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_campus_cand_batch ON campus_candidate(batch_id)",
        "CREATE INDEX IF NOT EXISTS idx_campus_cand_app   ON campus_candidate(application_id)",

        # ── Password reset / first-time set-password tokens ─────────────────────
        # Must be created BEFORE Migration 41 which ALTERs this table.
        """CREATE TABLE IF NOT EXISTS password_reset_token (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            token_hash  TEXT NOT NULL UNIQUE,
            purpose     TEXT NOT NULL DEFAULT 'reset'
                        CHECK (purpose IN ('reset','invite')),
            expires_at  TIMESTAMPTZ NOT NULL,
            used_at     TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_prt_user ON password_reset_token(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_prt_hash ON password_reset_token(token_hash)",

        # ── Migration 41: Vendor Management ──────────────────────────────────────────
        # Extend password_reset_token with account_type so one token table serves
        # staff, vendor, and candidate logins.  The FK on user_id is dropped so
        # vendor_user / candidate_user IDs can be stored in the same column.
        # Approach chosen: minimal change — no extra columns, no new token tables.
        """DO $$
DECLARE r RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'password_reset_token' AND column_name = 'account_type'
    ) THEN
        FOR r IN SELECT conname FROM pg_constraint
                 WHERE conrelid = 'password_reset_token'::regclass AND contype = 'f'
        LOOP
            EXECUTE 'ALTER TABLE password_reset_token DROP CONSTRAINT ' || quote_ident(r.conname);
        END LOOP;
        ALTER TABLE password_reset_token ALTER COLUMN user_id DROP NOT NULL;
        ALTER TABLE password_reset_token
            ADD COLUMN account_type TEXT NOT NULL DEFAULT 'staff'
            CHECK (account_type IN ('staff','vendor','candidate'));
    END IF;
END $$""",
        # Vendor (partner / sourcing company)
        """CREATE TABLE IF NOT EXISTS vendor (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name           TEXT NOT NULL,
            contact_email  TEXT,
            contact_phone  TEXT,
            status         TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active','suspended')),
            created_by     UUID REFERENCES app_user(id),
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        # Vendor user accounts (mirrors app_user auth columns)
        """CREATE TABLE IF NOT EXISTS vendor_user (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            vendor_id      UUID NOT NULL REFERENCES vendor(id) ON DELETE CASCADE,
            full_name      TEXT NOT NULL,
            email          TEXT NOT NULL UNIQUE,
            password_hash  TEXT,
            is_active      BOOLEAN NOT NULL DEFAULT TRUE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_vendor_user_vendor ON vendor_user(vendor_id)",
        "CREATE INDEX IF NOT EXISTS idx_vendor_user_email  ON vendor_user(LOWER(email))",
        # Requisition ↔ vendor access
        """CREATE TABLE IF NOT EXISTS requisition_vendor (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            requisition_id  UUID NOT NULL REFERENCES requisition(id) ON DELETE CASCADE,
            vendor_id       UUID NOT NULL REFERENCES vendor(id) ON DELETE CASCADE,
            opened_by       UUID REFERENCES app_user(id),
            opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (requisition_id, vendor_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_req_vendor_req    ON requisition_vendor(requisition_id)",
        "CREATE INDEX IF NOT EXISTS idx_req_vendor_vendor ON requisition_vendor(vendor_id)",
        # application.source — 'vendor:<uuid>' for vendor submissions
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'direct'",

        # ── Migration 42: Candidate Portal ───────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS candidate_user (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            candidate_id   UUID NOT NULL UNIQUE REFERENCES candidate(id) ON DELETE CASCADE,
            email          TEXT NOT NULL UNIQUE,
            password_hash  TEXT,
            is_active      BOOLEAN NOT NULL DEFAULT TRUE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cu_candidate ON candidate_user(candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_cu_email     ON candidate_user(LOWER(email))",
        """CREATE TABLE IF NOT EXISTS candidate_feedback (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            candidate_id     UUID NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
            application_id   UUID REFERENCES application(id) ON DELETE SET NULL,
            company_rating   SMALLINT NOT NULL CHECK (company_rating  BETWEEN 1 AND 5),
            interview_rating SMALLINT NOT NULL CHECK (interview_rating BETWEEN 1 AND 5),
            comments         TEXT,
            visible_to_ta    BOOLEAN NOT NULL DEFAULT TRUE,
            submitted_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cfb_candidate   ON candidate_feedback(candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_cfb_application ON candidate_feedback(application_id)",

        # ── Migration 43: Gamification — criticality flag + ledger + config ──
        # Criticality on requisition (multiplies gamification points)
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS criticality TEXT NOT NULL DEFAULT 'Medium' CHECK (criticality IN ('Low','Medium','High','Critical'))",
        # Append-only gamification ledger
        """CREATE TABLE IF NOT EXISTS gamification_event (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            subject_type   TEXT NOT NULL CHECK (subject_type IN ('recruiter','vendor','candidate','hm')),
            subject_id     UUID NOT NULL,
            event_type     TEXT NOT NULL,
            base_points    NUMERIC NOT NULL,
            criticality    TEXT NOT NULL DEFAULT 'Medium',
            multiplier     NUMERIC NOT NULL DEFAULT 1.0,
            points_awarded NUMERIC NOT NULL,
            requisition_id UUID REFERENCES requisition(id) ON DELETE SET NULL,
            application_id UUID REFERENCES application(id) ON DELETE SET NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_gev_subject ON gamification_event(subject_type, subject_id)",
        "CREATE INDEX IF NOT EXISTS idx_gev_req     ON gamification_event(requisition_id)",
        "CREATE INDEX IF NOT EXISTS idx_gev_app     ON gamification_event(application_id)",
        "CREATE INDEX IF NOT EXISTS idx_gev_created ON gamification_event(created_at)",
        # Config table — editable by TA admin
        """CREATE TABLE IF NOT EXISTS gamification_config (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by UUID REFERENCES app_user(id)
        )""",
        # Seed base event points and multipliers (ON CONFLICT preserves existing tuned values)
        """INSERT INTO gamification_config (key, value) VALUES
             ('points.offer_within_sla',   '50'),
             ('points.fast_screen',        '20'),
             ('points.offer_accepted',     '80'),
             ('points.offer_joined',       '100'),
             ('points.panel_pass',         '30'),
             ('points.feedback_on_time',   '25'),
             ('sla.feedback_hours',        '48'),
             ('points.sla_met_stage',      '15'),
             ('points.submission',         '5'),
             ('points.candidate_advanced', '10'),
             ('multiplier.Low',            '1.0'),
             ('multiplier.Medium',         '1.5'),
             ('multiplier.High',           '2.5'),
             ('multiplier.Critical',       '4.0'),
             ('tier.bronze',               '0'),
             ('tier.silver',               '900'),
             ('tier.gold',                 '2200'),
             ('tier.platinum',             '4500')
           ON CONFLICT (tenant_id, key) DO NOTHING""",
        # Named achievements
        """CREATE TABLE IF NOT EXISTS gamification_badge (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            subject_type TEXT NOT NULL CHECK (subject_type IN ('recruiter','vendor','candidate','hm')),
            subject_id   UUID NOT NULL,
            badge_key    TEXT NOT NULL,
            earned_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (subject_type, subject_id, badge_key)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_gbadge_subject ON gamification_badge(subject_type, subject_id)",
        # Migration 37: per-user Gmail App Password for individual email scanning (2026-07)
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS gmail_address      TEXT",
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS gmail_app_password TEXT",

        # ── Migration 45: Per-recruiter module access delegation (2026-07) ────────
        # TA Manager picks an individual recruiter and toggles which otherwise
        # admin/ta_manager-only modules that ONE recruiter can use. Off by
        # default — a (recruiter_id, module) row only exists once granted.
        """CREATE TABLE IF NOT EXISTS recruiter_module_access (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            recruiter_id UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            module       TEXT NOT NULL,
            enabled      BOOLEAN NOT NULL DEFAULT TRUE,
            granted_by   UUID REFERENCES app_user(id),
            granted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (recruiter_id, module)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_recruiter_module_access_recruiter ON recruiter_module_access(recruiter_id)",

        # ── Migration 44: One-time production cleanup ─────────────────────────────
        # Runs ONCE (guarded by system_settings key), and only once PLATFORM_ADMIN_EMAIL
        # is configured (no hardcoded tenant admin address — that's bound per-company
        # via the Platform Admin login flow).
        # • Ensures the platform admin account exists
        # • Deletes all other users (test/dummy accounts)
        # • Clears admin password → forces Forgot Password on first login
        # • Removes seeded BUs and Group Companies (managed via Settings → Organisation)
        f"""DO $$
DECLARE _admin_id UUID;
DECLARE _dummy_ids UUID[];
DECLARE _admin_email TEXT := '{os.environ.get("PLATFORM_ADMIN_EMAIL", "").strip().replace("'", "''")}';
BEGIN
    IF _admin_email = '' THEN
        RETURN;
    END IF;

    IF EXISTS (SELECT 1 FROM system_settings WHERE key = 'prod_cleanup_done') THEN
        RETURN;
    END IF;

    INSERT INTO app_user (full_name, email, role)
    VALUES ('TA Admin', _admin_email, 'admin')
    ON CONFLICT (email) DO NOTHING;

    SELECT id INTO _admin_id FROM app_user WHERE email = _admin_email;

    SELECT ARRAY(SELECT id FROM app_user WHERE email != _admin_email) INTO _dummy_ids;

    IF _dummy_ids IS NOT NULL AND array_length(_dummy_ids, 1) > 0 THEN
        UPDATE feedback_form        SET created_by = _admin_id WHERE created_by = ANY(_dummy_ids);
        UPDATE email_template       SET created_by = _admin_id WHERE created_by = ANY(_dummy_ids);
        UPDATE email_template       SET updated_by = _admin_id WHERE updated_by = ANY(_dummy_ids);
        UPDATE requisition          SET created_by = _admin_id WHERE created_by = ANY(_dummy_ids);
        UPDATE requisition          SET hiring_manager_id = NULL WHERE hiring_manager_id = ANY(_dummy_ids);
        UPDATE offer_chain_template SET created_by = _admin_id WHERE created_by = ANY(_dummy_ids);
        UPDATE sla_config           SET updated_by = _admin_id WHERE updated_by = ANY(_dummy_ids);
        UPDATE system_settings      SET updated_by = _admin_id WHERE updated_by = ANY(_dummy_ids);
        DELETE FROM offer_chain_template_step WHERE approver_id = ANY(_dummy_ids);
        DELETE FROM req_offer_approver         WHERE approver_id = ANY(_dummy_ids);
        DELETE FROM offer_approval_step        WHERE approver_id = ANY(_dummy_ids);
        DELETE FROM app_user WHERE id = ANY(_dummy_ids);
    END IF;

    UPDATE app_user
    SET password_hash = NULL, reset_token = NULL, reset_token_expires = NULL
    WHERE email = _admin_email;

    DELETE FROM business_unit
    WHERE id NOT IN (SELECT DISTINCT bu_id FROM requisition WHERE bu_id IS NOT NULL);

    DELETE FROM group_company
    WHERE id NOT IN (SELECT DISTINCT company_id FROM business_unit WHERE company_id IS NOT NULL);

    INSERT INTO system_settings (key, value) VALUES ('prod_cleanup_done', 'true')
    ON CONFLICT (key) DO NOTHING;
END $$""",

        # ── Migration 46: async campus invite email queue (throttled batches) ──
        """ALTER TABLE campus_candidate ADD COLUMN IF NOT EXISTS email_status TEXT
           NOT NULL DEFAULT 'pending'
           CHECK (email_status IN ('pending','queued','sent','failed'))""",
        "ALTER TABLE campus_candidate ADD COLUMN IF NOT EXISTS email_attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE campus_candidate ADD COLUMN IF NOT EXISTS email_error TEXT",
        "ALTER TABLE campus_candidate ADD COLUMN IF NOT EXISTS email_next_attempt_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS idx_campus_cand_email_status ON campus_candidate(email_status, email_next_attempt_at)",
        # ── Migration 47: recruiter-authored screening questions for Enteri AI (2026-07) ──
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS screening_questions TEXT[] DEFAULT '{}'",
        # ── Migration 48: close migration-drift gaps found in the 2026-07 bug audit —
        # these columns/tables were only ever added via standalone database/*.sql
        # files that a fresh DB never runs; mirroring them here so _auto_migrate()
        # alone is sufficient (matches database/07,08,09,10,26_*.sql). ──
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS hm_feedback    TEXT",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS hm_reviewed_at TIMESTAMPTZ",
        """ALTER TABLE requisition
           ADD COLUMN IF NOT EXISTS is_p1           BOOLEAN NOT NULL DEFAULT FALSE,
           ADD COLUMN IF NOT EXISTS hiring_location TEXT""",
        """ALTER TABLE requisition ADD COLUMN IF NOT EXISTS risk TEXT
           CONSTRAINT requisition_risk_check CHECK (risk IN ('low','medium','high','critical'))""",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS is_internal_movement BOOLEAN NOT NULL DEFAULT FALSE",
        """CREATE TABLE IF NOT EXISTS support_ticket (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            raised_by   UUID NOT NULL REFERENCES app_user(id),
            category    TEXT NOT NULL DEFAULT 'other'
                        CHECK (category IN ('login_issue','bug','data_issue','feature_request','other')),
            subject     TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','in_progress','resolved')),
            resolved_by UUID REFERENCES app_user(id),
            reply       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS st_raised_by  ON support_ticket(raised_by)",
        "CREATE INDEX IF NOT EXISTS st_status     ON support_ticket(status)",
        "CREATE INDEX IF NOT EXISTS st_created_at ON support_ticket(created_at DESC)",
        """CREATE TABLE IF NOT EXISTS login_log (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID REFERENCES app_user(id),
            user_role   TEXT,
            ip_address  TEXT,
            logged_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS ll_user      ON login_log(user_id)",
        "CREATE INDEX IF NOT EXISTS ll_logged_at ON login_log(logged_at DESC)",
        """CREATE TABLE IF NOT EXISTS sent_email_log (
            id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            application_id UUID        REFERENCES application(id),
            template_key   TEXT,
            template_name  TEXT,
            sent_to_email  TEXT        NOT NULL,
            sent_by        UUID        REFERENCES app_user(id),
            sent_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            subject        TEXT,
            notes          TEXT
        )""",
        """CREATE OR REPLACE VIEW v_requisition_aging AS
           SELECT
               r.id, r.title, r.status, r.roll_type, r.fiscal_year,
               r.is_p1, r.risk, r.hiring_location,
               gc.name  AS company,
               bu.name  AS business_unit,
               b.code   AS band,
               EXTRACT(DAY FROM (now() - r.opened_at))::INT AS aging_days,
               CASE
                 WHEN EXTRACT(DAY FROM (now() - r.opened_at)) <= 15 THEN '0-15'
                 WHEN EXTRACT(DAY FROM (now() - r.opened_at)) <= 30 THEN '16-30'
                 WHEN EXTRACT(DAY FROM (now() - r.opened_at)) <= 45 THEN '31-45'
                 WHEN EXTRACT(DAY FROM (now() - r.opened_at)) <= 60 THEN '46-60'
                 WHEN EXTRACT(DAY FROM (now() - r.opened_at)) <= 90 THEN '61-90'
                 ELSE '91+'
               END AS aging_bracket
           FROM requisition r
           JOIN business_unit bu ON bu.id = r.bu_id
           JOIN group_company gc ON gc.id = bu.company_id
           JOIN band           b  ON b.id  = r.band_id
           WHERE r.status = 'open'
             AND r.opened_at IS NOT NULL""",
        # ── Migration 49: Enternly Batch 1 — HRBP + BU tagging, Rehire, No-Poach,
        # Market Intel (2026-07). business_unit/group_company already exist
        # (see business_units() above / org_api.py) and are reused as-is —
        # only the HRBP layer + app_user.bu_id + requisition tagging are new. ──
        """CREATE TABLE IF NOT EXISTS hrbp (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            full_name   TEXT NOT NULL,
            email       TEXT NOT NULL UNIQUE,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS bu_hrbp_map (
            id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            bu_id    UUID NOT NULL REFERENCES business_unit(id),
            hrbp_id  UUID NOT NULL REFERENCES hrbp(id),
            UNIQUE (bu_id)
        )""",
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS bu_id UUID REFERENCES business_unit(id)",
        # Widen app_user.role CHECK to allow 'hrbp' — drop whatever the live
        # constraint is actually named, then re-add it under a fixed name.
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'app_user'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%role%'
    LOOP
        EXECUTE 'ALTER TABLE app_user DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE app_user ADD CONSTRAINT app_user_role_check
           CHECK (role IN ('admin','ta_manager','recruiter','hiring_manager',
                            'bu_head','director','interviewer','hrbp'))""",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS hrbp_id UUID REFERENCES hrbp(id)",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS hrbp_email TEXT",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS hrbp_name TEXT",
        "CREATE INDEX IF NOT EXISTS idx_requisition_hrbp ON requisition(hrbp_id)",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS flags JSONB NOT NULL DEFAULT '{}'",
        # Rehire seed
        """CREATE TABLE IF NOT EXISTS former_employee (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            full_name         TEXT,
            email             TEXT,
            phone             TEXT,
            emp_code          TEXT,
            last_designation  TEXT,
            exit_date         DATE,
            exit_type         TEXT,
            rehire_eligible   BOOLEAN NOT NULL DEFAULT TRUE,
            notes             TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_former_employee_email ON former_employee(lower(email))",
        "CREATE INDEX IF NOT EXISTS idx_former_employee_phone ON former_employee(phone)",
        # No-poach company list
        """CREATE TABLE IF NOT EXISTS no_poach_company (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_name     TEXT NOT NULL,
            normalized_name  TEXT,
            status           TEXT CHECK (status IN ('past','current')),
            source           TEXT NOT NULL DEFAULT 'karan',
            effective_from   DATE,
            effective_to     DATE,
            is_active        BOOLEAN NOT NULL DEFAULT TRUE,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_no_poach_normalized ON no_poach_company(normalized_name)",
        # Market intelligence — placeholder, confirm fields before final use
        """CREATE TABLE IF NOT EXISTS market_intelligence (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            role_family   TEXT,
            location      TEXT,
            skill         TEXT,
            median_ctc    NUMERIC,
            p25_ctc       NUMERIC,
            p75_ctc       NUMERIC,
            demand_index  NUMERIC,
            source        TEXT,
            as_of_date    DATE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        # ── Migration 50: Enternly Batch 1.1 (2026-07) — current_company on campus
        # intake for no-poach coverage, location on no_poach_company ──────────
        "ALTER TABLE campus_candidate ADD COLUMN IF NOT EXISTS current_company TEXT",
        "ALTER TABLE no_poach_company ADD COLUMN IF NOT EXISTS location TEXT",

        # ── Migration 51: attach the original JD file to a requisition (2026-07) ──
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS jd_file_path TEXT",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS jd_file_name TEXT",

        # ── Migration 52: indexes for the dashboard/KPI/reports/SLA hot paths (2026-07) ──
        # Load testing showed multi-second latency under concurrent read traffic on a
        # 2-core box; these columns are filtered/joined-on by /api/dashboard, /api/kpi/*,
        # /api/sla/dashboard, and the 8 reports2 pivots but had no supporting index,
        # forcing full scans of application/stage_event (the two largest tables) on
        # nearly every dashboard load.
        "CREATE INDEX IF NOT EXISTS idx_application_applied_at ON application(applied_at)",
        "CREATE INDEX IF NOT EXISTS idx_stage_event_to_status ON stage_event(to_status, application_id)",
        "CREATE INDEX IF NOT EXISTS idx_requisition_recruiter_recruiter ON requisition_recruiter(recruiter_id)",
        "CREATE INDEX IF NOT EXISTS idx_requisition_hiring_manager ON requisition(hiring_manager_id)",
        "CREATE INDEX IF NOT EXISTS idx_application_candidate ON application(candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_offer_application ON offer(application_id)",

        # ── Migration 53: Enteri AI attempt-lifecycle guard on the invite token (2026-07) ──
        # One attempt per invite: attempt_status tracks unused -> in_progress -> completed,
        # or revoked when a recruiter reissues a fresh link before the candidate finishes.
        "ALTER TABLE nexai_invite ADD COLUMN IF NOT EXISTS attempt_status TEXT NOT NULL DEFAULT 'unused'",
        "ALTER TABLE nexai_invite ADD COLUMN IF NOT EXISTS attempt_started_at TIMESTAMPTZ",
        "ALTER TABLE nexai_invite ADD COLUMN IF NOT EXISTS attempt_completed_at TIMESTAMPTZ",
        "ALTER TABLE nexai_invite ADD COLUMN IF NOT EXISTS superseded_by_token TEXT",
        "CREATE INDEX IF NOT EXISTS idx_nexai_invite_attempt_status ON nexai_invite (attempt_status)",

        # ── Migration 54: Enternly Calendly-style HM self-scheduling (2026-07) ──
        # A recruiter (or an auto-triggered "Panel + Auto" round) opens a scheduling
        # request; the HM proposes 3-6 slots; the candidate confirms one via a
        # public token link, same pattern as nexai_invite (renamed to
        # enteri_ai_invite by Migration 99). See database/54_*.sql
        # for the doc-only snapshot of this block.
        """CREATE TABLE IF NOT EXISTS interview_schedule_request (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            application_id    UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
            round_config_id   UUID REFERENCES round_config(id),
            hm_user_id        UUID REFERENCES app_user(id),
            status            TEXT NOT NULL DEFAULT 'awaiting_hm'
                              CHECK (status IN ('awaiting_hm','awaiting_candidate',
                                                 'confirmed','cancelled','expired')),
            candidate_token   TEXT UNIQUE,
            duration_min      INTEGER NOT NULL DEFAULT 45,
            meeting_link      TEXT,
            confirmed_slot_id UUID,
            created_by        UUID REFERENCES app_user(id),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            hm_submitted_at    TIMESTAMPTZ,
            confirmed_at       TIMESTAMPTZ
        )""",
        """CREATE TABLE IF NOT EXISTS interview_slot (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            request_id   UUID NOT NULL REFERENCES interview_schedule_request(id) ON DELETE CASCADE,
            start_utc    TIMESTAMPTZ NOT NULL,
            status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','taken','released')),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_isr_application ON interview_schedule_request(application_id)",
        "CREATE INDEX IF NOT EXISTS idx_isr_token       ON interview_schedule_request(candidate_token)",
        "CREATE INDEX IF NOT EXISTS idx_isr_hm_status   ON interview_schedule_request(hm_user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_islot_request   ON interview_slot(request_id)",
        # Deferred FK — interview_slot didn't exist yet when the request table was
        # created above (circular reference). Guarded so re-running never errors.
        """DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_isr_confirmed_slot'
    ) THEN
        ALTER TABLE interview_schedule_request
            ADD CONSTRAINT fk_isr_confirmed_slot
            FOREIGN KEY (confirmed_slot_id) REFERENCES interview_slot(id);
    END IF;
END $$""",

        # ── Migration 55: login rate-limiting ledger (2026-07) ──────────────────
        # Every login attempt (success or failure) is logged here so auth.py can
        # rate-limit brute-force/spray attempts by (ip+email) and by ip alone.
        # See database/55_*.sql for the doc-only snapshot of this block.
        """CREATE TABLE IF NOT EXISTS login_attempt (
            id          BIGSERIAL PRIMARY KEY,
            email       TEXT,
            ip_address  TEXT,
            attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            success     BOOLEAN NOT NULL DEFAULT false
        )""",
        "CREATE INDEX IF NOT EXISTS idx_login_attempt_ip_time    ON login_attempt (ip_address, attempted_at)",
        "CREATE INDEX IF NOT EXISTS idx_login_attempt_email_time ON login_attempt (email, attempted_at)",

        # ── Migration 56: generic activity_log — backend-only action timestamps
        # for anything stage_event/offer_approval_step don't already cover
        # (requisition lifecycle, screening pass/hold, interview scheduling,
        # Enteri AI invites/sessions, offers, campus, vendor, module-access). Read
        # only through /api/activity-log/* report endpoints, never a live feed. ──
        """CREATE TABLE IF NOT EXISTS activity_log (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            entity_type     TEXT NOT NULL,
            entity_id       UUID,
            requisition_id  UUID REFERENCES requisition(id),
            application_id  UUID REFERENCES application(id) ON DELETE CASCADE,
            action          TEXT NOT NULL,
            actor_id        UUID REFERENCES app_user(id),
            actor_role      TEXT,
            actor_label     TEXT,
            from_value      TEXT,
            to_value        TEXT,
            detail          JSONB,
            ip_address      TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_activity_log_app       ON activity_log (application_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_activity_log_req       ON activity_log (requisition_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_activity_log_entity    ON activity_log (entity_type, entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_activity_log_occurred  ON activity_log (occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_activity_log_actor     ON activity_log (actor_id)",

        # ── Migration 57: per-round panelist roster (2026-07) ────────────────────
        # interview_panel was previously only ever populated with the hiring
        # manager — real panelists had no roster to be invited/authorised from.
        # panelist_emails lets a recruiter list who's on a panel round; confirm_pick()
        # invites everyone on it, and scorecard_api._is_panelist() checks it directly
        # so a roster member is authorised even if the interview_panel insert lags.
        "ALTER TABLE round_config ADD COLUMN IF NOT EXISTS panelist_emails TEXT[] NOT NULL DEFAULT '{}'::TEXT[]",

        # ── Migration 58: de-dupe feedback_form by case-insensitive name, then
        # enforce it going forward (2026-07). _ensure_default_form() looked up
        # "Default Panel Scorecard" with a case-sensitive `name = %s`, but the
        # seeded row was "Default panel scorecard" -- every miss silently
        # inserted another duplicate (no constraint stopped it). Collapse any
        # existing duplicate groups first (repointing round_config/scorecard
        # references from the losers to the earliest-created row in each
        # group), THEN add the case-insensitive unique index so it can never
        # recur. Safe to re-run: the DO block is a no-op once no group has
        # more than one row, and the index creation is IF NOT EXISTS.
        """DO $$
        DECLARE
          grp RECORD;
          keeper UUID;
          losers UUID[];
        BEGIN
          FOR grp IN
            SELECT LOWER(name) AS lname, array_agg(id ORDER BY created_at ASC) AS ids
            FROM feedback_form
            GROUP BY LOWER(name)
            HAVING COUNT(*) > 1
          LOOP
            keeper := grp.ids[1];
            losers := grp.ids[2:array_length(grp.ids, 1)];
            UPDATE round_config SET feedback_form_id = keeper WHERE feedback_form_id = ANY(losers);
            UPDATE scorecard    SET feedback_form_id = keeper WHERE feedback_form_id = ANY(losers);
            DELETE FROM feedback_form WHERE id = ANY(losers);
          END LOOP;
        END $$""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_form_name_ci ON feedback_form (LOWER(name))",

        # ── Migration 59: persistent notification center (2026-07) ──────────────
        # Recipient-scoped, best-effort-write notifications (bell + Action Queue
        # cards). Deliberately separate from activity_log -- that table is
        # audit-only, never read live, and has no recipient/read-state concept.
        # No candidate_id column -- "candidate" is always reached via
        # application.candidate_id elsewhere in this schema; adding a
        # denormalized column here would be the one inconsistent table.
        """CREATE TABLE IF NOT EXISTS notification (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            recipient_user_id     UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            type                  TEXT NOT NULL,
            title                 TEXT NOT NULL,
            body                  TEXT,
            action_url            TEXT,
            is_actionable         BOOLEAN NOT NULL DEFAULT FALSE,
            is_read               BOOLEAN NOT NULL DEFAULT FALSE,
            requisition_id        UUID REFERENCES requisition(id) ON DELETE CASCADE,
            application_id        UUID REFERENCES application(id) ON DELETE CASCADE,
            interview_request_id  UUID REFERENCES interview_schedule_request(id) ON DELETE CASCADE,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            read_at               TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS idx_notification_recipient ON notification (recipient_user_id, is_read, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_notification_isr        ON notification (interview_request_id)",

        # ── Migration 60: campus_candidate.invite_status gets a 'blocked' value
        # (2026-07) -- a no-poach hard-match at campus bulk-invite time previously
        # had nowhere durable to record itself on the row (only the response +
        # activity_log), so a blocked candidate looked identical to any other
        # still-pending row. DROP+ADD is idempotent across repeated boots. ──────
        "ALTER TABLE campus_candidate DROP CONSTRAINT IF EXISTS campus_candidate_invite_status_check",
        """ALTER TABLE campus_candidate ADD CONSTRAINT campus_candidate_invite_status_check
           CHECK (invite_status = ANY (ARRAY['pending','invite_queued','invited',
                                              'interview_started','completed','blocked']))""",

        # ── Migration 61: persist the TA-reject reason on requisition (2026-07) ──
        # Previously only ever emailed once (ta_reject_requisition), never
        # stored -- a resend of that decision had no way to reproduce the
        # original wording. ──────────────────────────────────────────────────
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS rejection_reason TEXT",

        # ── Migration 62: a default Google Meet link per interview round (2026-07) ──
        # interview_schedule_request.meeting_link already existed but nothing in
        # the UI ever set it, so every panel interview's confirmation email/ICS
        # said "will be shared separately" -- a promise with no follow-through.
        # A round-level default (set once by the recruiter alongside panelist
        # emails) lets BOTH the Auto flow (no recruiter interaction at
        # schedule-time at all) and the Manual flow resolve a real link without
        # re-entering one per candidate.
        "ALTER TABLE round_config ADD COLUMN IF NOT EXISTS meeting_link TEXT",

        # ── Migration 63: Google Calendar OAuth for auto-generated Meet links
        # (2026-07) -- one shared connection, connected once by a
        # TA admin, used for every panel round's interview event. See
        # services/google_calendar.py. Single-row table (always the one active
        # connection, upserted by delete+insert on (re)connect) rather than a
        # per-recruiter table -- deliberately simpler than the abandoned
        # per-recruiter recruiter_google_token design in database/05_google_oauth.sql
        # (that table was never even added here, so it never actually existed on
        # any real database -- this is a fresh, independent design, not a revival).
        """CREATE TABLE IF NOT EXISTS google_calendar_connection (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            google_email  TEXT NOT NULL,
            access_token  TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            token_expiry  TIMESTAMPTZ,
            scope         TEXT,
            connected_by  UUID REFERENCES app_user(id),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        # Short-lived CSRF nonce for the OAuth redirect round-trip -- this
        # backend is stateless/JWT-only (no server-side session to stash a
        # nonce in), so the state param needs its own tiny durable store
        # instead. Rows are deleted the moment they're consumed in the callback.
        """CREATE TABLE IF NOT EXISTS google_oauth_state (
            state       TEXT PRIMARY KEY,
            created_by  UUID REFERENCES app_user(id),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",

        # ── Migration 64: proctoring media moves from local disk into Postgres
        # (2026-07) -- local-disk storage broke the moment a second backend
        # replica existed (a chunk written to replica A's disk was invisible to
        # a stream request served by replica B). Every replica already shares
        # this one Postgres, so storing the bytes here fixes that for free and
        # keeps the data on company infra per the legal gate in
        # routers/proctoring_api.py. See services/proctoring_storage.py.
        """CREATE TABLE IF NOT EXISTS proctoring_media (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id    UUID NOT NULL REFERENCES proctoring_session(id) ON DELETE CASCADE,
            media_type    TEXT NOT NULL,
            chunk_index   INT NOT NULL DEFAULT 0,
            ext           TEXT NOT NULL DEFAULT '',
            content_type  TEXT,
            data          BYTEA NOT NULL,
            byte_size     INT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (session_id, media_type, chunk_index)
        )""",

        # ── Migration 65: durable retry tracking for avatar pre-rendering
        # (2026-07) -- prerender_interview_videos() is fired via FastAPI
        # BackgroundTasks from enteri_ai_api._do_single_invite() as an in-process,
        # fire-and-forget call: if the worker process restarts between the
        # invite response being sent and that task actually running, the job
        # is silently dropped and render_status sits at its default 'pending'
        # forever with no record anything went wrong. enteri_ai_render_worker.py
        # is a periodic sweep (same claim/backoff/dead-letter shape as
        # campus_email_worker.py) that picks up any session stuck in
        # 'pending' past a grace window, or 'failed' with attempts left, and
        # retries it -- a durable backstop behind the fast in-process path,
        # not a replacement for it.
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS render_attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS render_claimed_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS idx_nexai_session_render_sweep ON nexai_session(render_status, render_claimed_at)",

        # ── Migration 66: Interview Assessment Form (2026-07) -- a richer,
        # section-grouped panel scorecard (Technical / Competency / Comments /
        # Recommendation) for every human panel round, replacing the 4-field
        # seeded default. A NEW named feedback_form row, not an edit to the
        # existing "Default panel scorecard" -- editing that in place would
        # silently rewrite every round already using it. 'neutral' is a new
        # verdict token: this form's Recommendation scale (Strongly Recommend /
        # Recommend / Neutral / Do Not Recommend) has a real middle option the
        # legacy Strong Hire/Hire/No Hire/Strong No Hire scale never did.
        "ALTER TABLE scorecard DROP CONSTRAINT IF EXISTS scorecard_verdict_check",
        """ALTER TABLE scorecard ADD CONSTRAINT scorecard_verdict_check
           CHECK (verdict = ANY (ARRAY['strong_yes','yes','neutral','no','strong_no']))""",
        """DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM feedback_form WHERE LOWER(name) = LOWER('Interview Assessment Form')) THEN
            INSERT INTO feedback_form (name, schema) VALUES ('Interview Assessment Form', '[
              {"key":"primary_skills","label":"Primary Skills","type":"textarea","required":false,"section":"Technical (If Applicable)"},
              {"key":"secondary_skills","label":"Secondary Skills","type":"textarea","required":false,"section":"Technical (If Applicable)"},
              {"key":"certifications","label":"Certifications","type":"textarea","required":false,"section":"Technical (If Applicable)"},
              {"key":"communication","label":"Communication","type":"rating_5","required":true,"section":"Competency"},
              {"key":"problem_solving","label":"Problem-Solving","type":"rating_5","required":true,"section":"Competency"},
              {"key":"adaptability","label":"Adaptability","type":"rating_5","required":true,"section":"Competency"},
              {"key":"teamwork","label":"Teamwork","type":"rating_5","required":true,"section":"Competency"},
              {"key":"culture_fit","label":"Culture Fit","type":"rating_5","required":true,"section":"Competency"},
              {"key":"strengths","label":"Strengths","type":"textarea","required":false,"section":"Comments"},
              {"key":"concerns","label":"Concerns","type":"textarea","required":false,"section":"Comments"},
              {"key":"red_flags","label":"Red Flags","type":"textarea","required":false,"section":"Comments"},
              {"key":"recommendation","label":"Recommendation","type":"single_choice","required":true,"section":"Recommendation",
               "options":["Strongly Recommend","Recommend","Neutral","Do Not Recommend"]},
              {"key":"final_notes","label":"Final Notes","type":"textarea","required":false,"section":"Recommendation"}
            ]'::jsonb);
          END IF;
        END $$""",
        # Assign to every human round lacking an explicit form of its own --
        # bot_interview rounds are excluded (AI-scored from the Enteri AI
        # transcript, never manually scored by a panelist) and any round that
        # already has a feedback_form_id keeps whatever it was already set to.
        """UPDATE round_config
           SET feedback_form_id = (SELECT id FROM feedback_form WHERE LOWER(name) = LOWER('Interview Assessment Form'))
           WHERE feedback_form_id IS NULL
             AND LOWER(round_type) <> 'bot_interview'""",
        # ── Migration 67: cv_ingest_jobs.uploaded_by (2026-07) -- the "Scan My
        # Email" per-user ingest job (cv_api.py) has always inserted this
        # column, but it was never added to the CREATE TABLE, so every scan
        # failed with "column uploaded_by does not exist".
        "ALTER TABLE cv_ingest_jobs ADD COLUMN IF NOT EXISTS uploaded_by UUID REFERENCES app_user(id) ON DELETE SET NULL",

        # ── Migration 68: CV Repository AI Screening Scorecard (2026-07) —
        # on-demand resume-vs-JD scoring for any CV Repository entry against
        # any requisition, independent of whether a real application exists.
        # Reuses services/screening.py's score_application() (same engine as
        # the automatic application-time scoring). Cached per (cv, req) pair
        # so re-opening a scorecard doesn't re-call the Groq LLM every time.
        """CREATE TABLE IF NOT EXISTS cv_scorecard (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cv_repository_id  UUID NOT NULL REFERENCES cv_repository(id) ON DELETE CASCADE,
            requisition_id    UUID NOT NULL REFERENCES requisition(id) ON DELETE CASCADE,
            match_score       NUMERIC,
            score_breakdown   JSONB,
            scored_by         UUID REFERENCES app_user(id) ON DELETE SET NULL,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (cv_repository_id, requisition_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cv_scorecard_req ON cv_scorecard(requisition_id)",

        # ── Migration 69: Candidate self-service profile + LinkedIn refresh
        # (2026-07) -- adds editable profile fields (given_name, skills),
        # structured work experience / education (candidate portal "My
        # Profile" tab), and LinkedIn OAuth connect + 6-month reminder
        # tracking. LinkedIn OIDC only ever returns name/photo/email, never
        # a profile URL or headline -- linkedin_url is always a manually
        # entered field; OAuth just verifies identity and refreshes
        # name/photo. See services/linkedin_oauth.py and
        # services/linkedin_reminder_worker.py.
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS given_name TEXT",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS skills TEXT[]",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_url TEXT",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_source TEXT",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_profile_name TEXT",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_profile_photo_url TEXT",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_oauth_sub TEXT",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_connected_at TIMESTAMPTZ",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_last_synced_at TIMESTAMPTZ",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_reminder_sent_at TIMESTAMPTZ",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_reminder_next_attempt_at TIMESTAMPTZ",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_reminder_attempts INT NOT NULL DEFAULT 0",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_reminder_last_error TEXT",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_reminders_opt_out BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS linkedin_unsub_token TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_linkedin_unsub_token ON candidate(linkedin_unsub_token) WHERE linkedin_unsub_token IS NOT NULL",

        """CREATE TABLE IF NOT EXISTS candidate_work_experience (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            candidate_id  UUID NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
            company       TEXT NOT NULL,
            title         TEXT NOT NULL,
            start_month   SMALLINT,
            start_year    SMALLINT,
            end_month     SMALLINT,
            end_year      SMALLINT,
            is_current    BOOLEAN NOT NULL DEFAULT FALSE,
            description   TEXT,
            source        TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','resume_parse')),
            sort_order    SMALLINT NOT NULL DEFAULT 0,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cwe_candidate ON candidate_work_experience(candidate_id)",

        """CREATE TABLE IF NOT EXISTS candidate_education (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            candidate_id   UUID NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
            institution    TEXT NOT NULL,
            degree         TEXT,
            field_of_study TEXT,
            start_year     SMALLINT,
            end_year       SMALLINT,
            source         TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','resume_parse')),
            sort_order     SMALLINT NOT NULL DEFAULT 0,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cedu_candidate ON candidate_education(candidate_id)",

        """CREATE TABLE IF NOT EXISTS linkedin_oauth_state (
            state        TEXT PRIMARY KEY,
            candidate_id UUID NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",

        # ── Migration 70: multi-BU HRBPs (2026-07) -- an HRBP can now be the
        # visibility-fallback owner of more than one business unit (previously
        # app_user.bu_id was a single scalar column). app_user_bu replaces it
        # as the fallback scope_requisitions_for_hrbp() (hrbp_api.py) matches
        # against; the primary rule (r.hrbp_email match) already spanned
        # multiple BUs and is unaffected. Backfilled from the old column so
        # existing single-BU HRBPs keep working unchanged; app_user.bu_id
        # itself is left in place (unused going forward) rather than dropped.
        """CREATE TABLE IF NOT EXISTS app_user_bu (
            user_id  UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            bu_id    UUID NOT NULL REFERENCES business_unit(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, bu_id)
        )""",
        """INSERT INTO app_user_bu (user_id, bu_id)
           SELECT id, bu_id FROM app_user WHERE bu_id IS NOT NULL
           ON CONFLICT DO NOTHING""",

        # ── Migration 71: HRBP -> Group Company assignment (2026-07) -- lets
        # an HRBP be assigned to an entire group company from the
        # Organisation screen, not just specific BUs. scope_requisitions_
        # for_hrbp() (hrbp_api.py) treats this as a third fallback: an HRBP
        # assigned to a company sees every BU under it, in addition to their
        # per-BU home assignments (app_user_bu) and direct hrbp_email
        # matches -- never other companies.
        """CREATE TABLE IF NOT EXISTS app_user_company (
            user_id    UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            company_id UUID NOT NULL REFERENCES group_company(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, company_id)
        )""",

        # ── Migration 72: Band -> Group Company mapping (2026-07) -- lets each
        # Group Company scope which Bands/Grades appear on its requisition-form
        # band dropdown, managed from the Organisation screen. Existing bands
        # are backfilled against every existing active group company so
        # today's requisition flow keeps working; TA Admin/Manager then
        # adjusts per-company mappings going forward via /api/bands.
        """CREATE TABLE IF NOT EXISTS band_company (
            band_id    UUID NOT NULL REFERENCES band(id) ON DELETE CASCADE,
            company_id UUID NOT NULL REFERENCES group_company(id) ON DELETE CASCADE,
            PRIMARY KEY (band_id, company_id)
        )""",
        """INSERT INTO band_company (band_id, company_id)
           SELECT b.id, gc.id FROM band b CROSS JOIN group_company gc
           WHERE gc.is_active = true
           AND NOT EXISTS (SELECT 1 FROM band_company)
           ON CONFLICT DO NOTHING""",

        # ── Migration 73: backfill the standalone `hrbp` lookup table
        # (requisition create/edit dropdown, hrbp_api.py list_hrbp()) from
        # existing app_user accounts with role='hrbp'. Until now nothing kept
        # the two in sync, so an HRBP created via Users & Access never showed
        # up on the requisition form -- admin_users.py now does this on every
        # create/update going forward; this backfills accounts created before
        # that fix (2026-07).
        # Rewritten as a NOT EXISTS guard rather than ON CONFLICT (email) --
        # Migration 96 later widens hrbp's unique constraint to
        # (tenant_id, email), which this statement (unlike Migration 44's
        # guarded one-time block) re-runs on every single boot, so it must
        # not depend on any particular constraint shape at all. This was
        # always a one-time drift catch-up anyway (admin_users.py's
        # _sync_hrbp_directory keeps hrbp in sync going forward), so it no
        # longer needs the DO UPDATE half either.
        """INSERT INTO hrbp (full_name, email, is_active)
           SELECT au.full_name, au.email, au.is_active FROM app_user au
           WHERE au.role = 'hrbp'
             AND NOT EXISTS (SELECT 1 FROM hrbp h WHERE h.email = au.email)""",

        # ── Migration 74: fix cv_repository.source CHECK — cv_api.py's
        # per-recruiter IMAP scan (_run_imap_ingest) has always inserted with
        # source='email_ingest', but the CHECK constraint never allowed that
        # value (only bulk_folder/upload/watcher/email/application), so every
        # attachment it found failed the INSERT and was silently swallowed
        # into the job's per-file errors list — the "Scan My Email" feature
        # has never actually stored a CV. Also adds cv_ingest_jobs.skipped
        # for the new "not actually a resume" classification (2026-07).
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'cv_repository'::regclass
               AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%source%'
    LOOP
        EXECUTE 'ALTER TABLE cv_repository DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'cv_repository'::regclass
          AND conname = 'cv_repository_source_check'
    ) THEN
        EXECUTE $sql$
            ALTER TABLE cv_repository ADD CONSTRAINT cv_repository_source_check
            CHECK (source IN ('bulk_folder','upload','watcher','email','application','email_ingest'))
        $sql$;
    END IF;
END $$""",
        "ALTER TABLE cv_ingest_jobs ADD COLUMN IF NOT EXISTS skipped INT NOT NULL DEFAULT 0",

        # ── Migration 75: per-mailbox scan checkpoint (2026-07) — the IMAP
        # CV scan used to only look at UNSEEN mail, which silently skipped
        # any candidate email the recruiter had already opened/read (a
        # near-certainty in practice). Now it scans a rolling time window
        # instead of relying on the read/unread flag, tracked here so a
        # 5-minute poll doesn't re-walk the whole window's messages from
        # scratch every cycle.
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS gmail_last_scan_at TIMESTAMPTZ",

        # ── Migration 76: backfill CVs already stuck at enrich_status='pending'
        # with no extractable text (2026-07) — cv_enricher.py's queue query
        # always excluded these, so they'd show "AI processing…" forever.
        # Same fix as cv_ingest.py now applies going forward; this is the
        # one-time catch-up for rows already sitting in that limbo.
        """UPDATE cv_repository SET enrich_status='done', enriched_at=now()
           WHERE enrich_status='pending' AND (raw_text IS NULL OR TRIM(raw_text) = '')""",

        # ── Migration 77: cancellable CV ingest jobs (2026-07) — a recruiter
        # watching a live "Scan Ingest Folder" / "Scan My Email" progress
        # modal can now stop it mid-run (e.g. they spot wrong files being
        # picked up) instead of waiting for the whole batch to finish.
        "ALTER TABLE cv_ingest_jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'cv_ingest_jobs'::regclass
               AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE cv_ingest_jobs DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'cv_ingest_jobs'::regclass
          AND conname = 'cv_ingest_jobs_status_check'
    ) THEN
        EXECUTE $sql$
            ALTER TABLE cv_ingest_jobs ADD CONSTRAINT cv_ingest_jobs_status_check
            CHECK (status IN ('running','done','failed','cancelled'))
        $sql$;
    END IF;
END $$""",

        # ── Migration 78: cv_repository.email/phone (2026-07) — the AI
        # enrichment pass now also extracts these from the resume text
        # (alongside candidate_name) so a CV Repository pool entry can be
        # mapped straight into a requisition's pipeline without the
        # recruiter having to already know the candidate's contact info.
        "ALTER TABLE cv_repository ADD COLUMN IF NOT EXISTS email TEXT",
        "ALTER TABLE cv_repository ADD COLUMN IF NOT EXISTS phone TEXT",

        # ── Migration 79: requisition.capex_opex (2026-07) — database/56_
        # requisition_payroll_capex.sql added this column but that file is
        # init-only (docker-entrypoint-initdb.d) and never runs against an
        # existing dev DB, so requisition creation was failing with
        # "column capex_opex does not exist". This is the actual live fix.
        """ALTER TABLE requisition ADD COLUMN IF NOT EXISTS capex_opex TEXT
           NOT NULL DEFAULT 'na' CHECK (capex_opex IN ('capex','opex','na'))""",

        # ── Migration 80: self-service interview reschedule (2026-07) --
        # reschedule_token is the public link embedded in the candidate's and
        # every panelist's confirmation email (see scheduling_api.py's
        # _finalize_booking_tx/_send_booking_notifications); calendar_uid +
        # ics_sequence let a reschedule re-send the SAME calendar event with
        # an incremented SEQUENCE instead of creating a duplicate one in the
        # recipient's calendar app.
        "ALTER TABLE interview ADD COLUMN IF NOT EXISTS reschedule_token TEXT",
        "ALTER TABLE interview ADD COLUMN IF NOT EXISTS calendar_uid TEXT",
        "ALTER TABLE interview ADD COLUMN IF NOT EXISTS ics_sequence INTEGER NOT NULL DEFAULT 0",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_interview_reschedule_token ON interview(reschedule_token) WHERE reschedule_token IS NOT NULL",

        # ── Migration 81: reschedule history + who-requested-it tracking
        # (2026-07) -- candidate and panel/HM get SEPARATE reschedule links
        # (reschedule_token was candidate-only until now) so a reschedule can
        # be attributed to 'candidate' vs 'panel'. interview_reschedule is a
        # timestamped, reportable log (one row per reschedule) feeding the
        # new "Interview Reschedules" Custom Reports explore -- see
        # services/report_catalog.py.
        "ALTER TABLE interview ADD COLUMN IF NOT EXISTS panel_reschedule_token TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_interview_panel_reschedule_token ON interview(panel_reschedule_token) WHERE panel_reschedule_token IS NOT NULL",
        """CREATE TABLE IF NOT EXISTS interview_reschedule (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            interview_id      UUID NOT NULL REFERENCES interview(id) ON DELETE CASCADE,
            application_id    UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
            requested_by      TEXT NOT NULL CHECK (requested_by IN ('candidate','panel','staff')),
            old_scheduled_at  TIMESTAMPTZ,
            new_scheduled_at  TIMESTAMPTZ NOT NULL,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_interview_reschedule_interview ON interview_reschedule(interview_id)",
        "CREATE INDEX IF NOT EXISTS idx_interview_reschedule_application ON interview_reschedule(application_id)",

        # ── Migration 82: backfill requisition_recruiter for ta_managers who
        # created requisitions before the "ta_manager can be an individual
        # contributor" auto-assign fix existed (pipeline_api.py create_requisition,
        # hiring_plan_api.py create_req_from_plan) -- without this, those
        # ta_managers' Recruiter Performance / v_recruiter_load rows stay stuck
        # at 0 open_reqs / 0 applications for requisitions they in fact own,
        # since that view only counts via requisition_recruiter (2026-07).
        """INSERT INTO requisition_recruiter (requisition_id, recruiter_id, is_owner, assigned_by)
           SELECT r.id, r.created_by, true, r.created_by
           FROM requisition r
           WHERE r.created_by IS NOT NULL
             AND r.created_by_role IN ('recruiter','ta_manager')
             AND NOT EXISTS (
               SELECT 1 FROM requisition_recruiter rr
               WHERE rr.requisition_id = r.id AND rr.recruiter_id = r.created_by
             )
           ON CONFLICT (requisition_id, recruiter_id) DO NOTHING""",

        # ── Migration 83: allow 'staff' in interview_reschedule.requested_by
        # (2026-07) -- the in-app recruiter/ta_manager/admin reschedule
        # endpoint (POST /api/interviews/{id}/reschedule, scheduling_api.py
        # reschedule_interview_staff) was inserting the actor's role
        # ('recruiter'/'ta_manager'/'admin') into this column, but Migration 81's
        # CHECK constraint only allowed 'candidate'/'panel' -- every staff-
        # initiated reschedule hit that CHECK, rolled back the WHOLE
        # transaction (including the interview.scheduled_at UPDATE), and
        # surfaced to the user as a raw DB error with the time silently not
        # changing. Widening the constraint to also allow 'staff' (the bucket
        # scheduling_api.py now passes for all 3 staff roles) fixes this.
        """ALTER TABLE interview_reschedule DROP CONSTRAINT IF EXISTS interview_reschedule_requested_by_check""",
        """ALTER TABLE interview_reschedule ADD CONSTRAINT interview_reschedule_requested_by_check
           CHECK (requested_by IN ('candidate','panel','staff'))""",

        # ── Migration 84: HM feedback reminder columns (2026-07) -- backing
        # services/hm_feedback_reminder_worker.py, a background job that
        # emails the hiring manager on a repeating cadence while an
        # application sits in 'interview' with no hm_feedback yet (mirrors
        # linkedin_reminder_worker's claim/resend/backoff column shape).
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS hm_feedback_reminder_sent_at TIMESTAMPTZ",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS hm_feedback_reminder_count INT NOT NULL DEFAULT 0",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS hm_feedback_reminder_next_attempt_at TIMESTAMPTZ",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS hm_feedback_reminder_attempts INT NOT NULL DEFAULT 0",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS hm_feedback_reminder_last_error TEXT",

        # ── Migration 85: app_user.created_by (2026-08) -- tracks who created
        # each account. Lets admin_users.py show a "Created by" column on
        # Users & Access, and lets Recruiters (previously locked out entirely)
        # create Hiring Manager accounts while still being unable to view or
        # manage any other role.
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES app_user(id)",

        # ── Migration 86: server-side proctoring ledger tables (2026-08) --
        # Phase 1 of rebuilding proctoring so the server keeps its own
        # independent record instead of trusting the browser's self-reported
        # strike_count (see terminate_invite_session). Storage only in this
        # phase -- no endpoint wiring, no browser changes, PROCTORING_AI_ENABLED
        # stays false. Later phases will read/write these tables:
        #   - proctoring_flag_ledger: one row per flag-tick the server has
        #     independently accepted, so termination can be corroborated
        #     server-side instead of trusting a client-computed strike_count.
        #   - proctoring_session_key: an issued per-session secret the browser
        #     must present to submit flags, so a session can't be spoofed by
        #     an arbitrary token holder.
        #   - proctoring_session_state: device_type ('laptop'/'phone'/'unknown')
        #     so future phases can apply different strike rules per device.
        #   - proctoring_pause_event: records every legitimate pause (e.g.
        #     low_light) with paused_at/resumed_at, so tick time during a
        #     pause is excluded from strike-persistence counting server-side.
        """CREATE TABLE IF NOT EXISTS proctoring_flag_ledger (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id         UUID NOT NULL REFERENCES proctoring_session(id) ON DELETE CASCADE,
            flag_type          TEXT NOT NULL,
            tick_index         INT NOT NULL DEFAULT 0,
            client_timestamp   TIMESTAMPTZ,
            server_received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            counts_as_strike   BOOLEAN NOT NULL DEFAULT false,
            raw_payload        JSONB,
            UNIQUE (session_id, flag_type, tick_index)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_flag_ledger_session ON proctoring_flag_ledger (session_id)",

        """CREATE TABLE IF NOT EXISTS proctoring_session_key (
            session_id     UUID NOT NULL UNIQUE REFERENCES proctoring_session(id) ON DELETE CASCADE,
            session_secret TEXT NOT NULL,
            issued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            revoked        BOOLEAN NOT NULL DEFAULT false
        )""",

        """CREATE TABLE IF NOT EXISTS proctoring_session_state (
            session_id                   UUID NOT NULL UNIQUE REFERENCES proctoring_session(id) ON DELETE CASCADE,
            device_type                  TEXT NOT NULL DEFAULT 'unknown',
            device_recommendation_shown  BOOLEAN NOT NULL DEFAULT false,
            created_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",

        """CREATE TABLE IF NOT EXISTS proctoring_pause_event (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id    UUID NOT NULL REFERENCES proctoring_session(id) ON DELETE CASCADE,
            pause_reason  TEXT NOT NULL,
            paused_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            resumed_at    TIMESTAMPTZ,
            raw_payload   JSONB
        )""",
        "CREATE INDEX IF NOT EXISTS idx_pause_event_session ON proctoring_pause_event (session_id)",

        # ── Migration 87: proctoring heartbeat (2026-08, Phase 3 Part D) --
        # A small history table, not a single last_heartbeat column, because
        # detecting a PAST gap between two consecutive heartbeats (for human
        # review of a completed session) needs the whole sequence, not just
        # the latest value. Browser sends one of these roughly every 10s while
        # the interview is active and not paused; server-side gap detection
        # (services/proctoring_scorer.py) flags any gap over a threshold
        # (default 60s) for human review only -- never auto-terminates.
        """CREATE TABLE IF NOT EXISTS proctoring_heartbeat (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id    UUID NOT NULL REFERENCES proctoring_session(id) ON DELETE CASCADE,
            received_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_heartbeat_session ON proctoring_heartbeat (session_id, received_at)",

        # ── Migration 88: proctoring termination discrepancy log (2026-08,
        # Phase 3 Part E) -- when SERVER_SIDE_PROCTORING_JUDGE is on (dev/test
        # only for now), a browser-claimed termination the server's own ledger
        # does NOT support is recorded here instead of being honoured, so a
        # human can review it. Gated off in production; see enteri_ai_api.py.
        """CREATE TABLE IF NOT EXISTS proctoring_termination_discrepancy (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id            UUID REFERENCES proctoring_session(id) ON DELETE CASCADE,
            nexai_session_id      UUID REFERENCES nexai_session(id) ON DELETE CASCADE,
            browser_strike_count  INT,
            browser_reason        TEXT,
            server_outcome        TEXT NOT NULL,
            server_detail         JSONB,
            reviewed              BOOLEAN NOT NULL DEFAULT false,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_term_discrepancy_session ON proctoring_termination_discrepancy (session_id)",

        # ── Migration 89: proctoring integrity flag (2026-08, Phase 4 Part B)
        # -- the unified "needs human attention" inbox across every kind of
        # tamper/integrity signal (monitoring gaps, termination discrepancies,
        # secret misuse, impossible data), feeding the Phase 4 digest email +
        # review-screen surfacing. Deliberately does NOT replace
        # proctoring_termination_discrepancy (kept -- see enteri_ai_api.py comment
        # at its call site): that table holds the rich, purpose-built detail
        # of a termination disagreement; this table is just a pointer into it
        # ("kind=termination_discrepancy happened, here's a summary, go
        # look") -- same additive-layering precedent as proctoring_session's
        # old JSONB flags column coexisting with proctoring_flag_ledger.
        # dedupe_key + the UNIQUE constraint is what makes re-running a
        # detector idempotent -- callers pick a key that identifies the
        # specific occurrence (e.g. a gap's start timestamp), not just the kind.
        """CREATE TABLE IF NOT EXISTS proctoring_integrity_flag (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id        UUID NOT NULL REFERENCES proctoring_session(id) ON DELETE CASCADE,
            nexai_session_id  UUID REFERENCES nexai_session(id) ON DELETE CASCADE,
            flag_kind         TEXT NOT NULL CHECK (flag_kind IN
                               ('monitoring_gap','termination_discrepancy','secret_misuse','impossible_data')),
            dedupe_key        TEXT NOT NULL DEFAULT 'default',
            detail            JSONB,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            reviewed          BOOLEAN NOT NULL DEFAULT false,
            reviewed_by       UUID REFERENCES app_user(id),
            reviewed_at       TIMESTAMPTZ,
            UNIQUE (session_id, flag_kind, dedupe_key)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_integrity_flag_session ON proctoring_integrity_flag (session_id)",
        "CREATE INDEX IF NOT EXISTS idx_integrity_flag_unreviewed ON proctoring_integrity_flag (session_id) WHERE reviewed = false",

        # ── Migration 90: integrity-flag digest tracking (2026-08, Phase 4
        # Part C) -- emailed_at marks a flag as already included in a sent
        # digest. The digest sender's atomic claim is
        # "UPDATE ... WHERE reviewed=false AND emailed_at IS NULL RETURNING
        # id" (services/proctoring_alerts.py) -- same idempotent-claim shape
        # as campus_email_worker's email_next_attempt_at, adapted to a
        # per-session synchronous trigger instead of a polling loop.
        "ALTER TABLE proctoring_integrity_flag ADD COLUMN IF NOT EXISTS emailed_at TIMESTAMPTZ",

        # ── Migration 91: recording_started_at (2026-08, Phase 5 Fix 4) --
        # consent_granted is recorded the moment the candidate clicks through,
        # BEFORE camera/screen permissions are actually requested -- a
        # permission-denied session (hard-blocked, see interview.html's P2/P3)
        # still reads "consent granted" with no recording ever having run.
        # consent_granted is a real authorization gate used by several
        # endpoints (identity snapshot, media-chunk upload, flags submit --
        # see proctoring_api.py's _assert_consented-style checks) and is left
        # completely untouched here. This is a SEPARATE, purely informational
        # signal: set once, the first time interview.html's recorders are
        # actually running (_startProcRecorders succeeds), never used as a gate.
        "ALTER TABLE proctoring_session ADD COLUMN IF NOT EXISTS recording_started_at TIMESTAMPTZ",

        # ── Migration 92: drop orphaned identity_snapshot_path (2026-08,
        # Phase 5 Fix 5) -- leftover from the old local-disk snapshot era;
        # snapshots have gone through Postgres BYTEA storage
        # (services/proctoring_storage.py) since Migration 64. Confirmed via
        # repo-wide grep: the only references left were this column's own
        # CREATE TABLE definition and database/09_proctoring.sql (a
        # documentation-only snapshot file, never auto-executed -- see this
        # file's own module docstring). Nothing in application code
        # reads/writes it.
        "ALTER TABLE proctoring_session DROP COLUMN IF EXISTS identity_snapshot_path",

        # ── Migration 93: one appeal per ATTEMPT, not per session forever
        # (2026-08, Phase 7 Fix 1) -- proctoring_appeal was keyed by
        # nexai_session_id, which relink_appeal reuses across every retake
        # (same nexai_session row is reset, not recreated), so the original
        # UNIQUE(nexai_session_id) permanently blocked a second appeal even
        # after a legitimate relink + re-termination. nexai_invite is the
        # thing that's actually new per attempt (Migration 53's
        # attempt_status lifecycle already treats it that way) -- rescope
        # uniqueness to nexai_invite_id instead. Table had zero rows at the
        # time of this migration (confirmed before writing it), so no
        # backfill or data-migration risk.
        "ALTER TABLE proctoring_appeal DROP CONSTRAINT IF EXISTS proctoring_appeal_nexai_session_id_key",
        "ALTER TABLE proctoring_appeal ADD COLUMN IF NOT EXISTS nexai_invite_id UUID REFERENCES nexai_invite(id)",
        # DROP-then-ADD pairing (not "ADD ... IF NOT EXISTS" -- Postgres has no
        # such form for constraints) is what makes this idempotent across the
        # repeated startups _auto_migrate() runs on every boot.
        "ALTER TABLE proctoring_appeal DROP CONSTRAINT IF EXISTS proctoring_appeal_nexai_invite_id_key",
        "ALTER TABLE proctoring_appeal ADD CONSTRAINT proctoring_appeal_nexai_invite_id_key UNIQUE (nexai_invite_id)",

        # ── Migration 94: multi-tenant platform roles (2026-08) -- Enternly is
        # moving from a single-customer deployment to a sellable multi-tenant
        # platform. Adds a `tenant` table (one row per customer company) and a
        # tenant_id column on app_user/group_company so a future customer's users
        # and org structure can be kept separate from this deployment's; every
        # existing row backfills onto a single seeded tenant so nothing changes
        # for the current deployment. Also widens app_user.role to add
        # platform_admin (Enternstech -- manages the tenant roster) and
        # company_admin (a customer's own super admin: user management, org
        # settings, integrations), and narrows what the existing ta_manager role
        # can reach (see auth_utils.py require_company_admin / admin_users.py,
        # org_api.py, bands_api.py, google_calendar_api.py, password_api.py).
        # See database/57_*.sql for the doc-only snapshot of this block.
        """CREATE TABLE IF NOT EXISTS tenant (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name                  TEXT NOT NULL,
            slug                  TEXT NOT NULL UNIQUE,
            status                TEXT NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active','trial','suspended')),
            plan                  TEXT NOT NULL DEFAULT 'standard',
            primary_contact_email TEXT,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """INSERT INTO tenant (id, name, slug, status, plan)
           VALUES ('00000000-0000-0000-0000-000000000001',
                   'EnternsTech Pvt. Ltd.', 'enternstech', 'active', 'standard')
           ON CONFLICT (slug) DO NOTHING""",
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE app_user SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE app_user ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE app_user ALTER COLUMN tenant_id SET NOT NULL",
        "ALTER TABLE group_company ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE group_company SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE group_company ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE group_company ALTER COLUMN tenant_id SET NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_app_user_tenant ON app_user(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_group_company_tenant ON group_company(tenant_id)",
        # Widen app_user.role CHECK to add platform_admin + company_admin, and
        # reconcile the pre-existing drift where 'hrbp' (Migration 49) was
        # added to this constraint here but never mirrored into a committed
        # database/*.sql snapshot -- drop whatever the live constraint is
        # actually named, then re-add it under a fixed name with the full,
        # current role list.
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'app_user'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%role%'
    LOOP
        EXECUTE 'ALTER TABLE app_user DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE app_user ADD CONSTRAINT app_user_role_check
           CHECK (role IN ('platform_admin','company_admin','admin','ta_manager',
                            'recruiter','hiring_manager','bu_head','director',
                            'interviewer','hrbp'))""",

        # ── Migration 95: session freshness, per-tenant role labels, and a
        # client/RPO provision (2026-08).
        #
        # 1) token_version on app_user -- a staff JWT is valid for up to
        #    TOKEN_HOURS after login; auth_utils._refresh_staff_claims() now
        #    re-reads role/tenant_id/token_version live on every request
        #    instead of trusting the (up to 8h stale) JWT claims, and rejects
        #    the request outright if this counter has been bumped since the
        #    token was issued (an admin forcing one user's sessions to end
        #    without waiting for natural expiry).
        # 2) tenant.role_labels -- every customer uses different job titles
        #    for the same underlying role (recruiter/bu_head/director/hrbp/
        #    etc. are fixed system role KEYS that permission checks key off
        #    of; role_labels only overrides what each is CALLED for that
        #    tenant -- see admin_users.py get_role_labels/save_role_labels).
        # 3) client table + requisition.client_id -- some customers are
        #    staffing/RPO agencies who hire on behalf of external clients,
        #    not only for their own internal roles. client is tenant-scoped
        #    (each customer manages its own client roster); a requisition's
        #    client_id is nullable -- NULL means an internal hire, set means
        #    hiring on behalf of that client. See client_api.py.
        # See database/58_*.sql for the doc-only snapshot of this block.
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS role_labels JSONB NOT NULL DEFAULT '{}'::jsonb",
        """CREATE TABLE IF NOT EXISTS client (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   UUID NOT NULL REFERENCES tenant(id),
            name        TEXT NOT NULL,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, name)
        )""",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS client_id UUID REFERENCES client(id)",
        "CREATE INDEX IF NOT EXISTS idx_requisition_client ON requisition(client_id)",
        "CREATE INDEX IF NOT EXISTS idx_client_tenant ON client(tenant_id)",

        # ── Migration 96: tenant_id everywhere it was still missing, and every
        # unique constraint that could plausibly collide across two different
        # customers re-scoped from global to per-tenant (2026-08). This is the
        # follow-through on Migration 94/95 -- those added tenant_id to
        # app_user/group_company/client only; every other tenant-owned table
        # (requisitions, candidates, vendors, bands, HRBPs, templates, config)
        # was still one shared pool across every customer. With a single
        # tenant in production today every backfill below is unconditionally
        # correct (there is nothing else a row could belong to); this must
        # land before a second customer's data enters the system, or the two
        # customers would see each other's candidates/requisitions/config.
        # See database/59_*.sql for the doc-only snapshot of this block.
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE requisition SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE requisition ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE requisition ALTER COLUMN tenant_id SET NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_requisition_tenant ON requisition(tenant_id)",

        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE candidate SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE candidate ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE candidate ALTER COLUMN tenant_id SET NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_candidate_tenant ON candidate(tenant_id)",
        # candidate.email was globally unique -- the same person applying to
        # two different customers must be two separate rows, one per tenant.
        "DROP INDEX IF EXISTS uidx_candidate_email",
        "CREATE UNIQUE INDEX IF NOT EXISTS uidx_candidate_email ON candidate (tenant_id, LOWER(email))",

        "ALTER TABLE candidate_user ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE candidate_user SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE candidate_user ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE candidate_user ALTER COLUMN tenant_id SET NOT NULL",
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'candidate_user'::regclass AND contype = 'u'
    LOOP
        EXECUTE 'ALTER TABLE candidate_user DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        "ALTER TABLE candidate_user ADD CONSTRAINT candidate_user_tenant_email_key UNIQUE (tenant_id, email)",

        "ALTER TABLE vendor ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE vendor SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE vendor ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE vendor ALTER COLUMN tenant_id SET NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_vendor_tenant ON vendor(tenant_id)",

        "ALTER TABLE vendor_user ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE vendor_user SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE vendor_user ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE vendor_user ALTER COLUMN tenant_id SET NOT NULL",
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'vendor_user'::regclass AND contype = 'u'
    LOOP
        EXECUTE 'ALTER TABLE vendor_user DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        "ALTER TABLE vendor_user ADD CONSTRAINT vendor_user_tenant_email_key UNIQUE (tenant_id, email)",

        "ALTER TABLE band ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE band SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE band ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE band ALTER COLUMN tenant_id SET NOT NULL",
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'band'::regclass AND contype = 'u'
    LOOP
        EXECUTE 'ALTER TABLE band DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        "ALTER TABLE band ADD CONSTRAINT band_tenant_code_key UNIQUE (tenant_id, code)",

        "ALTER TABLE hrbp ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE hrbp SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE hrbp ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE hrbp ALTER COLUMN tenant_id SET NOT NULL",
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'hrbp'::regclass AND contype = 'u'
    LOOP
        EXECUTE 'ALTER TABLE hrbp DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        "ALTER TABLE hrbp ADD CONSTRAINT hrbp_tenant_email_key UNIQUE (tenant_id, email)",

        "ALTER TABLE former_employee ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE former_employee SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE former_employee ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE former_employee ALTER COLUMN tenant_id SET NOT NULL",
        "DROP INDEX IF EXISTS idx_former_employee_email",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_former_employee_email ON former_employee(tenant_id, LOWER(email))",

        "ALTER TABLE no_poach_company ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE no_poach_company SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE no_poach_company ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE no_poach_company ALTER COLUMN tenant_id SET NOT NULL",
        "DROP INDEX IF EXISTS idx_no_poach_normalized",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_no_poach_normalized ON no_poach_company(tenant_id, normalized_name)",

        "ALTER TABLE email_template ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE email_template SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE email_template ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE email_template ALTER COLUMN tenant_id SET NOT NULL",
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'email_template'::regclass AND contype = 'u'
    LOOP
        EXECUTE 'ALTER TABLE email_template DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        "ALTER TABLE email_template ADD CONSTRAINT email_template_tenant_key_key UNIQUE (tenant_id, template_key)",

        "ALTER TABLE offer_chain_template ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE offer_chain_template SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE offer_chain_template ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE offer_chain_template ALTER COLUMN tenant_id SET NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_offer_chain_template_tenant ON offer_chain_template(tenant_id)",

        "ALTER TABLE feedback_form ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE feedback_form SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE feedback_form ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE feedback_form ALTER COLUMN tenant_id SET NOT NULL",
        "DROP INDEX IF EXISTS idx_feedback_form_name_ci",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_form_name_ci ON feedback_form (tenant_id, LOWER(name))",

        "ALTER TABLE cv_repository ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE cv_repository SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE cv_repository ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE cv_repository ALTER COLUMN tenant_id SET NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_cv_repository_tenant ON cv_repository(tenant_id)",
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'cv_repository'::regclass AND contype = 'u'
    LOOP
        EXECUTE 'ALTER TABLE cv_repository DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        "ALTER TABLE cv_repository ADD CONSTRAINT cv_repository_tenant_hash_key UNIQUE (tenant_id, file_hash)",

        "ALTER TABLE hiring_plan_rows ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE hiring_plan_rows SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE hiring_plan_rows ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE hiring_plan_rows ALTER COLUMN tenant_id SET NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_hiring_plan_rows_tenant ON hiring_plan_rows(tenant_id)",

        "ALTER TABLE gamification_event ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE gamification_event SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE gamification_event ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE gamification_event ALTER COLUMN tenant_id SET NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_gev_tenant ON gamification_event(tenant_id)",

        "ALTER TABLE gamification_badge ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE gamification_badge SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE gamification_badge ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE gamification_badge ALTER COLUMN tenant_id SET NOT NULL",
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'gamification_badge'::regclass AND contype = 'u'
    LOOP
        EXECUTE 'ALTER TABLE gamification_badge DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE gamification_badge ADD CONSTRAINT gamification_badge_tenant_subject_key
           UNIQUE (tenant_id, subject_type, subject_id, badge_key)""",

        "ALTER TABLE google_calendar_connection ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE google_calendar_connection SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE google_calendar_connection ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE google_calendar_connection ALTER COLUMN tenant_id SET NOT NULL",
        # Was an implicit single-row-for-the-whole-deployment table (deleted +
        # re-inserted on every (re)connect) -- one row per tenant from here on.
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_gcal_conn_tenant ON google_calendar_connection(tenant_id)",

        # system_settings / sla_config / gamification_config were single
        # shared-platform-wide config -- one customer's SMTP/SLA/scoring
        # settings must not be every other customer's too.
        "ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE system_settings SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE system_settings ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE system_settings ALTER COLUMN tenant_id SET NOT NULL",
        "ALTER TABLE system_settings DROP CONSTRAINT IF EXISTS system_settings_pkey",
        "ALTER TABLE system_settings ADD CONSTRAINT system_settings_pkey PRIMARY KEY (tenant_id, key)",

        "ALTER TABLE sla_config ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE sla_config SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE sla_config ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE sla_config ALTER COLUMN tenant_id SET NOT NULL",
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'sla_config'::regclass AND contype = 'u'
    LOOP
        EXECUTE 'ALTER TABLE sla_config DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        "ALTER TABLE sla_config ADD CONSTRAINT sla_config_tenant_key_key UNIQUE (tenant_id, config_key)",

        "ALTER TABLE gamification_config ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id)",
        "UPDATE gamification_config SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL",
        "ALTER TABLE gamification_config ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001'",
        "ALTER TABLE gamification_config ALTER COLUMN tenant_id SET NOT NULL",
        "ALTER TABLE gamification_config DROP CONSTRAINT IF EXISTS gamification_config_pkey",
        "ALTER TABLE gamification_config ADD CONSTRAINT gamification_config_pkey PRIMARY KEY (tenant_id, key)",

        # group_company.name was globally unique even though the column has
        # carried tenant_id since Migration 94 -- two tenants couldn't both
        # name a division "Corporate".
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'group_company'::regclass AND contype = 'u'
    LOOP
        EXECUTE 'ALTER TABLE group_company DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        "ALTER TABLE group_company ADD CONSTRAINT group_company_tenant_name_key UNIQUE (tenant_id, name)",

        # ── Migration 97: system_status -- a genuinely global (not per-tenant)
        # key/value table for deployment-level background-worker status
        # (email/CV ingest pollers, heartbeats, kill-switches). Migration 96
        # widened system_settings' primary key to (tenant_id, key) for real
        # per-customer settings (SMTP, company name, ...); several background
        # services were reusing that same table as a generic global KV store
        # for things that have nothing to do with any one tenant (e.g. "is
        # the CV scanner paused" is one on/off switch for the whole
        # deployment) -- those call sites now point at this table instead.
        # See database/60_*.sql for the doc-only snapshot of this block.
        """CREATE TABLE IF NOT EXISTS system_status (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        # Carry forward any values already written under the old keys before
        # this table existed, so an in-flight kill-switch/heartbeat isn't
        # silently reset by the migration itself.
        """INSERT INTO system_status (key, value, updated_at)
           SELECT key, value, updated_at FROM system_settings
           WHERE key IN ('email_ingest_status', 'cv_scan_paused',
                         'cv_enricher_heartbeat', 'recruiter_email_scan_status',
                         'activity_log_last_failure', 'bg_lock_last_error')
              OR key LIKE 'bg_task_status:%'
           ON CONFLICT (key) DO NOTHING""",

        # ── Migration 98: daily HR trivia question + per-subject answer
        # streak (2026-08). Points/tier/rank stay derived from the
        # gamification_event ledger everywhere else (services/gamification.py)
        # -- but a streak that resets after a missed day needs sequential
        # date-gap bookkeeping that can't be expressed as a single aggregate.
        # user_gamification_streak is a deliberate, narrow exception to the
        # "derive, don't store" convention: its only two writers are the
        # answer/skip endpoints below, both already single-row transactions,
        # so maintaining it at write-time is cheap and avoids a growing
        # window-function query on every dashboard load.
        # See database/61_gamification_daily_question.sql for the doc-only snapshot.
        """CREATE TABLE IF NOT EXISTS hr_question (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
            question_text    TEXT NOT NULL,
            option_a         TEXT NOT NULL,
            option_b         TEXT NOT NULL,
            option_c         TEXT NOT NULL,
            correct_option   TEXT NOT NULL CHECK (correct_option IN ('a','b','c')),
            explanation_text TEXT,
            active           BOOLEAN NOT NULL DEFAULT true,
            created_by       UUID REFERENCES app_user(id),
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_hrq_tenant_active ON hr_question(tenant_id, active)",

        """CREATE TABLE IF NOT EXISTS user_question_answer (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
            subject_type     TEXT NOT NULL CHECK (subject_type IN ('recruiter','vendor','candidate','hm')),
            subject_id       UUID NOT NULL,
            question_id      UUID NOT NULL REFERENCES hr_question(id),
            answer_date      DATE NOT NULL,
            selected_option  TEXT CHECK (selected_option IN ('a','b','c')),
            is_correct       BOOLEAN,
            was_skipped      BOOLEAN NOT NULL DEFAULT false,
            points_awarded   NUMERIC NOT NULL DEFAULT 0,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (subject_type, subject_id, answer_date)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_uqa_subject_date ON user_question_answer(subject_type, subject_id, answer_date)",

        """CREATE TABLE IF NOT EXISTS user_gamification_streak (
            subject_type       TEXT NOT NULL CHECK (subject_type IN ('recruiter','vendor','candidate','hm')),
            subject_id         UUID NOT NULL,
            tenant_id          UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
            current_streak     INT NOT NULL DEFAULT 0,
            longest_streak     INT NOT NULL DEFAULT 0,
            last_activity_date DATE,
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (subject_type, subject_id)
        )""",

        """INSERT INTO gamification_config (key, value) VALUES
             ('points.daily_question_correct', '10')
           ON CONFLICT (tenant_id, key) DO NOTHING""",

        # ── Migration 99: rename the "NexAI" brand to "Enteri AI" (2026-08) ──
        # Every migration above that created nexai_session/nexai_invite/etc.
        # is left untouched — those statements already ran against any
        # existing database, and editing them in place would just silently
        # no-op there while leaving the real, data-bearing tables under
        # their old names forever. This migration instead does the actual
        # RENAME against whatever currently exists, so it works whether
        # nexai_session already existed (an existing deployment) or was
        # only just created moments ago by the migrations above (a fresh
        # install) — either way this step finds it under the old name and
        # renames it forward. Guarded with existence checks so re-running
        # it on every startup after the first time is a safe no-op.
        """DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'nexai_session') THEN
        ALTER TABLE nexai_session RENAME TO enteri_ai_session;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'nexai_invite') THEN
        ALTER TABLE nexai_invite RENAME TO enteri_ai_invite;
    END IF;
END $$""",
        """DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'nexai_session_status_check') THEN
        ALTER TABLE enteri_ai_session RENAME CONSTRAINT nexai_session_status_check TO enteri_ai_session_status_check;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'proctoring_appeal_nexai_invite_id_key') THEN
        ALTER TABLE proctoring_appeal RENAME CONSTRAINT proctoring_appeal_nexai_invite_id_key TO proctoring_appeal_enteri_ai_invite_id_key;
    END IF;
END $$""",
        """DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_nexai_app') THEN
        ALTER INDEX idx_nexai_app RENAME TO idx_enteri_ai_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_nexai_req') THEN
        ALTER INDEX idx_nexai_req RENAME TO idx_enteri_ai_req;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_nexai_status') THEN
        ALTER INDEX idx_nexai_status RENAME TO idx_enteri_ai_status;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_nexai_invite_token') THEN
        ALTER INDEX idx_nexai_invite_token RENAME TO idx_enteri_ai_invite_token;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_nexai_invite_attempt_status') THEN
        ALTER INDEX idx_nexai_invite_attempt_status RENAME TO idx_enteri_ai_invite_attempt_status;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_nexai_session_render_sweep') THEN
        ALTER INDEX idx_nexai_session_render_sweep RENAME TO idx_enteri_ai_session_render_sweep;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_proc_nexai') THEN
        ALTER INDEX idx_proc_nexai RENAME TO idx_proc_enteri_ai;
    END IF;
END $$""",
        # Column renames, guarded per table since not every deployment has
        # every optional integrity/discrepancy table yet.
        """DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='proctoring_session' AND column_name='nexai_session_id') THEN
        ALTER TABLE proctoring_session RENAME COLUMN nexai_session_id TO enteri_ai_session_id;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='proctoring_appeal' AND column_name='nexai_session_id') THEN
        ALTER TABLE proctoring_appeal RENAME COLUMN nexai_session_id TO enteri_ai_session_id;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='proctoring_appeal' AND column_name='nexai_invite_id') THEN
        ALTER TABLE proctoring_appeal RENAME COLUMN nexai_invite_id TO enteri_ai_invite_id;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='proctoring_termination_discrepancy' AND column_name='nexai_session_id') THEN
        ALTER TABLE proctoring_termination_discrepancy RENAME COLUMN nexai_session_id TO enteri_ai_session_id;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='proctoring_integrity_flag' AND column_name='nexai_session_id') THEN
        ALTER TABLE proctoring_integrity_flag RENAME COLUMN nexai_session_id TO enteri_ai_session_id;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='campus_candidate' AND column_name='nexai_session_id') THEN
        ALTER TABLE campus_candidate RENAME COLUMN nexai_session_id TO enteri_ai_session_id;
    END IF;
END $$""",
        # Data values — 'nexai_bot' was a live stage name stored in
        # application.status / stage_event.from_status/to_status and a
        # sla_config.config_key; must be migrated before the CHECK
        # constraint below stops allowing the old value.
        "UPDATE application SET status='enteri_ai_bot' WHERE status='nexai_bot'",
        "UPDATE stage_event SET from_status='enteri_ai_bot' WHERE from_status='nexai_bot'",
        "UPDATE stage_event SET to_status='enteri_ai_bot' WHERE to_status='nexai_bot'",
        "UPDATE sla_config SET config_key='stage_enteri_ai_bot' WHERE config_key='stage_nexai_bot'",
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'application'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE application ADD CONSTRAINT application_status_check
           CHECK (status IN (
               'applied','screen','enteri_ai_bot','shortlisted','interview',
               'documentation','offered','hired','rejected','on_hold'
           ))""",
        # interview.html tagged the bot's conversation turns with the
        # literal speaker value 'nexai'; bring historical transcripts in
        # line with the renamed frontend so old sessions still render
        # correctly.
        """UPDATE enteri_ai_session
           SET conversation = (
               SELECT jsonb_agg(
                   CASE WHEN elem->>'speaker' = 'nexai'
                        THEN jsonb_set(elem, '{speaker}', '"enteri_ai"')
                        ELSE elem END
               )
               FROM jsonb_array_elements(conversation) elem
           )
           WHERE conversation IS NOT NULL AND conversation::text LIKE '%\"nexai\"%'""",
        # ── Migration 100: platform-superadmin / company-admin boolean flags
        # (2026-08). Enternly's platform_admin/company_admin roles today are
        # pure aliases layered onto the single `role` string -- no dedicated
        # platform console, tenant CRUD, or cross-tenant reach exists yet.
        # This adds two explicit, authoritative boolean flags on app_user so
        # gating no longer depends on parsing/enumerating role strings: an
        # account can be flagged is_platform_superadmin (Enternstech staff
        # managing the whole tenant roster) and/or is_company_admin (a
        # customer's own super admin) independently of whatever `role` value
        # it also carries for labeling/nav purposes. Existing platform_admin/
        # company_admin/admin accounts are backfilled onto the flags so no
        # one is locked out; going forward, new gating reads the flags, not
        # the role string. Also adds app_user.last_login_at (no prior column
        # tracked this anywhere). See database/62_*.sql for the doc-only
        # snapshot of this block.
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS is_platform_superadmin BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS is_company_admin BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ",
        """UPDATE app_user SET is_platform_superadmin = TRUE
           WHERE role IN ('platform_admin','admin')
             AND tenant_id = '00000000-0000-0000-0000-000000000001'""",
        """UPDATE app_user SET is_company_admin = TRUE
           WHERE role IN ('company_admin','admin')
             AND tenant_id <> '00000000-0000-0000-0000-000000000001'""",
        # ── Migration 101: platform-managed tenant fields + placement_officer
        # role (2026-08). The platform console (Feature C) needs to create,
        # classify (Company vs College), brand, and lifecycle-manage tenants
        # -- none of tenant_type/tenant_code/logo_url/primary_colour/
        # subscription dates/grace period/is_deleted existed before this.
        # tenant.plan is intentionally NOT touched here (kept as the single
        # subscription-plan column per design decision, just read/written
        # under the alias "subscription_plan" in Python, never duplicated in
        # SQL). is_deleted is a soft-delete flag only -- tenant rows are
        # never hard-deleted. Also widens app_user.role to add
        # placement_officer (a College tenant's own campus recruiting
        # contact, created via POST /api/platform/tenants/{id}/placement-
        # officers, scoped to that one tenant like any other role -- no such
        # role existed anywhere in the codebase before this). See
        # database/63_*.sql for the doc-only snapshot of this block.
        """ALTER TABLE tenant ADD COLUMN IF NOT EXISTS tenant_type TEXT NOT NULL DEFAULT 'Company'
           CHECK (tenant_type IN ('Company','College'))""",
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS tenant_code TEXT",
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS logo_url TEXT",
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS primary_colour TEXT",
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS subscription_start_date DATE",
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS subscription_end_date DATE",
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS grace_period_days INT NOT NULL DEFAULT 0",
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE",
        "UPDATE tenant SET tenant_code = 'ET_0001' WHERE tenant_code IS NULL AND slug = 'enternstech'",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_code ON tenant(tenant_code)
           WHERE tenant_code IS NOT NULL""",
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'app_user'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%role%'
    LOOP
        EXECUTE 'ALTER TABLE app_user DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE app_user ADD CONSTRAINT app_user_role_check
           CHECK (role IN ('platform_admin','company_admin','admin','ta_manager',
                            'recruiter','hiring_manager','bu_head','director',
                            'interviewer','hrbp','placement_officer'))""",
        # ── Migration 102: module_catalog / tenant_module_config /
        # subscription_plan_config (2026-08). Feature D's tenant-level module
        # gate: a module must be enabled for the TENANT before any of its
        # users -- even a company admin -- can use it; module_access.py's
        # existing per-recruiter delegation is the inner gate on top of this
        # outer one. subscription_plan_config is created here (not deferred
        # to the later Subscriptions commit) so the plan/module live
        # constraint (a tenant can never enable a module its plan doesn't
        # allow) ships complete in the same migration as the tables it
        # constrains, rather than as a dangling half-feature. Every existing
        # tenant is defaulted to all-modules-enabled so nothing regresses on
        # deploy. See database/64_*.sql for the doc-only snapshot.
        """CREATE TABLE IF NOT EXISTS module_catalog (
            key           TEXT PRIMARY KEY,
            label         TEXT NOT NULL,
            "group"       TEXT,
            default_route TEXT,
            icon          TEXT,
            is_active     BOOLEAN NOT NULL DEFAULT TRUE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS tenant_module_config (
            tenant_id    UUID NOT NULL REFERENCES tenant(id),
            module_key   TEXT NOT NULL REFERENCES module_catalog(key),
            is_enabled   BOOLEAN NOT NULL DEFAULT TRUE,
            enabled_at   TIMESTAMPTZ,
            disabled_at  TIMESTAMPTZ,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, module_key)
        )""",
        """CREATE TABLE IF NOT EXISTS subscription_plan_config (
            plan_name            TEXT PRIMARY KEY,
            allowed_modules_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            price_monthly        NUMERIC,
            price_yearly         NUMERIC,
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        # '[]' = no restriction (every module allowed) -- the safe
        # interpretation for a plan that pre-dates this feature, so the
        # existing 'standard' plan doesn't suddenly lock every tenant out
        # the moment this migration runs. A genuinely restrictive plan is
        # created explicitly afterward via the Subscriptions screen's CRUD.
        """INSERT INTO subscription_plan_config (plan_name, allowed_modules_json)
           VALUES ('standard', '[]'::jsonb) ON CONFLICT (plan_name) DO NOTHING""",
        """INSERT INTO module_catalog (key, label, "group", default_route, icon) VALUES
             ('vendors','Vendor Management','Admin','vendors','🤝'),
             ('form_fields','Application Form Fields','Admin','form_fields','🧾'),
             ('req_approvals','Requisition Approvals','Admin','ta_req_approvals','📝'),
             ('organisation','Organisation','Admin','organisation','🏢'),
             ('sla_settings','SLA Settings','Admin','sla_settings','⏱'),
             ('chain_templates','Approval Chain Templates','Admin','chain_templates','⛓'),
             ('email_templates','Email Templates','Admin','email_templates','✉️'),
             ('campus_hiring','Campus Hiring','Sourcing','campus_hiring','🎓'),
             ('enteri_ai_tracker','Enteri AI','Sourcing','enteri_ai_tracker','🤖'),
             ('kpi_dashboard','KPI Dashboard','Analytics','kpi_dashboard','📈'),
             ('gamification','Leaderboard','Analytics','gamification','🏆'),
             ('proctoring_review','Proctoring','Pipeline','proctoring_review','🔍'),
             ('hiring_plan','Hiring Plan','Sourcing','hiring_plan','📑'),
             ('cv_repository','CV Repository','Admin','cv_repository','📂'),
             ('ai_scorecard','AI Scorecard','Admin','ai_scorecard','🎯'),
             ('no_poach','No Poach List','Admin','no_poach','🚫')
           ON CONFLICT (key) DO NOTHING""",
        """INSERT INTO tenant_module_config (tenant_id, module_key, is_enabled, enabled_at)
           SELECT t.id, m.key, TRUE, now() FROM tenant t CROSS JOIN module_catalog m
           ON CONFLICT (tenant_id, module_key) DO NOTHING""",
        # ── Migration 103: support_ticket_reply (2026-08). The platform
        # console's cross-tenant Issues & Tickets screen (Feature G) needs a
        # threaded reply history on top of support_ticket's existing single
        # `reply` field. See database/65_*.sql for the doc-only snapshot.
        """CREATE TABLE IF NOT EXISTS support_ticket_reply (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id  UUID NOT NULL REFERENCES support_ticket(id),
            author_id  UUID NOT NULL REFERENCES app_user(id),
            body       TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ticket_reply_ticket ON support_ticket_reply(ticket_id, created_at)",
        # ── Migration 104: platform_settings (2026-08). A small KV table for
        # platform-console-configurable defaults (default new-tenant plan,
        # default enabled modules) -- deliberately separate from
        # system_status, which is reserved for background-worker
        # heartbeats/kill-switches, not admin-configurable settings. See
        # database/66_*.sql for the doc-only snapshot.
        """CREATE TABLE IF NOT EXISTS platform_settings (
            key        TEXT PRIMARY KEY,
            value      JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
    ]
    for sql in migrations:
        try:
            query(sql, fetch=False)
        except Exception as exc:
            # Log but don't crash — a failed migration shouldn't block startup
            print(f"[auto-migrate] WARNING: {exc}")

    # Seed built-in email template defaults (idempotent — skips existing rows)
    try:
        from .services.email_templates import ensure_defaults as _ensure_email_defaults, fix_legacy_templates as _fix_legacy_templates
        _ensure_email_defaults()
        _fix_legacy_templates()
    except Exception as _edt_exc:
        print(f"[auto-migrate] email template seed failed: {_edt_exc}")

    # Ensure CV store directory exists
    import os as _os
    _cv_store = _os.environ.get("CV_STORE_DIR", "/app/cv_store")
    _os.makedirs(_cv_store, exist_ok=True)
    _cv_inbox = _os.environ.get("CV_INBOX_DIR", "/app/cv_inbox")
    _os.makedirs(_cv_inbox, exist_ok=True)
    _jd_store = _os.environ.get("JD_STORE_DIR", "/app/jd_store")
    _os.makedirs(_jd_store, exist_ok=True)


# Background worker tasks — kept referenced so they can't be silently
# garbage-collected mid-run (a Task with no strong reference is eligible for
# GC per asyncio semantics, which would kill the worker with no traceback).
_bg_tasks: set = set()


def _track_bg_task(task, name: str) -> None:
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

    def _log_if_failed(t):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            print(f"[startup] WARNING: background task '{name}' crashed: {exc}")
            # print() alone is invisible to anyone not tailing stdout at the
            # exact moment it happens -- persist so a crashed loop is
            # discoverable later, same pattern as email_ingest.py's status.
            try:
                query(
                    """INSERT INTO system_status (key, value) VALUES (%s, %s)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                    [f"bg_task_status:{name}", f"crashed|{exc}"[:500]],
                    fetch=False,
                )
            except Exception:
                pass

    task.add_done_callback(_log_if_failed)


@app.on_event("startup")
async def _tune_sync_thread_pool():
    """
    Almost every route handler in this app is a plain `def`, not `async def`
    (routers call synchronous psycopg2 code directly) — Starlette runs those
    via AnyIO's thread-pool offload, which defaults to a 40-thread cap per
    process regardless of DB capacity or worker count. Under concurrent load
    that cap becomes the bottleneck long before the (now-pooled, see db.py)
    DB connections do: requests queue for a thread slot first. Raised here,
    at process startup, to a size configurable independently of DB_POOL_MAX.
    """
    import anyio
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = int(os.getenv("SYNC_THREAD_POOL_SIZE", "64"))


# Postgres advisory-lock key identifying "the background-workers singleton
# lock" — arbitrary fixed 64-bit int, just needs to be unique within this DB.
_BG_WORKER_LOCK_KEY = 727384910
_bg_worker_lock_conn = None  # kept open for the process's lifetime


def _try_acquire_bg_worker_lock() -> bool:
    """
    `uvicorn --workers=N` forks N independent OS processes, each running its
    own full copy of this module — including every @app.on_event("startup")
    handler. Without a guard, N worker processes each started their own
    copy of every background loop (cv_enricher, recruiter_email_worker,
    email_ingest, campus_email_worker, enteri_ai_render_worker,
    linkedin_reminder_worker): N x the Groq API calls (hitting rate limits
    far sooner than the code's own throttling assumed), N x IMAP polling of
    the same mailboxes, N x redundant CPU-heavy text extraction of the same
    messages — a major, constant, avoidable multiplier on both CPU and the
    shared external rate limit, on top of whatever load real traffic adds.

    A Postgres advisory lock lets exactly one worker process "win" and run
    the background loops; the rest skip starting them and just serve HTTP
    requests. This uses a dedicated connection that is never returned to
    the pool and stays open for the process's lifetime — the lock is tied
    to the SESSION that took it, so a pooled connection would release it
    the moment that connection went back to the pool for an unrelated query.
    """
    global _bg_worker_lock_conn
    import psycopg2
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME", "oneclickhire"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", [_BG_WORKER_LOCK_KEY])
        acquired = cur.fetchone()[0]
        cur.close()
        if acquired:
            _bg_worker_lock_conn = conn  # keep open — releasing it drops the lock
            try:
                query("DELETE FROM system_status WHERE key = 'bg_lock_last_error'", [], fetch=False)
            except Exception:
                pass
        else:
            conn.close()
        return bool(acquired)
    except Exception as exc:
        print(f"[startup] could not acquire background-worker singleton lock: {exc}")
        # Persisted (not just printed) for the same reason _track_bg_task
        # persists a crashed-task reason below: a connection-level failure
        # here (vs. a losing pg_try_advisory_lock race, which is normal and
        # not logged as an error) means every worker's retry will keep
        # failing the same way until whatever this says gets fixed —
        # visible via GET /api/cv/stats without needing server log access.
        try:
            query(
                """INSERT INTO system_status (key, value, updated_at) VALUES (%s, %s, now())
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                ["bg_lock_last_error", str(exc)[:500]],
                fetch=False,
            )
        except Exception:
            pass
        return False


def _launch_background_tasks():
    """Start every background asyncio task. Only ever called from the one
    worker process that holds the singleton lock (see
    _bg_worker_lock_watchdog below)."""
    import asyncio as _asyncio

    try:
        from .services.cv_enricher import start_enricher as _start_cv_enricher
        _track_bg_task(_asyncio.create_task(_start_cv_enricher()), "cv_enricher")
    except Exception as exc:
        print(f"[startup] cv_enricher failed to start: {exc}")
    try:
        from .services.email_ingest import start_email_poller as _start_email_poller
        _track_bg_task(_asyncio.create_task(_start_email_poller()), "email_ingest")
    except Exception as exc:
        print(f"[startup] email_ingest failed to start: {exc}")
    try:
        from .services.campus_email_worker import start_campus_email_worker as _start_campus_email_worker
        _track_bg_task(_asyncio.create_task(_start_campus_email_worker()), "campus_email_worker")
    except Exception as exc:
        print(f"[startup] campus_email_worker failed to start: {exc}")
    try:
        from .services.enteri_ai_render_worker import start_enteri_ai_render_worker as _start_enteri_ai_render_worker
        _track_bg_task(_asyncio.create_task(_start_enteri_ai_render_worker()), "enteri_ai_render_worker")
    except Exception as exc:
        print(f"[startup] enteri_ai_render_worker failed to start: {exc}")
    try:
        from .services.linkedin_reminder_worker import start_linkedin_reminder_worker as _start_linkedin_reminder_worker
        _track_bg_task(_asyncio.create_task(_start_linkedin_reminder_worker()), "linkedin_reminder_worker")
    except Exception as exc:
        print(f"[startup] linkedin_reminder_worker failed to start: {exc}")
    try:
        from .services.recruiter_email_worker import start_recruiter_email_worker as _start_recruiter_email_worker
        _track_bg_task(_asyncio.create_task(_start_recruiter_email_worker()), "recruiter_email_worker")
    except Exception as exc:
        print(f"[startup] recruiter_email_worker failed to start: {exc}")
    try:
        from .services.hm_feedback_reminder_worker import start_hm_feedback_reminder_worker as _start_hm_feedback_reminder_worker
        _track_bg_task(_asyncio.create_task(_start_hm_feedback_reminder_worker()), "hm_feedback_reminder_worker")
    except Exception as exc:
        print(f"[startup] hm_feedback_reminder_worker failed to start: {exc}")


_BG_LOCK_RETRY_SECONDS = 30


async def _bg_worker_lock_watchdog():
    """
    Keeps retrying pg_try_advisory_lock every _BG_LOCK_RETRY_SECONDS until
    this process wins it, then launches the background tasks once and
    returns. The one-shot version of this (a single attempt at startup,
    give up forever on failure) had two failure modes with no recovery:
    a transient DB hiccup during container boot meant NO worker process
    ever tried again, and if the winning process later died (crash, OOM,
    DB restart dropping its connection and releasing the lock), nothing
    else picked it back up either — every background loop (cv_enricher,
    email ingest, campus/enteri-ai/linkedin/recruiter-email workers) stayed
    dead until someone noticed and manually restarted the whole app. The
    other N-1 worker processes now just keep polling for the lock in the
    background instead of trying once and giving up, so a dead holder's
    lock gets picked up by another worker within one retry interval.
    """
    import asyncio as _asyncio
    while True:
        won_lock = await _asyncio.to_thread(_try_acquire_bg_worker_lock)
        if won_lock:
            print("[startup] this worker process won the background-worker singleton lock")
            _launch_background_tasks()
            return
        await _asyncio.sleep(_BG_LOCK_RETRY_SECONDS)


@app.on_event("startup")
async def _start_background_services():
    """Kick off the lock watchdog as a background task — every worker
    process runs this, but only the one that eventually holds the lock
    launches the actual background loops (see _bg_worker_lock_watchdog)."""
    import asyncio as _asyncio
    _asyncio.create_task(_bg_worker_lock_watchdog())

_UPLOADS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads")
)
os.makedirs(_UPLOADS_DIR, exist_ok=True)

# Resolve the frontend directory.
_FRONTEND_DIR = os.environ.get(
    "FRONTEND_DIR",
    os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend")
    ),
)
_ASSETS_DIR = os.path.join(_FRONTEND_DIR, "assets")
if os.path.isdir(_ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")

# Local avatar video storage — used when GCS_BUCKET_NAME is not set (dev / orb-only mode).
# Videos are written here by prerender.py and served at /media/avatar_videos/<filename>.
_AVATAR_MEDIA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "media", "avatar_videos")
)
os.makedirs(_AVATAR_MEDIA_DIR, exist_ok=True)
app.mount("/media/avatar_videos", StaticFiles(directory=_AVATAR_MEDIA_DIR), name="avatar_videos")

_RESUME_MIME = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
}

# Paths that do NOT require a JWT
_PUBLIC = {
    "/", "/login", "/api/health", "/api/auth/login",
    "/platform-login", "/platform-admin", "/api/platform/auth/login",
    "/set-password",
    "/candidate-portal",
    "/vendor-portal",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/reset-token/validate",
    "/enteri-ai-interview",
    # Candidate-facing Enteri AI interview endpoints — token-based, no JWT.
    # Staff-only Enteri AI routes (invite/send, bulk-invite, resend-invite,
    # invite-tracker, etc.) are deliberately NOT listed here — they enforce
    # their own JWT auth and must stay behind the middleware.
    "/api/enteri-ai/invite/validate",
    "/api/enteri-ai/invite/begin",
    "/api/enteri-ai/invite/render-status",
    "/api/enteri-ai/invite/converse",
    "/api/enteri-ai/invite/terminate",
    "/api/enteri-ai/invite/appeal",
    "/api/enteri-ai/invite/transcribe",
    "/interview-schedule",
    # Candidate-facing self-scheduling slot-picker — token-based, no JWT
    "/reschedule",
    # Self-service interview reschedule page (candidate or panelist) — token-based, no JWT
    # Vendor portal login — vendor receives JWT after this call
    "/api/vendors/portal/login",
    # Candidate portal login
    "/api/candidate/portal/login",
    # Google OAuth callback -- Google redirects the admin's browser here
    # directly with just ?code=&state=, no Authorization header; authenticated
    # via the one-time state nonce instead (see google_calendar_api.callback).
    "/api/google/callback",
    # LinkedIn OAuth callback -- same shape as the Google one above, but for
    # a candidate's own LinkedIn connect (see candidate_portal_api.portal_linkedin_callback).
    "/api/candidate/linkedin/callback",
    # One-click unsubscribe link in the LinkedIn reminder email footer --
    # deliberately no login required, authenticated via its own per-candidate token.
    "/api/candidate/linkedin/unsubscribe",
}
_PUBLIC_PREFIXES = (
    "/assets/",
    "/api/enteri-ai/invite/submit/",       # /api/enteri-ai/invite/submit/{session_id}
    "/api/proctoring/candidate/",      # candidate token-auth proctoring endpoints
    "/api/proctoring/session/",        # candidate session-secret-auth proctoring ledger endpoints (Phase 2)
    "/api/campus/session/",            # campus resume upload + is-campus check (token-auth, no JWT)
    "/api/scheduling/pick",            # candidate slot validate/confirm — token-auth, no JWT
    "/api/scheduling/reschedule",       # self-service reschedule validate/confirm — token-auth, no JWT
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    else:
        # A new top-level tab (View/Download CV, view resume) can't attach a
        # custom Authorization header, so those links carry the JWT as a
        # ?token= query param instead (see viewAuth() in the frontend and
        # _cv_auth's identical fallback) -- accept it here too, or this
        # middleware 401s the request before it ever reaches the route.
        token = request.query_params.get("token", "")
    if not token:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    try:
        request.state.user = _decode(token)
    except Exception:
        return JSONResponse(status_code=401, content={"detail": "Invalid or expired token"})
    return await call_next(request)


@app.exception_handler(psycopg2.Error)
def db_error_handler(request: Request, exc: psycopg2.Error):
    return JSONResponse(status_code=400,
                        content={"error": "database constraint or bad reference",
                                 "detail": str(exc).splitlines()[0]})


# ---------------- resume serving ----------------
@app.get("/api/resume/{filename}")
def serve_resume(filename: str, request: Request):
    """Authenticated endpoint to view or download a candidate resume.
    hiring_manager added so a round's HM can see the candidate's CV as part
    of cross-round context carry-forward (see scorecard_api's consolidated
    panel-feedback endpoint)."""
    role = request.state.user.get("role", "")
    if role not in ("admin", "ta_manager", "recruiter", "hiring_manager"):
        return JSONResponse(status_code=403, content={"detail": "Not authorised to view resumes"})

    # Prevent path traversal
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "Invalid filename")

    file_path = os.path.join(_UPLOADS_DIR, safe_name)
    if not os.path.isfile(file_path):
        # Fallback: new resumes are stored in cv_store (single canonical location)
        _cv_store_dir = os.environ.get("CV_STORE_DIR", "/app/cv_store")
        file_path = os.path.join(_cv_store_dir, safe_name)
        if not os.path.isfile(file_path):
            raise HTTPException(404, "Resume file not found")

    ext = os.path.splitext(safe_name)[1].lower()
    media_type = _RESUME_MIME.get(ext, "application/octet-stream")

    # PDFs open inline in the browser; other formats force download
    disposition = "inline" if ext == ".pdf" else "attachment"
    return FileResponse(
        file_path,
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{safe_name}"'},
    )


# ---------------- health ----------------
# Dedicated executor for the health check, deliberately separate from the
# app's general sync-thread-pool (anyio's default executor, sized by
# SYNC_THREAD_POOL_SIZE and shared by every plain `def` route handler). A
# CPU-heavy burst (bulk CV scanning) can fully occupy that shared pool,
# which queued the health check behind everything else and made the
# container look "unhealthy" for a few seconds even though the process
# itself was fine — a probe that shares a resource with the thing it's
# supposed to detect trouble in isn't a useful probe. Two threads is
# already generous for one "SELECT 1".
import concurrent.futures as _cf
_health_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="health-check")


@app.get("/api/health")
async def health():
    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()
    try:
        await _asyncio.wait_for(
            loop.run_in_executor(_health_executor, query_one, "SELECT 1 AS ok"),
            timeout=3.0,
        )
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "degraded", "error": str(e)})


# Reference-data lookups needed to populate the requisition create/edit modal.
# Any role that can create or edit a requisition (see canCreate in the
# frontend) must be able to read these, or their dropdowns silently render
# empty for that role.
_REQ_FORM_ROLES = ("admin", "ta_manager", "recruiter", "hiring_manager", "hrbp")


def _assert_can_view_req_form_data(request: Request) -> None:
    assert_staff(request.state.user)
    if request.state.user.get("role") not in _REQ_FORM_ROLES:
        raise HTTPException(403, "Not authorised")


# ---------------- reference / config ----------------
@app.get("/api/users")
def users(request: Request):
    _assert_can_view_req_form_data(request)
    return query(
        "SELECT id, full_name, email, role FROM app_user WHERE is_active = true ORDER BY full_name"
    )


@app.get("/api/bands")
def bands(request: Request):
    _assert_can_view_req_form_data(request)
    return query(
        """SELECT b.id, b.code, b.rank, b.description, b.is_active,
               COALESCE(
                   (SELECT array_agg(bc.company_id) FROM band_company bc WHERE bc.band_id = b.id),
                   ARRAY[]::uuid[]
               ) AS company_ids
           FROM band b ORDER BY b.rank"""
    )


@app.get("/api/group-companies")
def group_companies_list(request: Request):
    _assert_can_view_req_form_data(request)
    return query("SELECT id, name, domain FROM group_company WHERE is_active = true ORDER BY name")


@app.get("/api/business-units")
def business_units(request: Request, company_id: str = None):
    _assert_can_view_req_form_data(request)
    if company_id:
        return query(
            """SELECT bu.id, bu.name, gc.id AS company_id, gc.name AS company
               FROM business_unit bu JOIN group_company gc ON gc.id = bu.company_id
               WHERE bu.is_active = true AND gc.id = %s
               ORDER BY bu.name""",
            [company_id],
        )
    return query(
        """SELECT bu.id, bu.name, gc.id AS company_id, gc.name AS company
           FROM business_unit bu JOIN group_company gc ON gc.id = bu.company_id
           WHERE bu.is_active = true
           ORDER BY gc.name, bu.name"""
    )


@app.get("/api/requisitions")
def requisitions(request: Request):
    assert_staff(request.state.user)
    if request.state.user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    # Exclude pending_ta_approval reqs from the public listing used by the apply modal
    return query(
        """SELECT r.id, r.title, r.status, r.roll_type, r.fiscal_year,
                  r.budgeted_ctc, b.code AS band, bu.name AS business_unit,
                  (SELECT COUNT(*) FROM application  WHERE requisition_id = r.id) AS in_pipeline,
                  (SELECT COUNT(*) FROM round_config WHERE requisition_id = r.id) AS levels
           FROM requisition r
           JOIN band b ON b.id = r.band_id
           JOIN business_unit bu ON bu.id = r.bu_id
           WHERE COALESCE(r.approval_status, 'approved') = 'approved'
           ORDER BY r.created_at DESC"""
    )


# ---------------- applications / pipeline ----------------
class ApplyIn(BaseModel):
    requisition_id: str
    full_name: str
    email: str
    phone: str | None = None
    gender: str = "undisclosed"
    resume_text: str = ""
    years_experience: float | None = None
    source: str = "career_site"
    # Extended informational fields — captured for recruiter context only.
    # These do NOT affect the screening score or any algorithm.
    current_company: str | None = None
    current_designation: str | None = None
    current_location: str | None = None
    current_ctc_fixed: float | None = None
    current_ctc_variable: float | None = None
    current_ctc_bonus: float | None = None
    expected_ctc_fixed: float | None = None
    expected_ctc_variable: float | None = None
    expected_ctc_bonus: float | None = None
    notice_period_days: int | None = None
    willing_to_relocate: bool | None = None


from .services.candidate_dedup import (
    find_existing_candidate as _find_existing_candidate,
    dedup_or_create_candidate as _dedup_or_create_candidate,
)


def _sum_ctc(*parts):
    """Sum CTC components, returning None if all parts are None/zero."""
    total = sum(p for p in parts if p is not None)
    return total if total > 0 else None


def _parse_relocate(val) -> bool | None:
    """Convert FormData string ('yes'/'no'/'open'/'') to bool or None."""
    if isinstance(val, bool):
        return val
    if val in ("yes", "true", "1"):
        return True
    if val in ("no", "false", "0"):
        return False
    return None


def _maybe_issue_candidate_invite(cand_id: str, email: str, full_name: str) -> None:
    """
    If a candidate_user does not yet exist for this candidate, create one and
    send a set-password invite so they can access the portal.
    Safe to call multiple times — idempotent.
    """
    try:
        existing = query_one(
            "SELECT id FROM candidate_user WHERE candidate_id=%s", [cand_id]
        )
        if existing:
            return
        cand_row = query_one("SELECT tenant_id FROM candidate WHERE id=%s", [cand_id])
        tenant_id = (cand_row or {}).get("tenant_id")
        cu = query_one(
            """INSERT INTO candidate_user (candidate_id, email, tenant_id)
               VALUES (%s, %s, %s) ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id""",
            [cand_id, email.lower().strip(), tenant_id],
        )
        if cu:
            from .routers.password_api import issue_invite_for_external_user
            issue_invite_for_external_user(str(cu["id"]), email, full_name, "candidate", tenant_id=tenant_id)
            _prefill_profile_from_resume(cand_id)
    except Exception as exc:
        print(f"[candidate-portal] Auto-invite failed for {email}: {exc}")


def _prefill_profile_from_resume(cand_id: str) -> None:
    """
    First-time portal account creation: if this candidate already has a
    resume on file (from application intake), parse it and fill in any
    empty My Profile fields (given name, phone, skills, work experience,
    education) — see candidate_profile_parser.apply_parsed_profile for the
    fill-empty-only merge policy. Best-effort: any failure here must never
    block account creation, so it's caught and logged, never raised.
    """
    try:
        cv_row = query_one(
            """SELECT cr.raw_text FROM cv_repository cr
               JOIN candidate c ON c.cv_repository_id = cr.id
               WHERE c.id=%s AND cr.raw_text IS NOT NULL AND cr.raw_text != ''""",
            [cand_id],
        )
        if not cv_row:
            return
        from .services.candidate_profile_parser import parse_resume_to_profile, apply_parsed_profile
        parsed = parse_resume_to_profile(cv_row["raw_text"])
        if parsed:
            apply_parsed_profile(cand_id, parsed)
    except Exception as exc:
        print(f"[candidate-portal] profile prefill from resume failed for {cand_id} (non-fatal): {exc}")


def _send_jd_email(candidate_name: str, candidate_email: str, req_id: str) -> None:
    """Thin alias — see services/jd_email.py (shared with the CV Repository
    pool → requisition mapping path in routers/cv_api.py)."""
    from .services.jd_email import send_application_received_jd_email
    send_application_received_jd_email(candidate_name, candidate_email, req_id)


def _store_extended_fields(application_id: str, **kwargs):
    """
    Update the informational extended columns on an application row.
    CTC totals are auto-computed. Only non-None kwargs are written.
    Does NOT touch match_score or any screening column.
    """
    cols_vals = [
        ("current_company",       kwargs.get("current_company")),
        ("current_designation",   kwargs.get("current_designation")),
        ("current_location",      kwargs.get("current_location")),
        ("current_ctc_fixed",     kwargs.get("current_ctc_fixed")),
        ("current_ctc_variable",  kwargs.get("current_ctc_variable")),
        ("current_ctc_bonus",     kwargs.get("current_ctc_bonus")),
        ("current_ctc_total",     _sum_ctc(
            kwargs.get("current_ctc_fixed"),
            kwargs.get("current_ctc_variable"),
            kwargs.get("current_ctc_bonus"),
        )),
        ("expected_ctc_fixed",    kwargs.get("expected_ctc_fixed")),
        ("expected_ctc_variable", kwargs.get("expected_ctc_variable")),
        ("expected_ctc_bonus",    kwargs.get("expected_ctc_bonus")),
        ("expected_ctc_total",    _sum_ctc(
            kwargs.get("expected_ctc_fixed"),
            kwargs.get("expected_ctc_variable"),
            kwargs.get("expected_ctc_bonus"),
        )),
        ("notice_period_days",    kwargs.get("notice_period_days")),
        ("willing_to_relocate",   kwargs.get("willing_to_relocate")),
    ]
    provided = [(col, val) for col, val in cols_vals if val is not None]
    if not provided:
        return
    sets = ", ".join(f"{col} = %s" for col, _ in provided)
    vals = [val for _, val in provided] + [application_id]
    query(f"UPDATE application SET {sets} WHERE id = %s", vals, fetch=False)


@app.post("/api/apply")
def apply(payload: ApplyIn):
    """Text-paste application: create/reuse candidate → auto-screen."""
    _req_approval = query_one(
        "SELECT approval_status, tenant_id FROM requisition WHERE id=%s", [payload.requisition_id]
    )
    if _req_approval and (_req_approval.get("approval_status") or "approved") != "approved":
        raise HTTPException(403, "This requisition is not open for applications yet.")
    _apply_tenant_id = (_req_approval or {}).get("tenant_id")
    try:
        pipeline._check_no_poach_block(payload.current_company, payload.requisition_id)
    except pipeline.NoPoachBlockedError as exc:
        raise HTTPException(409, str(exc))
    cand_id = _dedup_or_create_candidate(
        full_name=payload.full_name,
        email=payload.email,
        phone=payload.phone,
        gender=payload.gender,
        source=payload.source,
        resume_url=None,
        requisition_id=payload.requisition_id,
    )
    app_row = pipeline.intake_and_screen(
        payload.requisition_id, cand_id, payload.resume_text, payload.years_experience,
        current_company=payload.current_company,
    )
    _store_extended_fields(
        app_row["id"],
        current_company=payload.current_company,
        current_designation=payload.current_designation,
        current_location=payload.current_location,
        current_ctc_fixed=payload.current_ctc_fixed,
        current_ctc_variable=payload.current_ctc_variable,
        current_ctc_bonus=payload.current_ctc_bonus,
        expected_ctc_fixed=payload.expected_ctc_fixed,
        expected_ctc_variable=payload.expected_ctc_variable,
        expected_ctc_bonus=payload.expected_ctc_bonus,
        notice_period_days=payload.notice_period_days,
        willing_to_relocate=payload.willing_to_relocate,
    )
    # Ingest pasted resume text into the CV Repository too, so every
    # candidate — file upload or paste — is searchable/viewable in one place.
    if payload.resume_text and payload.resume_text.strip():
        try:
            _cv_result = _cv_ingest_and_link(
                data=payload.resume_text.encode("utf-8"),
                filename=f"{payload.full_name or 'candidate'}_resume.txt",
                source="application",
                uploaded_by=None,
                candidate_id=str(cand_id),
                req_id=payload.requisition_id,
                tenant_id=_apply_tenant_id,
            )
            _cv_id = _cv_result.get("cv_id") if _cv_result else None
            if _cv_id:
                _cv_row = query_one(
                    "SELECT file_path FROM cv_repository WHERE id=%s", [_cv_id]
                )
                if _cv_row and _cv_row.get("file_path"):
                    query(
                        "UPDATE candidate SET resume_url=%s WHERE id=%s",
                        [_cv_row["file_path"], str(cand_id)],
                        fetch=False,
                    )
        except Exception as _cv_exc:
            print(f"[cv-ingest] Failed to link pasted resume for candidate {cand_id}: {_cv_exc}")
    _send_jd_email(payload.full_name, payload.email, payload.requisition_id)
    _maybe_issue_candidate_invite(cand_id, payload.email, payload.full_name)
    return {"application_id": app_row["id"], "match_score": app_row["match_score"],
            "breakdown": app_row["score_breakdown"]}


_ALLOWED_RESUME_TYPES = {".pdf", ".docx", ".doc", ".jpg", ".jpeg", ".png"}


@app.post("/api/parse-resume-contact")
async def parse_resume_contact(file: UploadFile = File(...)):
    """Parse a resume file and return extracted contact info for form pre-fill."""
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in _ALLOWED_RESUME_TYPES:
        raise HTTPException(400, f"Unsupported file type '{suffix}'.")
    file_bytes = await file.read()
    try:
        text, _ = _parse_resume(file_bytes, file.filename or "")
    except NotImplementedError:
        raise HTTPException(422, "Image files are not supported as resumes; upload PDF or Word.")
    from .services.resume_parser import extract_contact_info
    return extract_contact_info(text)


@app.post("/api/apply/upload")
async def apply_upload(
    requisition_id: str = Form(...),
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    gender: str = Form("undisclosed"),
    years_experience: float = Form(None),
    source: str = Form("career_site"),
    # Extended informational fields — not used in screening
    current_company: str = Form(""),
    current_designation: str = Form(""),
    current_location: str = Form(""),
    current_ctc_fixed: float = Form(None),
    current_ctc_variable: float = Form(None),
    current_ctc_bonus: float = Form(None),
    expected_ctc_fixed: float = Form(None),
    expected_ctc_variable: float = Form(None),
    expected_ctc_bonus: float = Form(None),
    notice_period_days: int = Form(None),
    willing_to_relocate: str = Form(""),
    file: UploadFile = File(...),
):
    """File-upload path: extract text from PDF/Word, dedup check, then auto-screen."""
    _req_approval_u = query_one(
        "SELECT approval_status, tenant_id FROM requisition WHERE id=%s", [requisition_id]
    )
    if _req_approval_u and (_req_approval_u.get("approval_status") or "approved") != "approved":
        raise HTTPException(403, "This requisition is not open for applications yet.")
    _apply_upload_tenant_id = (_req_approval_u or {}).get("tenant_id")
    try:
        pipeline._check_no_poach_block(current_company or None, requisition_id)
    except pipeline.NoPoachBlockedError as exc:
        raise HTTPException(409, str(exc))
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in _ALLOWED_RESUME_TYPES:
        raise HTTPException(
            400, f"Unsupported file type '{suffix or 'none'}'. Upload a PDF or Word document."
        )

    file_bytes = await file.read()
    try:
        resume_text, warning = _parse_resume(file_bytes, file.filename or "")
    except NotImplementedError:
        raise HTTPException(422, "Image files are not supported as resumes; upload PDF or Word.")

    # No save to _UPLOADS_DIR — cv_store is the single canonical location.
    # Candidate is created first (needed for ingest), resume_url updated after.
    cand_id = _dedup_or_create_candidate(
        full_name=full_name,
        email=email,
        phone=phone or None,
        gender=gender,
        source=source,
        resume_url=None,
        requisition_id=requisition_id,
    )
    app_row = pipeline.intake_and_screen(
        requisition_id, cand_id, resume_text, years_experience, len(file_bytes),
        current_company=current_company or None,
    )
    _store_extended_fields(
        app_row["id"],
        current_company=current_company or None,
        current_designation=current_designation or None,
        current_location=current_location or None,
        current_ctc_fixed=current_ctc_fixed,
        current_ctc_variable=current_ctc_variable,
        current_ctc_bonus=current_ctc_bonus,
        expected_ctc_fixed=expected_ctc_fixed,
        expected_ctc_variable=expected_ctc_variable,
        expected_ctc_bonus=expected_ctc_bonus,
        notice_period_days=notice_period_days,
        willing_to_relocate=_parse_relocate(willing_to_relocate),
    )
    # Ingest into CV Repository (single canonical file in cv_store, hash-deduped).
    # After ingest, point candidate.resume_url at the cv_store file so every
    # downstream consumer (profile view, rescreen, CSV export) resolves one path.
    try:
        _cv_result = _cv_ingest_and_link(
            data=file_bytes,
            filename=file.filename or f"{_uuid.uuid4()}{suffix}",
            source="application",
            uploaded_by=None,
            candidate_id=str(cand_id),
            req_id=requisition_id,
            tenant_id=_apply_upload_tenant_id,
        )
        _cv_id = _cv_result.get("cv_id") if _cv_result else None
        if _cv_id:
            _cv_row = query_one(
                "SELECT file_path FROM cv_repository WHERE id=%s", [_cv_id]
            )
            if _cv_row and _cv_row.get("file_path"):
                query(
                    "UPDATE candidate SET resume_url=%s WHERE id=%s",
                    [_cv_row["file_path"], str(cand_id)],
                    fetch=False,
                )
    except Exception as _cv_exc:
        print(f"[cv-ingest] Failed to link resume for candidate {cand_id}: {_cv_exc}")
    _send_jd_email(full_name, email, requisition_id)
    _maybe_issue_candidate_invite(cand_id, email, full_name)
    return {
        "application_id": app_row["id"],
        "match_score": app_row["match_score"],
        "breakdown": app_row["score_breakdown"],
        "resume_preview": resume_text[:400] if resume_text else "",
        "warning": warning,
    }


@app.post("/api/applications/{application_id}/bot-round")
def bot_round(application_id: str, request: Request):
    assert_staff(request.state.user)
    if request.state.user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    return pipeline.run_bot_round(application_id)


@app.get("/api/applications/{application_id}/screening-detail")
def screening_detail(application_id: str, request: Request):
    """Full screening breakdown for the 'Why this score?' recruiter panel."""
    assert_staff(request.state.user)
    if request.state.user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    row = query_one(
        """SELECT a.id, a.match_score, a.score_breakdown, a.ai_screen_detail,
                  a.avg_tenure_months, a.stability_score, a.stability_status,
                  a.ai_fit_score, a.status,
                  c.full_name AS candidate_name
           FROM application a
           JOIN candidate c ON c.id = a.candidate_id
           WHERE a.id = %s""",
        [application_id],
    )
    if not row:
        raise HTTPException(404, "application not found")
    return dict(row)


class ManualTenureIn(BaseModel):
    avg_tenure_months: float


@app.post("/api/applications/{application_id}/manual-tenure")
def manual_tenure(application_id: str, payload: ManualTenureIn, request: Request):
    """
    Recruiter submits average tenure (months) for a pending_manual application.
    Recomputes stability_score and match_score with full four-dimension weights.
    JWT-protected (middleware handles auth).
    """
    assert_staff(request.state.user)
    if request.state.user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    if payload.avg_tenure_months <= 0:
        raise HTTPException(400, "avg_tenure_months must be > 0")
    actor_id = getattr(request.state, "user", {}).get("sub")
    return pipeline.update_manual_tenure(application_id, payload.avg_tenure_months, actor_id)


@app.post("/api/applications/{application_id}/re-screen")
def re_screen(application_id: str, request: Request):
    """
    Deliberate recruiter action: re-run AI screening using the stored resume.
    Does not affect bot_score / combined_score / pipeline status.
    JWT-protected (middleware handles auth).
    """
    assert_staff(request.state.user)
    if request.state.user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    actor_id = getattr(request.state, "user", {}).get("sub")
    return pipeline.rescreen_application(application_id, actor_id)


@app.get("/api/requisitions/{requisition_id}/chart")
def chart(requisition_id: str, request: Request):
    assert_staff(request.state.user)
    if request.state.user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    return pipeline.top_chart(requisition_id)


# ---------------- scheduling ----------------
class ScheduleIn(BaseModel):
    application_id: str
    panel_emails: list[str] = []
    start_in_hours: int = 24
    duration_min: int = 45
    meet_link: str = ""


@app.post("/api/schedule")
def schedule(payload: ScheduleIn, request: Request):
    if request.state.user.get("role") not in ("recruiter", "ta_manager", "admin"):
        return JSONResponse(status_code=403, content={"detail": "Not authorised to schedule interviews"})
    app_row = query_one(
        """SELECT a.id, c.email, c.full_name, r.title AS job_title, r.tenant_id
           FROM application a
           JOIN candidate c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE a.id = %s""",
        [payload.application_id],
    )
    if not app_row:
        raise HTTPException(404, "application not found")
    start = datetime.utcnow() + timedelta(hours=payload.start_in_hours)
    organizer_email = request.state.user.get("email") or ""

    from .services.email_validation import assert_real_email
    valid_panel_emails = []
    for _pe in payload.panel_emails:
        try:
            valid_panel_emails.append(assert_real_email(_pe))
        except ValueError as exc:
            print(f"[schedule] Dropping invalid panel email: {exc}")
    payload.panel_emails = valid_panel_emails

    meeting = connectors.schedule_meeting(
        organizer_email=organizer_email,
        candidate_email=app_row["email"],
        panel_emails=payload.panel_emails,
        start_time=start,
        duration_min=payload.duration_min,
        meet_link=payload.meet_link,
        candidate_name=app_row.get("full_name") or "Candidate",
        job_title=app_row.get("job_title") or "the role",
        tenant_id=app_row.get("tenant_id"),
    )
    rc = query_one(
        """SELECT id FROM round_config
           WHERE requisition_id = (SELECT requisition_id FROM application WHERE id=%s)
           ORDER BY sequence LIMIT 1""",
        [payload.application_id],
    )
    iv = query_one(
        """INSERT INTO interview
             (application_id, round_config_id, scheduled_at, meet_link, gcal_event_id, mode)
           VALUES (%s, %s, %s, %s, %s, 'virtual')
           RETURNING id""",
        [payload.application_id, rc["id"] if rc else None, start,
         meeting["meet_link"], meeting["gcal_event_id"]],
    )
    # Populate interview_panel from panel_emails (look up app_user by email)
    if iv and payload.panel_emails:
        for email in payload.panel_emails:
            pu = query_one(
                "SELECT id FROM app_user WHERE LOWER(email) = LOWER(%s) AND is_active = TRUE",
                [email],
            )
            if pu:
                query(
                    """INSERT INTO interview_panel (interview_id, interviewer_id)
                       VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                    [str(iv["id"]), str(pu["id"])],
                    fetch=False,
                )
    confirmation_email_sent = False
    try:
        import html as _html
        from .services.email_templates import render_template as _render_sched_tmpl
        from .services.email_layout import build_branded_email
        _interview_time = connectors.to_ist(start).strftime("%A, %d %B %Y at %I:%M %p IST")
        _cand_name = app_row.get("full_name") or "Candidate"
        _job_title = app_row.get("job_title") or "the position"
        _et_subj, _et_body = _render_sched_tmpl("interview_scheduled", {
            "candidate_name": _cand_name,
            "job_title":      _job_title,
            "interview_time": _interview_time,
            "meet_link":      meeting["meet_link"],
        })
        _et_html = build_branded_email(
            eyebrow="Application Tracking System",
            hero_title_html="Your Interview<br>is Scheduled.",
            hero_subtitle=f"Hi {_html.escape(_cand_name)}, here are the details for your upcoming interview.",
            hero_footer_label=_job_title,
            detail_cells=[
                ("Candidate", _cand_name), ("Position", _job_title),
                ("Date & Time", _interview_time), ("Mode", "Virtual"),
            ],
            about_text=_et_body,
            about_heading=None,
            cta_label="Join Meeting" if meeting.get("meet_link") else None,
            cta_link=meeting.get("meet_link") or None,
        )
        connectors.send_email(app_row["email"], _et_subj, _et_body, html=_et_html, tenant_id=app_row.get("tenant_id"))
        confirmation_email_sent = True
    except Exception as _sched_email_exc:
        print(f"[schedule] Email send failed: {_sched_email_exc}")
    # meeting already carries the real invite_sent/invite_stub/invite_missing
    # signal from connectors.schedule_meeting() -- never collapse it to a bare
    # success here. The interview/meeting record above is kept either way.
    return {**meeting, "confirmation_email_sent": confirmation_email_sent}


# ---------------- reports ----------------
@app.get("/api/reports/{view_name}")
def report(view_name: str, request: Request):
    assert_staff(request.state.user)
    if request.state.user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    allowed = {
        "tat": "v_req_time_to_fill",
        "recruiter-load": "v_recruiter_load",
        "gender": "v_gender_split",
        "positions": "v_positions_by_fy",
        "budget": "v_budget_vs_offered",
        "bu": "v_bu_summary",
        "roll": "v_roll_split",
    }
    if view_name not in allowed:
        raise HTTPException(404, f"unknown report. choose: {list(allowed)}")
    return query(f"SELECT * FROM {allowed[view_name]}")


# ---------------- admin system endpoints ----------------
@app.get("/api/admin/db-stats")
def db_stats(request: Request):
    if request.state.user.get("role") != "admin":
        return JSONResponse(status_code=403, content={"detail": "Admin only"})
    tables = [
        "app_user", "requisition", "application", "candidate",
        "interview", "scorecard", "offer", "stage_event", "enteri_ai_session",
    ]
    result = {}
    for t in tables:
        row = query_one(f"SELECT COUNT(*) AS n FROM {t}")
        result[t] = int(row["n"]) if row else 0
    return result


@app.get("/api/admin/cv-database")
def cv_database(request: Request):
    """CV / candidate database — full candidate list with application data."""
    if request.state.user.get("role") != "admin":
        return JSONResponse(status_code=403, content={"detail": "Admin only"})

    summary = query_one(
        """
        SELECT
          COUNT(DISTINCT LOWER(c.email))                                    AS total_candidates,
          COUNT(a.id)                                                       AS total_applications,
          COUNT(DISTINCT LOWER(c.email)) FILTER
            (WHERE c.resume_url IS NOT NULL AND c.resume_url <> '')         AS resumes_on_file,
          ROUND(AVG(a.combined_score)
            FILTER (WHERE a.combined_score IS NOT NULL)::numeric, 1)        AS avg_score,
          COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE a.status = 'hired') AS total_joined
        FROM candidate c
        LEFT JOIN application a ON a.candidate_id = c.id
        """,
    )

    candidates = query(
        """
        SELECT * FROM (
          SELECT DISTINCT ON (LOWER(c.email))
            c.id, c.full_name, c.email, c.gender, c.source,
            c.resume_url,
            c.created_at                                                     AS registered_at,
            (SELECT COUNT(DISTINCT a_cnt.requisition_id)
             FROM application a_cnt
             JOIN candidate c_dup ON c_dup.id = a_cnt.candidate_id
             WHERE LOWER(c_dup.email) = LOWER(c.email))                     AS total_applications,
            (SELECT r.title
             FROM application a2
             JOIN candidate c2d ON c2d.id = a2.candidate_id
             JOIN requisition r ON r.id = a2.requisition_id
             WHERE LOWER(c2d.email) = LOWER(c.email)
             ORDER BY a2.applied_at DESC LIMIT 1)                           AS latest_position,
            (SELECT a3.status
             FROM application a3
             JOIN candidate c3d ON c3d.id = a3.candidate_id
             WHERE LOWER(c3d.email) = LOWER(c.email)
             ORDER BY a3.applied_at DESC LIMIT 1)                           AS latest_status,
            (SELECT a4.combined_score
             FROM application a4
             JOIN candidate c4d ON c4d.id = a4.candidate_id
             WHERE LOWER(c4d.email) = LOWER(c.email)
             ORDER BY a4.combined_score DESC NULLS LAST LIMIT 1)            AS best_score,
            (SELECT a5.bot_score
             FROM application a5
             JOIN candidate c5d ON c5d.id = a5.candidate_id
             WHERE LOWER(c5d.email) = LOWER(c.email)
             ORDER BY a5.bot_score DESC NULLS LAST LIMIT 1)                 AS ai_score,
            (SELECT a6.match_score
             FROM application a6
             JOIN candidate c6d ON c6d.id = a6.candidate_id
             WHERE LOWER(c6d.email) = LOWER(c.email)
             ORDER BY a6.match_score DESC NULLS LAST LIMIT 1)               AS match_score
          FROM candidate c
          ORDER BY LOWER(c.email), c.created_at ASC
        ) deduped
        ORDER BY registered_at DESC
        """,
    )

    return {"summary": dict(summary) if summary else {}, "candidates": candidates}


@app.get("/api/admin/sys-logs")
def sys_logs(request: Request, limit: int = 100):
    if request.state.user.get("role") != "admin":
        return JSONResponse(status_code=403, content={"detail": "Admin only"})
    return query(
        """SELECT se.id, se.from_status, se.to_status,
                  COALESCE(u.full_name, 'system') AS actor,
                  se.note, se.occurred_at
           FROM stage_event se
           LEFT JOIN app_user u ON u.id = se.actor_id
           ORDER BY se.occurred_at DESC
           LIMIT %s""",
        [min(limit, 500)],
    )


# ---------------- frontend ----------------
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

if os.path.isdir(_FRONTEND_DIR):
    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        with open(os.path.join(_FRONTEND_DIR, "login.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/enteri-ai-interview", response_class=HTMLResponse)
    def enteri_ai_interview_page():
        """Public candidate-facing AI interview page — accessed via invite token."""
        with open(os.path.join(_FRONTEND_DIR, "interview.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/interview-schedule", response_class=HTMLResponse)
    def interview_schedule_page():
        """Public candidate-facing slot picker — accessed via a scheduling token."""
        with open(os.path.join(_FRONTEND_DIR, "schedule.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/reschedule", response_class=HTMLResponse)
    def reschedule_page():
        """Public self-service reschedule page (candidate or panelist) — accessed via reschedule_token."""
        with open(os.path.join(_FRONTEND_DIR, "reschedule.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/set-password", response_class=HTMLResponse)
    def set_password_page():
        with open(os.path.join(_FRONTEND_DIR, "set-password.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/platform-login", response_class=HTMLResponse)
    def platform_login_page():
        with open(os.path.join(_FRONTEND_DIR, "platform-login.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/platform-admin", response_class=HTMLResponse)
    def platform_admin_page():
        with open(os.path.join(_FRONTEND_DIR, "platform-admin.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/candidate-portal", response_class=HTMLResponse)
    def candidate_portal_page():
        """Candidate-facing login + application status page."""
        with open(os.path.join(_FRONTEND_DIR, "candidate-portal.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/vendor-portal", response_class=HTMLResponse)
    def vendor_portal_page():
        """Vendor-facing login + open requisitions / submissions page."""
        with open(os.path.join(_FRONTEND_DIR, "vendor-portal.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(os.path.join(_FRONTEND_DIR, "index.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)
