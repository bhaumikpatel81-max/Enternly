-- Migration 22: Real AI screening + stability dimension columns
-- Adds per-application AI fit detail, tenure analysis, and stability scoring.
-- All ALTER TABLE statements use ADD COLUMN IF NOT EXISTS (idempotent).

ALTER TABLE application ADD COLUMN IF NOT EXISTS ai_fit_score      NUMERIC;
ALTER TABLE application ADD COLUMN IF NOT EXISTS ai_screen_detail  JSONB;
ALTER TABLE application ADD COLUMN IF NOT EXISTS avg_tenure_months NUMERIC;
ALTER TABLE application ADD COLUMN IF NOT EXISTS stability_score   NUMERIC;
ALTER TABLE application ADD COLUMN IF NOT EXISTS stability_status  TEXT
    CHECK (stability_status IS NULL
        OR stability_status IN ('computed', 'pending_manual', 'not_applicable'));

COMMENT ON COLUMN application.ai_fit_score     IS '0-100 holistic fit score from Groq LLM';
COMMENT ON COLUMN application.ai_screen_detail IS 'JSON: {strengths, concerns, rationale, scored_by}';
COMMENT ON COLUMN application.avg_tenure_months IS 'Average role tenure parsed from resume (months)';
COMMENT ON COLUMN application.stability_score  IS '0-100 stability score derived from avg_tenure_months';
COMMENT ON COLUMN application.stability_status IS 'computed | pending_manual | not_applicable';
