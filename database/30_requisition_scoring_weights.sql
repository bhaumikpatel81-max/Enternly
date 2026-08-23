-- Migration 39 (auto-migrate slot 39)
-- Per-requisition scoring weights, fresher-role flag, and panel_consensus column.
--
-- resume_weight + interview_weight: control the combined-score blend for
--   each requisition (default 0.40 / 0.60). Must sum to 1.0 — the service
--   layer normalises in case of floating-point drift.
-- is_fresher_role: when TRUE, forces the fresher scoring model (education +
--   project relevance + keyword + AI) regardless of candidate experience years.
-- panel_consensus on application: 'advance' | 'reject' | 'split' — computed
--   and stored on every scorecard submission so list queries need no JSONB parse.
--
-- NOTE: SQL files in database/ are documentation only — actual execution happens
--       via the inline auto-migrate list in backend/app/main.py.

ALTER TABLE requisition ADD COLUMN IF NOT EXISTS resume_weight    NUMERIC(4,2) DEFAULT 0.40;
ALTER TABLE requisition ADD COLUMN IF NOT EXISTS interview_weight NUMERIC(4,2) DEFAULT 0.60;
ALTER TABLE requisition ADD COLUMN IF NOT EXISTS is_fresher_role  BOOLEAN      DEFAULT FALSE;

ALTER TABLE application ADD COLUMN IF NOT EXISTS panel_consensus  TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'application'::regclass
          AND conname  = 'application_panel_consensus_check'
    ) THEN
        EXECUTE $sql$
            ALTER TABLE application ADD CONSTRAINT application_panel_consensus_check
            CHECK (panel_consensus IS NULL OR panel_consensus IN ('advance','reject','split'))
        $sql$;
    END IF;
END $$;
