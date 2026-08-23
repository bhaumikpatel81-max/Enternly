-- Migration 18: Add proctoring_complete flag missing from 09_proctoring.sql
-- The complete_session endpoint in proctoring_api.py references this column.
-- Run after 09_proctoring.sql.
ALTER TABLE proctoring_session
  ADD COLUMN IF NOT EXISTS proctoring_complete BOOLEAN NOT NULL DEFAULT FALSE;
