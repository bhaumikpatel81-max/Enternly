-- Migration: NexAI candidate interview invite tokens
-- Candidates receive a time-limited link; recruiter never sees the interview UI.

CREATE TABLE IF NOT EXISTS nexai_invite (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    token          TEXT NOT NULL UNIQUE,
    invited_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '7 days',
    used_at        TIMESTAMPTZ,          -- set when candidate starts the session
    created_by     UUID REFERENCES app_user(id)
);

CREATE INDEX IF NOT EXISTS idx_nexai_invite_token ON nexai_invite (token);
