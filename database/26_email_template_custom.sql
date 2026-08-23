-- ============================================================
-- Migration 26: Custom email templates + manual send log
-- Adds template_key, valid_placeholders, updated_by, is_builtin
-- to email_template. Creates sent_email_log.
-- Idempotent: safe to re-run.
-- ============================================================

ALTER TABLE email_template ADD COLUMN IF NOT EXISTS template_key       TEXT UNIQUE;
ALTER TABLE email_template ADD COLUMN IF NOT EXISTS valid_placeholders JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE email_template ADD COLUMN IF NOT EXISTS updated_by         UUID  REFERENCES app_user(id);
ALTER TABLE email_template ADD COLUMN IF NOT EXISTS is_builtin         BOOLEAN NOT NULL DEFAULT FALSE;

-- Audit log for every manual "Send Email" action from the candidates list
CREATE TABLE IF NOT EXISTS sent_email_log (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID        REFERENCES application(id),
    template_key   TEXT,
    template_name  TEXT,
    sent_to_email  TEXT        NOT NULL,
    sent_by        UUID        REFERENCES app_user(id),
    sent_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    subject        TEXT,
    notes          TEXT
);
