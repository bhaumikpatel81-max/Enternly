-- ============================================================
-- Migration 33: Gamification + criticality flag
-- ============================================================

-- 1. Criticality flag on requisition ---------------------------
ALTER TABLE requisition ADD COLUMN IF NOT EXISTS criticality TEXT NOT NULL DEFAULT 'Medium'
    CHECK (criticality IN ('Low','Medium','High','Critical'));

-- 2. Gamification event ledger (append-only — never updated) ---
CREATE TABLE IF NOT EXISTS gamification_event (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_type    TEXT NOT NULL CHECK (subject_type IN ('recruiter','vendor','candidate','hm')),
    subject_id      UUID NOT NULL,
    event_type      TEXT NOT NULL,
    base_points     NUMERIC NOT NULL,
    criticality     TEXT NOT NULL DEFAULT 'Medium',
    multiplier      NUMERIC NOT NULL DEFAULT 1.0,
    points_awarded  NUMERIC NOT NULL,
    requisition_id  UUID REFERENCES requisition(id) ON DELETE SET NULL,
    application_id  UUID REFERENCES application(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gev_subject   ON gamification_event(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_gev_req       ON gamification_event(requisition_id);
CREATE INDEX IF NOT EXISTS idx_gev_app       ON gamification_event(application_id);
CREATE INDEX IF NOT EXISTS idx_gev_created   ON gamification_event(created_at);

-- 3. Config table — all thresholds live here; tune without code -
CREATE TABLE IF NOT EXISTS gamification_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by UUID REFERENCES app_user(id)
);

-- Seed base event points
INSERT INTO gamification_config (key, value) VALUES
  ('points.offer_within_sla',   '50'),
  ('points.fast_screen',        '20'),
  ('points.offer_accepted',     '80'),
  ('points.offer_joined',       '100'),
  ('points.panel_pass',         '30'),
  ('points.feedback_on_time',   '25'),
  ('points.sla_met_stage',      '15'),
  ('points.submission',         '5'),
  ('points.candidate_advanced', '10'),
  -- Criticality multipliers
  ('multiplier.Low',            '1.0'),
  ('multiplier.Medium',         '1.5'),
  ('multiplier.High',           '2.5'),
  ('multiplier.Critical',       '4.0'),
  -- Tier thresholds (cumulative points)
  ('tier.bronze',               '0'),
  ('tier.silver',               '200'),
  ('tier.gold',                 '600'),
  ('tier.platinum',             '1500')
ON CONFLICT (key) DO NOTHING;

-- 4. Badges (named achievements) --------------------------------
CREATE TABLE IF NOT EXISTS gamification_badge (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_type TEXT NOT NULL CHECK (subject_type IN ('recruiter','vendor','candidate','hm')),
    subject_id   UUID NOT NULL,
    badge_key    TEXT NOT NULL,
    earned_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (subject_type, subject_id, badge_key)
);

CREATE INDEX IF NOT EXISTS idx_gbadge_subject ON gamification_badge(subject_type, subject_id);
