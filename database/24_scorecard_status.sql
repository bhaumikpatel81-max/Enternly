-- Migration 24: Scorecard draft/submit workflow
-- Adds status (draft|submitted) and created_at columns to scorecard.
-- Makes submitted_at nullable so drafts don't need a timestamp.
-- Marks all pre-existing rows as submitted (they had submitted_at set by the old NOT NULL DEFAULT).

ALTER TABLE scorecard ALTER COLUMN submitted_at DROP NOT NULL;

ALTER TABLE scorecard ADD COLUMN IF NOT EXISTS status     TEXT        NOT NULL DEFAULT 'draft';
ALTER TABLE scorecard ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Back-fill: any row with submitted_at already set is a real submission
UPDATE scorecard SET status = 'submitted' WHERE submitted_at IS NOT NULL AND status = 'draft';
