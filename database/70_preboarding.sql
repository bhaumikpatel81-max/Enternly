-- preboarding_case / preboarding_content / preboarding_policy_ack /
-- asset_task (2026-09). Doc-only snapshot of "Migration 109" in main.py's
-- _auto_migrate(), which is what actually runs on every boot.
--
-- ATS spec §12, Preboarding & Asset Allocation: a case per candidate once
-- their offer is accepted, tenant-configurable welcome/policy content,
-- per-case policy acknowledgements, and asset-allocation requests routed
-- to IT/Admin/HR/Security. Mirrors Migrations 105/107's case+child-rows
-- shape and gates the same way via require_tenant_module('preboarding').
-- Brand-new tables (not a pre-tenancy retrofit), so tenant_id is declared
-- NOT NULL with a default straight from creation rather than needing the
-- add-column/backfill/set-not-null sequence in 59_tenant_isolation.sql.
--
-- Preboarding is initiated via its own POST .../initiate endpoint
-- (staff-triggered, guarded on the candidate having an accepted offer) --
-- it never fires automatically off the offer flow, so offers_api.py stays
-- untouched.

CREATE TABLE IF NOT EXISTS preboarding_case (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    candidate_id          UUID NOT NULL REFERENCES candidate(id),
    application_id        UUID REFERENCES application(id),
    offer_id              UUID,
    status                TEXT NOT NULL DEFAULT 'not_started'
                          CHECK (status IN ('not_started','in_progress','ready','joined')),
    portal_access_token   TEXT,
    initiated_by          UUID REFERENCES app_user(id),
    initiated_at          TIMESTAMPTZ,
    joining_date          DATE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_preboarding_case_tenant ON preboarding_case(tenant_id);
CREATE INDEX IF NOT EXISTS idx_preboarding_case_candidate ON preboarding_case(candidate_id);

CREATE TABLE IF NOT EXISTS preboarding_content (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    title          TEXT NOT NULL,
    content_type   TEXT NOT NULL
                   CHECK (content_type IN ('company_info','welcome_video','org_structure','policy')),
    body           TEXT,
    url            TEXT,
    display_order  INT NOT NULL DEFAULT 0,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_preboarding_content_tenant ON preboarding_content(tenant_id);

-- Seed defaults for the seed tenant only, guarded by NOT EXISTS since
-- there's no natural unique key to ON CONFLICT against.
INSERT INTO preboarding_content (tenant_id, title, content_type, body, display_order)
SELECT '00000000-0000-0000-0000-000000000001', 'Welcome', 'company_info',
       'Welcome to the team! We''re excited to have you on board.', 0
WHERE NOT EXISTS (
  SELECT 1 FROM preboarding_content
  WHERE tenant_id = '00000000-0000-0000-0000-000000000001' AND content_type = 'company_info'
);

INSERT INTO preboarding_content (tenant_id, title, content_type, body, display_order)
SELECT '00000000-0000-0000-0000-000000000001', 'Our Organisation', 'org_structure',
       'Org chart placeholder — replace with your company''s actual structure.', 1
WHERE NOT EXISTS (
  SELECT 1 FROM preboarding_content
  WHERE tenant_id = '00000000-0000-0000-0000-000000000001' AND content_type = 'org_structure'
);

CREATE TABLE IF NOT EXISTS preboarding_policy_ack (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    preboarding_case_id   UUID NOT NULL REFERENCES preboarding_case(id) ON DELETE CASCADE,
    policy_key            TEXT NOT NULL,
    policy_label          TEXT NOT NULL,
    acknowledged          BOOLEAN NOT NULL DEFAULT FALSE,
    acknowledged_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_preboarding_policy_ack_tenant ON preboarding_policy_ack(tenant_id);

-- Policy keys (hr_policy, leave_policy, insurance, code_of_conduct,
-- it_security) are seeded per-case at /initiate time from a constant in
-- preboarding_api.py, not from a separate config table -- the spec left
-- this choice open and a per-case seed keeps the schema to exactly the
-- four tables above.

CREATE TABLE IF NOT EXISTS asset_task (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    preboarding_case_id   UUID NOT NULL REFERENCES preboarding_case(id) ON DELETE CASCADE,
    asset_type            TEXT NOT NULL,
    assigned_team         TEXT NOT NULL CHECK (assigned_team IN ('IT','Admin','HR','Security')),
    status                TEXT NOT NULL DEFAULT 'requested'
                          CHECK (status IN ('requested','in_progress','assigned','completed','cancelled')),
    notes                 TEXT,
    requested_by          UUID,
    updated_by            UUID,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_asset_task_tenant ON asset_task(tenant_id);
CREATE INDEX IF NOT EXISTS idx_asset_task_case ON asset_task(preboarding_case_id);

INSERT INTO module_catalog (key, label, "group", default_route, icon) VALUES
  ('preboarding','Preboarding & Asset Allocation','Onboarding','preboarding','🎒')
ON CONFLICT (key) DO NOTHING;

INSERT INTO tenant_module_config (tenant_id, module_key, is_enabled, enabled_at)
SELECT t.id, 'preboarding', TRUE, now() FROM tenant t
ON CONFLICT (tenant_id, module_key) DO NOTHING;

-- ── Migration 110 (2026-09): scheduled + confirmed chaining ─────────────
-- Doc-only snapshot of "Migration 110" in main.py's _auto_migrate(). Adds
-- the "joining date near -> propose -> human confirms -> portal opens"
-- flow on top of the preboarding_case/preboarding_policy_ack tables above,
-- without touching their existing columns/rows. 'proposed' is a new
-- pre-in_progress status written only by
-- services/preboarding_proposer_worker.py's daily loop -- it never seeds
-- policy acks or opens the portal; POST /candidates/{id}/confirm does that.

ALTER TABLE preboarding_case ADD COLUMN IF NOT EXISTS confirmed_by UUID;
ALTER TABLE preboarding_case ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;
ALTER TABLE preboarding_case ADD COLUMN IF NOT EXISTS auto_proposed BOOLEAN NOT NULL DEFAULT FALSE;

-- Widen preboarding_case.status — drop old CHECK and replace (name varies by Postgres)
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'preboarding_case'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE preboarding_case DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$;

ALTER TABLE preboarding_case ADD CONSTRAINT preboarding_case_status_check
    CHECK (status IN ('proposed','not_started','in_progress','ready','joined'));

CREATE TABLE IF NOT EXISTS preboarding_config (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              UUID NOT NULL UNIQUE REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    days_before_joining    INT NOT NULL DEFAULT 14,
    auto_propose_enabled   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO preboarding_config (tenant_id) VALUES ('00000000-0000-0000-0000-000000000001')
ON CONFLICT (tenant_id) DO NOTHING;
