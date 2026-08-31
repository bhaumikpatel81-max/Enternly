-- platform_settings (2026-08). Doc-only snapshot of "Migration 104" in
-- main.py's _auto_migrate(), which is what actually runs on every boot.
--
-- A small KV table for platform-console-configurable defaults (default
-- new-tenant plan, default enabled modules) -- deliberately separate from
-- system_status, which is reserved for background-worker heartbeats/
-- kill-switches, not admin-configurable settings.

CREATE TABLE IF NOT EXISTS platform_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
