-- Migration 08: report fields, aging view, Enteri AI session table
-- Run AFTER 01-07 migrations.

-- ── Requisition: priority, risk, location ────────────────────────────────────
ALTER TABLE requisition
  ADD COLUMN IF NOT EXISTS is_p1           BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS risk            TEXT
    CONSTRAINT requisition_risk_check CHECK (risk IN ('low','medium','high','critical')),
  ADD COLUMN IF NOT EXISTS hiring_location TEXT;

-- ── Application: internal movement flag ──────────────────────────────────────
ALTER TABLE application
  ADD COLUMN IF NOT EXISTS is_internal_movement BOOLEAN NOT NULL DEFAULT FALSE;

-- ── Computed view: open requisition aging ────────────────────────────────────
CREATE OR REPLACE VIEW v_requisition_aging AS
SELECT
    r.id,
    r.title,
    r.status,
    r.roll_type,
    r.fiscal_year,
    r.is_p1,
    r.risk,
    r.hiring_location,
    gc.name  AS company,
    bu.name  AS business_unit,
    b.code   AS band,
    EXTRACT(DAY FROM (now() - r.opened_at))::INT AS aging_days,
    CASE
      WHEN EXTRACT(DAY FROM (now() - r.opened_at)) <= 15 THEN '0-15'
      WHEN EXTRACT(DAY FROM (now() - r.opened_at)) <= 30 THEN '16-30'
      WHEN EXTRACT(DAY FROM (now() - r.opened_at)) <= 45 THEN '31-45'
      WHEN EXTRACT(DAY FROM (now() - r.opened_at)) <= 60 THEN '46-60'
      WHEN EXTRACT(DAY FROM (now() - r.opened_at)) <= 90 THEN '61-90'
      ELSE '91+'
    END AS aging_bracket
FROM requisition r
JOIN business_unit bu ON bu.id = r.bu_id
JOIN group_company gc ON gc.id = bu.company_id
JOIN band           b  ON b.id  = r.band_id
WHERE r.status = 'open'
  AND r.opened_at IS NOT NULL;

-- ── Enteri AI session table ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS enteri_ai_session (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    requisition_id  UUID NOT NULL REFERENCES requisition(id),
    questions       JSONB,
    transcript      JSONB,
    raw_score       NUMERIC,
    score_detail    JSONB,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','in_progress','completed','failed')),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_enteri_ai_app     ON enteri_ai_session(application_id);
CREATE INDEX        IF NOT EXISTS idx_enteri_ai_req     ON enteri_ai_session(requisition_id);
CREATE INDEX        IF NOT EXISTS idx_enteri_ai_status  ON enteri_ai_session(status);
