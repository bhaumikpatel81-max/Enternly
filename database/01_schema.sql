-- ============================================================
-- ONE CLICK HIRE  -  Core schema (Phase 1)
-- Target: PostgreSQL 14+
--
-- Design principle: EVERYTHING IS CONFIG.
-- Bands, business units, group companies, approval chains,
-- email templates, feedback forms and interview rounds are all
-- stored as editable DATA, not hard-coded. When EnternsTech changes
-- its band structure or an approval flow, you add/edit rows --
-- no code change, no redeploy.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- for gen_random_uuid()

-- ------------------------------------------------------------
-- ORGANISATION STRUCTURE  (all config)
-- ------------------------------------------------------------

-- Group companies: EnternsTech (parent), Maxsapient, Andpayments, etc.
CREATE TABLE group_company (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    domain      TEXT,                       -- e.g. "Aviation", "IOT"
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Business units: corporate, traffic, mobility, etc.
-- A BU belongs to a group company.
CREATE TABLE business_unit (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  UUID NOT NULL REFERENCES group_company(id),
    name        TEXT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, name)
);

-- Bands: 5, 4C, 4B ... 1A.
-- `rank` gives a numeric order (lower number = more junior) so the
-- system can reason about seniority without parsing the code string.
-- `is_active` lets you retire a band when the structure changes
-- WITHOUT deleting history tied to it.
CREATE TABLE band (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code        TEXT NOT NULL UNIQUE,       -- "4C", "3A", ...
    rank        INT  NOT NULL,              -- 1 = lowest (band 5) ... 13 = 1A
    description TEXT,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- PEOPLE  (recruiters, hiring managers, approvers)
-- ------------------------------------------------------------
CREATE TABLE app_user (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name   TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL DEFAULT 'recruiter'
                CHECK (role IN ('admin','ta_manager','recruiter',
                                'hiring_manager','bu_head','director','interviewer')),
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- REQUISITIONS  (an open position)
-- ------------------------------------------------------------
CREATE TABLE requisition (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_ref        TEXT,
    req_code            TEXT UNIQUE,                    -- auto-generated REQ-0001
    title               TEXT NOT NULL,
    bu_id               UUID NOT NULL REFERENCES business_unit(id),
    band_id             UUID NOT NULL REFERENCES band(id),
    roll_type           TEXT NOT NULL DEFAULT 'on_roll'
                        CHECK (roll_type IN ('on_roll','off_roll')),
    job_description     TEXT,
    key_skills          TEXT[],
    min_experience      NUMERIC,
    max_experience      NUMERIC,
    budgeted_ctc        NUMERIC,
    budgeted_fixed      NUMERIC,
    budgeted_variable   NUMERIC,
    openings            INT NOT NULL DEFAULT 1,
    fiscal_year         TEXT,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','open','on_hold','closed','cancelled')),
    approval_status     TEXT DEFAULT 'approved'
                        CHECK (approval_status IN ('approved','pending_ta_approval','rejected')),
    created_by_role     TEXT,
    hiring_manager_id   UUID REFERENCES app_user(id),
    created_by          UUID REFERENCES app_user(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    opened_at           TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    -- extended fields
    is_p1               BOOLEAN NOT NULL DEFAULT FALSE,
    risk                TEXT CHECK (risk IN ('low','medium','high','critical')),
    hiring_location     TEXT,
    project             TEXT,
    grade_level         TEXT,
    priority            TEXT CHECK (priority IN ('critical','high','medium','low')),
    source_channels     TEXT[],
    is_fresher_role     BOOLEAN DEFAULT FALSE,
    resume_weight       NUMERIC(4,2) DEFAULT 0.40,
    interview_weight    NUMERIC(4,2) DEFAULT 0.60,
    criticality         TEXT NOT NULL DEFAULT 'Medium',
    screening_questions TEXT[] DEFAULT '{}',            -- recruiter-set questions for Enteri AI
    is_internal_movement BOOLEAN NOT NULL DEFAULT FALSE
);

-- Many recruiters can share a requisition. A TA manager can assign
-- recruiters, or a recruiter can self-assign. `is_owner` marks the
-- primary owner.
CREATE TABLE requisition_recruiter (
    requisition_id  UUID NOT NULL REFERENCES requisition(id) ON DELETE CASCADE,
    recruiter_id    UUID NOT NULL REFERENCES app_user(id),
    is_owner        BOOLEAN NOT NULL DEFAULT FALSE,
    assigned_by     UUID REFERENCES app_user(id),
    assigned_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (requisition_id, recruiter_id)
);

-- ------------------------------------------------------------
-- INTERVIEW ROUND CONFIG  (per requisition, fully customizable)
-- A senior req can have 4 rounds; a fresher req can have an
-- assessment instead of a bot interview. round_type is open text
-- so you can add new types (bot_interview, assessment, technical,
-- hr, managerial...) without schema changes.
-- ------------------------------------------------------------
CREATE TABLE round_config (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requisition_id  UUID NOT NULL REFERENCES requisition(id) ON DELETE CASCADE,
    sequence        INT NOT NULL,           -- 1, 2, 3 ... order of rounds
    name            TEXT NOT NULL,          -- "AI screening", "Tech round 1"
    round_type      TEXT NOT NULL,          -- bot_interview | assessment | panel | hr
    is_auto         BOOLEAN NOT NULL DEFAULT FALSE,  -- bot/assessment = auto
    feedback_form_id UUID,                  -- which form interviewers fill (FK below)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (requisition_id, sequence)
);

-- ------------------------------------------------------------
-- CANDIDATES + APPLICATIONS
-- A candidate is a person; an application is that person against
-- one requisition. The same person can apply to several reqs.
-- ------------------------------------------------------------
CREATE TABLE candidate (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name   TEXT NOT NULL,
    email       TEXT NOT NULL,
    phone       TEXT,
    gender      TEXT CHECK (gender IN ('male','female','undisclosed')),
    -- gender is the only diversity axis EnternsTech tracks today;
    -- 'undisclosed' keeps reporting honest when not provided.
    resume_url  TEXT,                       -- GCP Cloud Storage object path
    source      TEXT,                       -- naukri | linkedin | referral | career_site
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE application (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requisition_id  UUID NOT NULL REFERENCES requisition(id),
    candidate_id    UUID NOT NULL REFERENCES candidate(id),
    -- Automated screening output:
    match_score     NUMERIC,                -- 0-100 JD match from screening engine
    score_breakdown JSONB,                  -- {"skills":8,"experience":true,...}
    -- Combined chart score = screening + bot interview (computed later)
    bot_score       NUMERIC,                -- AI bot / assessment result, 0-100
    combined_score  NUMERIC,                -- weighted blend for the "top chart"
    current_round   INT NOT NULL DEFAULT 0, -- which round_config.sequence they're in
    status          TEXT NOT NULL DEFAULT 'applied'
                    CHECK (status IN ('applied','screening','screen_passed',
                                      'screen_rejected','interviewing','selected',
                                      'rejected','offer_stage','offered','joined','dropped')),
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (requisition_id, candidate_id)   -- one application per person per req
);

-- ------------------------------------------------------------
-- INTERVIEWS + SCORECARDS
-- ------------------------------------------------------------
CREATE TABLE interview (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    round_config_id UUID NOT NULL REFERENCES round_config(id),
    scheduled_at    TIMESTAMPTZ,
    duration_min    INT DEFAULT 45,
    mode            TEXT DEFAULT 'virtual'
                    CHECK (mode IN ('virtual','in_person','telephonic','bot')),
    meet_link       TEXT,                   -- Google Meet URL (auto-generated)
    gcal_event_id   TEXT,                   -- Google Calendar event id
    status          TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled','completed','no_show','cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Which panel members are on an interview (many-to-many)
CREATE TABLE interview_panel (
    interview_id    UUID NOT NULL REFERENCES interview(id) ON DELETE CASCADE,
    interviewer_id  UUID NOT NULL REFERENCES app_user(id),
    PRIMARY KEY (interview_id, interviewer_id)
);

-- Feedback form DEFINITIONS are config: a recruiter can build/edit
-- a form at any time. The fields live in JSONB so the form can be
-- any shape (ratings, yes/no, free text) without schema changes.
CREATE TABLE feedback_form (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    schema      JSONB NOT NULL,             -- [{"key":"tech","label":"Technical","type":"rating_5"},...]
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_by  UUID REFERENCES app_user(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A filled-in scorecard for one interview by one interviewer.
-- form_data answers the feedback_form.schema. verdict drives the gate.
CREATE TABLE scorecard (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interview_id    UUID NOT NULL REFERENCES interview(id) ON DELETE CASCADE,
    interviewer_id  UUID NOT NULL REFERENCES app_user(id),
    feedback_form_id UUID REFERENCES feedback_form(id),
    form_data       JSONB,                  -- the actual answers
    overall_score   NUMERIC,
    verdict         TEXT CHECK (verdict IN ('strong_yes','yes','no','strong_no')),
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (interview_id, interviewer_id)
);

-- ------------------------------------------------------------
-- APPROVAL CHAINS  (per band, fully customizable)
-- Each band routes offer sign-off differently. approver_steps is
-- an ordered JSON list so you/recruiters define WHO signs off and
-- in what order, editable any time.
-- e.g. [{"step":1,"role":"bu_head"},{"step":2,"role":"director"}]
-- ------------------------------------------------------------
CREATE TABLE approval_chain (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    band_id         UUID REFERENCES band(id),  -- null = default chain
    name            TEXT NOT NULL,
    approver_steps  JSONB NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- OFFERS
-- ------------------------------------------------------------
CREATE TABLE offer (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  UUID NOT NULL REFERENCES application(id),
    fixed_ctc       NUMERIC,
    variable_ctc    NUMERIC,
    total_ctc       NUMERIC,
    approval_chain_id UUID REFERENCES approval_chain(id),
    approval_state  JSONB,                  -- tracks which steps approved
    status          TEXT NOT NULL DEFAULT 'pending_approval'
                    CHECK (status IN ('pending_approval','approved','rejected',
                                      'released','accepted','declined')),
    darwin_pushed   BOOLEAN NOT NULL DEFAULT FALSE,  -- integration flag (Phase 6)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- EMAIL TEMPLATES  (config: customizable by any recruiter)
-- ------------------------------------------------------------
CREATE TABLE email_template (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,              -- "Interview invite", "Schedule to panel"
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,              -- supports {{candidate_name}} etc. tokens
    category    TEXT,                       -- candidate | panel | offer
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_by  UUID REFERENCES app_user(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- STAGE EVENT LOG  (THE engine of all TAT reporting)
-- Every time an application changes stage, we log it with a
-- timestamp. TAT for any stage = difference between consecutive
-- events. This single table powers every TAT report you listed.
-- ------------------------------------------------------------
CREATE TABLE stage_event (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    from_status     TEXT,
    to_status       TEXT NOT NULL,
    actor_id        UUID REFERENCES app_user(id),  -- who moved it (null = system/auto)
    note            TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Helpful indexes for the reporting queries
CREATE INDEX idx_application_req     ON application(requisition_id);
CREATE INDEX idx_application_status  ON application(status);
CREATE INDEX idx_stage_event_app     ON stage_event(application_id);
CREATE INDEX idx_stage_event_time    ON stage_event(occurred_at);
CREATE INDEX idx_requisition_bu      ON requisition(bu_id);
CREATE INDEX idx_requisition_status  ON requisition(status);
CREATE INDEX idx_requisition_fy      ON requisition(fiscal_year);
