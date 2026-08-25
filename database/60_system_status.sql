-- Global system_status table (2026-08). Doc-only snapshot of "Migration 97"
-- in main.py's _auto_migrate(), which is what actually runs on every boot.
--
-- Migration 96 widened system_settings' primary key to (tenant_id, key) for
-- real per-customer settings (SMTP, company name, ...). Several background
-- services (email/CV ingest pollers, heartbeats, kill-switches) were reusing
-- that same table as a generic global key/value store for things that have
-- nothing to do with any one tenant -- this table is what they use instead.

CREATE TABLE IF NOT EXISTS system_status (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO system_status (key, value, updated_at)
SELECT key, value, updated_at FROM system_settings
WHERE key IN ('email_ingest_status', 'cv_scan_paused',
              'cv_enricher_heartbeat', 'recruiter_email_scan_status',
              'activity_log_last_failure', 'bg_lock_last_error')
   OR key LIKE 'bg_task_status:%'
ON CONFLICT (key) DO NOTHING;
