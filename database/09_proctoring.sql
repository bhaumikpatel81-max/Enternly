-- Migration 09: Proctoring tables
-- Run AFTER 01-08 migrations.
-- LEGAL NOTE: All columns here store regulated personal/biometric data.
-- Retention is set per-session by the legal team via retention_until.

CREATE TABLE IF NOT EXISTS proctoring_session (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    enteri_ai_session_id      UUID REFERENCES enteri_ai_session(id) ON DELETE SET NULL,
    application_id            UUID NOT NULL REFERENCES application(id),

    -- B1: Consent
    consent_granted           BOOLEAN NOT NULL DEFAULT FALSE,
    consent_text              TEXT,
    consented_at              TIMESTAMPTZ,
    proctoring_declined       BOOLEAN NOT NULL DEFAULT FALSE,

    -- B2: Identity capture
    identity_snapshot_path    TEXT,   -- GCP path or local path (dev)
    -- identity_match is a specialist paid service (government-ID matching).
    -- Scaffold: status field only. Do NOT implement natively.
    -- See AVATAR_PROCTORING_SPEC.md "Do NOT attempt natively" section.
    identity_match_status     TEXT NOT NULL DEFAULT 'not_attempted'
                              CHECK (identity_match_status IN
                                     ('not_attempted','pending','matched','mismatch','vendor_error')),

    -- B3/B4/B5: Media storage (GCP paths)
    webcam_video_path         TEXT,
    screen_video_path         TEXT,
    screen_recording_declined BOOLEAN NOT NULL DEFAULT FALSE,

    -- B6: AI behaviour flags [{ts_ms, type, confidence, detail}]
    flags                     JSONB NOT NULL DEFAULT '[]'::jsonb,
    flag_count                INT   NOT NULL DEFAULT 0,

    -- B7: Human review
    reviewer_notes            TEXT,
    reviewed_by               UUID REFERENCES app_user(id),
    reviewed_at               TIMESTAMPTZ,
    human_decision            TEXT CHECK (human_decision IN
                                          ('cleared','flagged_minor','flagged_major','voided')),

    -- Storage / retention
    retention_until           TIMESTAMPTZ,   -- Legal sets this value
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_proc_enteri_ai ON proctoring_session(enteri_ai_session_id);
CREATE INDEX IF NOT EXISTS idx_proc_app     ON proctoring_session(application_id);
CREATE INDEX IF NOT EXISTS idx_proc_review  ON proctoring_session(reviewed_at)
  WHERE human_decision IS NULL;
