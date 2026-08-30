-- ============================================================
-- Auth migration: add password-based login support
-- Run AFTER 01_schema.sql and 02_seed.sql
-- pgcrypto is already enabled by 01_schema.sql
-- ============================================================

ALTER TABLE app_user
  ADD COLUMN IF NOT EXISTS password_hash        TEXT,
  ADD COLUMN IF NOT EXISTS reset_token          TEXT,
  ADD COLUMN IF NOT EXISTS reset_token_expires  TIMESTAMPTZ;

-- No default password is set. All users (including the TA Admin account)
-- must use "Forgot password" to set their password on first login.
-- The forgot-password flow sends a one-time link via the configured SMTP account.
