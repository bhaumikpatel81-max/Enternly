-- ============================================================
-- Migration 34: Recruiter screening questions on requisition
-- Safe to re-run (ADD COLUMN IF NOT EXISTS).
-- screening_questions stores free-text questions the recruiter
-- wants Enteri AI to ask in addition to its auto-generated ones.
-- ============================================================

ALTER TABLE requisition
  ADD COLUMN IF NOT EXISTS screening_questions TEXT[] DEFAULT '{}';
