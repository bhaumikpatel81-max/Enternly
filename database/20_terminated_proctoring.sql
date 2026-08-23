-- Migration 20: Support terminated_proctoring session status
-- Allows the proctoring strike system to mark sessions that were auto-terminated
-- separately from normally completed ones, so recruiters can distinguish them.

-- Drop the existing inline CHECK (PostgreSQL auto-names it nexai_session_status_check)
-- and recreate with the additional status value.
ALTER TABLE nexai_session
  DROP CONSTRAINT IF EXISTS nexai_session_status_check;

ALTER TABLE nexai_session
  ADD CONSTRAINT nexai_session_status_check
  CHECK (status IN ('pending','in_progress','completed','failed','terminated_proctoring'));

-- Store the human-readable reason for termination (e.g. "3 strikes: phone detected")
ALTER TABLE nexai_session
  ADD COLUMN IF NOT EXISTS termination_reason TEXT;
