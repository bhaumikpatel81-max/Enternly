-- Migration 19: Add email_sent guard to enteri_ai_session to prevent duplicate emails
ALTER TABLE enteri_ai_session
  ADD COLUMN IF NOT EXISTS email_sent BOOLEAN NOT NULL DEFAULT FALSE;
