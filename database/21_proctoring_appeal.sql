-- Migration 21: Proctoring appeal workflow
-- Stores candidate appeals against auto-terminated NexAI sessions.
-- One appeal per session (UNIQUE on nexai_session_id).

CREATE TABLE IF NOT EXISTS proctoring_appeal (
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
);

CREATE INDEX IF NOT EXISTS idx_proctoring_appeal_application
    ON proctoring_appeal (application_id);
CREATE INDEX IF NOT EXISTS idx_proctoring_appeal_status
    ON proctoring_appeal (status);
