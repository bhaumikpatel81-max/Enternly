-- Migration 54: Enternly Calendly-style HM self-scheduling (2026-07)
-- Doc-only snapshot -- the executable migration lives in backend/app/main.py's
-- _auto_migrate() list. See that file for the statements that actually run.
--
-- Flow: recruiter (or an auto-triggered "Panel + Auto" round) opens a
-- scheduling request -> HM proposes 3-6 slots -> candidate confirms one via
-- a public token link (same pattern as nexai_invite) -> both get an ICS
-- invite over SMTP with the candidate's CV attached.

CREATE TABLE IF NOT EXISTS interview_schedule_request (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id    UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    round_config_id   UUID REFERENCES round_config(id),
    hm_user_id        UUID REFERENCES app_user(id),
    status            TEXT NOT NULL DEFAULT 'awaiting_hm'
                      CHECK (status IN ('awaiting_hm','awaiting_candidate',
                                         'confirmed','cancelled','expired')),
    candidate_token   TEXT UNIQUE,            -- public link token (like nexai_invite.token)
    duration_min      INTEGER NOT NULL DEFAULT 45,
    meeting_link      TEXT,                   -- HM-provided URL, optional
    confirmed_slot_id UUID,                   -- FK added below (interview_slot doesn't exist yet)
    created_by        UUID REFERENCES app_user(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    hm_submitted_at    TIMESTAMPTZ,
    confirmed_at       TIMESTAMPTZ
);

-- The 3-6 candidate-visible slots the HM proposed on the 2-month grid.
CREATE TABLE IF NOT EXISTS interview_slot (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id   UUID NOT NULL REFERENCES interview_schedule_request(id) ON DELETE CASCADE,
    start_utc    TIMESTAMPTZ NOT NULL,        -- stored UTC, rendered in IST client-side
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','taken','released')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_isr_application ON interview_schedule_request(application_id);
CREATE INDEX IF NOT EXISTS idx_isr_token       ON interview_schedule_request(candidate_token);
CREATE INDEX IF NOT EXISTS idx_isr_hm_status   ON interview_schedule_request(hm_user_id, status);
CREATE INDEX IF NOT EXISTS idx_islot_request   ON interview_slot(request_id);

ALTER TABLE interview_schedule_request
    ADD CONSTRAINT fk_isr_confirmed_slot
    FOREIGN KEY (confirmed_slot_id) REFERENCES interview_slot(id);
