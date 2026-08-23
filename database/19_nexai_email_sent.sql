-- Migration 19: Add email_sent guard to nexai_session to prevent duplicate emails
ALTER TABLE nexai_session
  ADD COLUMN IF NOT EXISTS email_sent BOOLEAN NOT NULL DEFAULT FALSE;
