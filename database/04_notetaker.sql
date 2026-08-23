-- ============================================================
-- ENTERNLY  -  Meeting notetaker tables (Phase add-on)
-- Stores consent, recordings, transcripts and AI-generated notes
-- for every interview round, including the AI bot round.
-- Run AFTER 01_schema.sql.
--
-- LEGAL NOTE: recording only proceeds after consent is captured.
-- The consent row is written BEFORE any recording starts.
-- ============================================================

-- Candidate consent to be recorded, captured per interview.
-- No recording may start without a matching 'granted' row.
CREATE TABLE recording_consent (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interview_id    UUID NOT NULL REFERENCES interview(id) ON DELETE CASCADE,
    candidate_id    UUID NOT NULL REFERENCES candidate(id),
    consent_state   TEXT NOT NULL DEFAULT 'pending'
                    CHECK (consent_state IN ('pending','granted','declined','withdrawn')),
    consent_text    TEXT,                   -- exact notice the candidate saw
    region          TEXT,                   -- for region-specific rules (DPDP, GDPR)
    responded_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (interview_id)
);

-- One recording record per interview. provider tells you whether the
-- native pipeline or a third-party (Fireflies/Read.ai) produced it,
-- so the fallback is auditable.
CREATE TABLE meeting_recording (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interview_id    UUID NOT NULL REFERENCES interview(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL DEFAULT 'native'
                    CHECK (provider IN ('native','fireflies','readai','otter')),
    video_url       TEXT,                   -- GCP Cloud Storage object path
    audio_url       TEXT,
    duration_sec    INT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','recording','processing','ready','failed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (interview_id, provider)
);

-- Transcript text for a recording (kept separate so large text doesn't
-- bloat the recording row).
CREATE TABLE meeting_transcript (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recording_id    UUID NOT NULL REFERENCES meeting_recording(id) ON DELETE CASCADE,
    full_text       TEXT,
    segments        JSONB,                  -- [{"speaker":"panel","ts":0,"text":"..."}]
    language        TEXT DEFAULT 'en',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- AI-generated notes/summary from a transcript: summary, action items,
-- key Q&A, and a suggested score the panel can accept or override.
CREATE TABLE meeting_notes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recording_id    UUID NOT NULL REFERENCES meeting_recording(id) ON DELETE CASCADE,
    summary         TEXT,
    key_points      JSONB,                  -- ["...", "..."]
    action_items    JSONB,
    suggested_score NUMERIC,                -- assistive only; panel decides
    shared_with     JSONB,                  -- emails the notes were sent to
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_recording_interview ON meeting_recording(interview_id);
CREATE INDEX idx_consent_interview   ON recording_consent(interview_id);
