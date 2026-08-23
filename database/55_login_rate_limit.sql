-- Migration 55: login rate-limiting ledger (2026-07)
-- Doc-only snapshot -- the executable migration lives in backend/app/main.py's
-- _auto_migrate() list. See that file for the statements that actually run.
--
-- Every login attempt (success or failure) is logged here so auth.py can
-- rate-limit brute-force/spray attempts by (ip+email) and by ip alone.

CREATE TABLE IF NOT EXISTS login_attempt (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT,
    ip_address  TEXT,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    success     BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_login_attempt_ip_time    ON login_attempt (ip_address, attempted_at);
CREATE INDEX IF NOT EXISTS idx_login_attempt_email_time ON login_attempt (email, attempted_at);
