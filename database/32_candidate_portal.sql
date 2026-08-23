-- ============================================================
-- Migration 32: Candidate Portal
-- ============================================================
-- candidate_user  — portal login accounts linked to candidate rows
-- candidate_feedback — company / interview experience ratings
--
-- account_type='candidate' in password_reset_token was introduced
-- in 31_vendor_management.sql — do NOT re-add it here.
-- ============================================================

-- 1. Candidate portal login accounts ----------------------------
CREATE TABLE IF NOT EXISTS candidate_user (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id   UUID NOT NULL UNIQUE REFERENCES candidate(id) ON DELETE CASCADE,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cu_candidate ON candidate_user(candidate_id);
CREATE INDEX IF NOT EXISTS idx_cu_email     ON candidate_user(LOWER(email));

-- 2. Candidate experience feedback ------------------------------
CREATE TABLE IF NOT EXISTS candidate_feedback (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id     UUID NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
    application_id   UUID REFERENCES application(id) ON DELETE SET NULL,
    company_rating   SMALLINT NOT NULL CHECK (company_rating  BETWEEN 1 AND 5),
    interview_rating SMALLINT NOT NULL CHECK (interview_rating BETWEEN 1 AND 5),
    comments         TEXT,
    visible_to_ta    BOOLEAN NOT NULL DEFAULT TRUE,
    submitted_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cfb_candidate   ON candidate_feedback(candidate_id);
CREATE INDEX IF NOT EXISTS idx_cfb_application ON candidate_feedback(application_id);
