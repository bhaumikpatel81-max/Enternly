-- module_catalog / tenant_module_config / subscription_plan_config
-- (2026-08). Doc-only snapshot of "Migration 102" in main.py's
-- _auto_migrate(), which is what actually runs on every boot.
--
-- Feature D's tenant-level module gate: a module must be enabled for the
-- TENANT before any of its users -- even a company admin -- can use it;
-- module_access.py's existing per-recruiter delegation is the inner gate
-- on top of this outer one. subscription_plan_config is created here (not
-- deferred to the later Subscriptions feature) so the plan/module live
-- constraint (a tenant can never enable a module its plan doesn't allow)
-- ships complete in the same migration as the tables it constrains.
-- Every existing tenant is defaulted to all-modules-enabled so nothing
-- regresses on deploy.

CREATE TABLE IF NOT EXISTS module_catalog (
    key           TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    "group"       TEXT,
    default_route TEXT,
    icon          TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenant_module_config (
    tenant_id    UUID NOT NULL REFERENCES tenant(id),
    module_key   TEXT NOT NULL REFERENCES module_catalog(key),
    is_enabled   BOOLEAN NOT NULL DEFAULT TRUE,
    enabled_at   TIMESTAMPTZ,
    disabled_at  TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, module_key)
);

CREATE TABLE IF NOT EXISTS subscription_plan_config (
    plan_name            TEXT PRIMARY KEY,
    allowed_modules_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    price_monthly        NUMERIC,
    price_yearly         NUMERIC,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- '[]' = no restriction (every module allowed) -- the safe interpretation
-- for a plan that pre-dates this feature, so the existing 'standard' plan
-- doesn't suddenly lock every tenant out the moment this migration runs.
INSERT INTO subscription_plan_config (plan_name, allowed_modules_json)
VALUES ('standard', '[]'::jsonb) ON CONFLICT (plan_name) DO NOTHING;

INSERT INTO module_catalog (key, label, "group", default_route, icon) VALUES
  ('vendors','Vendor Management','Admin','vendors','🤝'),
  ('form_fields','Application Form Fields','Admin','form_fields','🧾'),
  ('req_approvals','Requisition Approvals','Admin','ta_req_approvals','📝'),
  ('organisation','Organisation','Admin','organisation','🏢'),
  ('sla_settings','SLA Settings','Admin','sla_settings','⏱'),
  ('chain_templates','Approval Chain Templates','Admin','chain_templates','⛓'),
  ('email_templates','Email Templates','Admin','email_templates','✉️'),
  ('campus_hiring','Campus Hiring','Sourcing','campus_hiring','🎓'),
  ('enteri_ai_tracker','Enteri AI','Sourcing','enteri_ai_tracker','🤖'),
  ('kpi_dashboard','KPI Dashboard','Analytics','kpi_dashboard','📈'),
  ('gamification','Leaderboard','Analytics','gamification','🏆'),
  ('proctoring_review','Proctoring','Pipeline','proctoring_review','🔍'),
  ('hiring_plan','Hiring Plan','Sourcing','hiring_plan','📑'),
  ('cv_repository','CV Repository','Admin','cv_repository','📂'),
  ('ai_scorecard','AI Scorecard','Admin','ai_scorecard','🎯'),
  ('no_poach','No Poach List','Admin','no_poach','🚫')
ON CONFLICT (key) DO NOTHING;

INSERT INTO tenant_module_config (tenant_id, module_key, is_enabled, enabled_at)
SELECT t.id, m.key, TRUE, now() FROM tenant t CROSS JOIN module_catalog m
ON CONFLICT (tenant_id, module_key) DO NOTHING;
