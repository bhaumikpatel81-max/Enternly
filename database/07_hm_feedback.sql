-- Migration 07: hiring-manager feedback columns on application
-- Run AFTER 01-06 migrations.
ALTER TABLE application
  ADD COLUMN IF NOT EXISTS hm_feedback    TEXT,
  ADD COLUMN IF NOT EXISTS hm_reviewed_at TIMESTAMPTZ;
