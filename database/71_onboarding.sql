-- employee_master / onboarding_case / onboarding_task (2026-09). Doc-only
-- snapshot of "Migration 111" in main.py's _auto_migrate(), which is what
-- actually runs on every boot.
--
-- ATS spec §13, Onboarding & Employee Master: fires on Day-1 (joining
-- date), independent of preboarding -- there is no automatic trigger from
-- the preboarding or offer flows; staff call POST .../day-one and
-- POST .../convert-to-employee explicitly. employee_master is the record
-- §14's future HRMS sync will read, so its field names are kept plain and
-- complete rather than abbreviated. Mirrors the case+child-rows shape of
-- every prior onboarding-family module and gates the same way via
-- require_tenant_module('onboarding'). Brand-new tables, so tenant_id is
-- declared NOT NULL with a default straight from creation rather than
-- needing the add-column/backfill/set-not-null sequence in
-- 59_tenant_isolation.sql.

CREATE TABLE IF NOT EXISTS employee_master (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    candidate_id    UUID NOT NULL REFERENCES candidate(id),
    application_id  UUID REFERENCES application(id),
    employee_code   TEXT NOT NULL,
    designation     TEXT,
    department_id   UUID REFERENCES business_unit(id),
    manager_id      UUID REFERENCES app_user(id),
    location        TEXT,
    grade           TEXT,
    cost_center     TEXT,
    joining_date    DATE,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','pre_sync','inactive')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, employee_code)
);

CREATE INDEX IF NOT EXISTS idx_employee_master_tenant ON employee_master(tenant_id);
CREATE INDEX IF NOT EXISTS idx_employee_master_candidate ON employee_master(candidate_id);

CREATE TABLE IF NOT EXISTS onboarding_case (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    candidate_id        UUID NOT NULL REFERENCES candidate(id),
    employee_master_id  UUID REFERENCES employee_master(id),
    status              TEXT NOT NULL DEFAULT 'not_started'
                        CHECK (status IN ('not_started','day_one','completed')),
    day_one_at          TIMESTAMPTZ,
    initiated_by        UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_onboarding_case_tenant ON onboarding_case(tenant_id);
CREATE INDEX IF NOT EXISTS idx_onboarding_case_candidate ON onboarding_case(candidate_id);

CREATE TABLE IF NOT EXISTS onboarding_task (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    onboarding_case_id   UUID NOT NULL REFERENCES onboarding_case(id) ON DELETE CASCADE,
    task_key             TEXT NOT NULL,
    task_label           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done')),
    completed_by         UUID,
    completed_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_onboarding_task_tenant ON onboarding_task(tenant_id);
CREATE INDEX IF NOT EXISTS idx_onboarding_task_case ON onboarding_task(onboarding_case_id);

-- Day-1 checklist (welcome_letter, induction, policy_acceptance,
-- credential_activation) is seeded per-case at /day-one time from a
-- constant in onboarding_api.py, matching preboarding's policy-ack seeding
-- convention -- no separate config table.

INSERT INTO module_catalog (key, label, "group", default_route, icon) VALUES
  ('onboarding','Onboarding & Employee Master','Onboarding','onboarding','🪪')
ON CONFLICT (key) DO NOTHING;

INSERT INTO tenant_module_config (tenant_id, module_key, is_enabled, enabled_at)
SELECT t.id, 'onboarding', TRUE, now() FROM tenant t
ON CONFLICT (tenant_id, module_key) DO NOTHING;
