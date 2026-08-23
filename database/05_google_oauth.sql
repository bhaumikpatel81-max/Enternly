-- ============================================================
-- ENTERNLY  -  Recruiter Google OAuth tokens (Phase 3)
-- Stores each recruiter's personal OAuth tokens so interviews
-- auto-schedule on THEIR Google Calendar with a Meet link.
-- Run AFTER 01_schema.sql.
-- ============================================================

CREATE TABLE recruiter_google_token (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    google_email  TEXT        NOT NULL,           -- email returned by Google userinfo
    access_token  TEXT        NOT NULL,
    refresh_token TEXT,                           -- NULL until first offline grant
    token_expiry  TIMESTAMPTZ,
    scope         TEXT,                           -- space-separated scope list
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id)                              -- one linked account per recruiter
);

CREATE INDEX idx_google_token_user ON recruiter_google_token(user_id);
