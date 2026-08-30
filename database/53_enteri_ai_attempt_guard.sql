-- Migration 53: Enteri AI attempt-lifecycle guard on the invite token (2026-07)
-- Doc-only snapshot — the executable migration lives in backend/app/main.py's
-- _auto_migrate() list. See that file for the statements that actually run.

ALTER TABLE enteri_ai_invite
    ADD COLUMN IF NOT EXISTS attempt_status TEXT NOT NULL DEFAULT 'unused',
    -- unused | in_progress | completed | revoked
    ADD COLUMN IF NOT EXISTS attempt_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS attempt_completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_by_token TEXT;

CREATE INDEX IF NOT EXISTS idx_enteri_ai_invite_attempt_status
    ON enteri_ai_invite (attempt_status);
