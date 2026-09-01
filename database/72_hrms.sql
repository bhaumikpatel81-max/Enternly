-- hrms_provider_config / hrms_sync (2026-09). Doc-only snapshot of
-- "Migration 112" in main.py's _auto_migrate(), which is what actually
-- runs on every boot.
--
-- ATS spec §14, HRMS Multi-Provider Integration Layer: pushes
-- employee_master (Migration 111) + verified documents (Migration 105)
-- into whichever external HRMS a tenant configures
-- (successfactors/workday/oracle_hcm/darwinbox/zoho_people/bamboohr/
-- greythr). Provider credentials live in system_settings only
-- (hrms_{provider}_base / hrms_{provider}_key, per tenant) -- never in
-- hrms_provider_config or source control. Mirrors the case+child-rows
-- shape of every prior module and gates the same way via
-- require_tenant_module('hrms'). Brand-new tables, so tenant_id is
-- declared NOT NULL with a default straight from creation rather than
-- needing the add-column/backfill/set-not-null sequence in
-- 59_tenant_isolation.sql.
--
-- The inbound webhook (POST /api/integrations/hrms/webhooks/{provider})
-- carries no JWT -- it's listed in main.py's _PUBLIC_PREFIXES and instead
-- authenticates itself via an HMAC signature checked against a per-tenant
-- secret in system_settings (hrms_webhook_secret_{provider}), with the
-- tenant resolved from the stored hrms_sync row (looked up by
-- external_ref), never from the request. See services/bgv_connectors.py's
-- verify_webhook_signature (reused here, not duplicated).

CREATE TABLE IF NOT EXISTS hrms_provider_config (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    provider       TEXT NOT NULL
                   CHECK (provider IN ('successfactors','workday','oracle_hcm','darwinbox',
                                       'zoho_people','bamboohr','greythr')),
    is_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    field_mapping  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_hrms_provider_config_tenant ON hrms_provider_config(tenant_id);

CREATE TABLE IF NOT EXISTS hrms_sync (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    provider            TEXT NOT NULL,
    candidate_id        UUID NOT NULL REFERENCES candidate(id),
    employee_master_id  UUID REFERENCES employee_master(id),
    external_ref        TEXT,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','in_progress','success','failed')),
    request_payload     JSONB,
    response_summary    TEXT,
    error               TEXT,
    synced_by           UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_hrms_sync_tenant ON hrms_sync(tenant_id);
CREATE INDEX IF NOT EXISTS idx_hrms_sync_candidate ON hrms_sync(candidate_id);

INSERT INTO module_catalog (key, label, "group", default_route, icon) VALUES
  ('hrms','HRMS Integration','Onboarding','hrms','🔗')
ON CONFLICT (key) DO NOTHING;

INSERT INTO tenant_module_config (tenant_id, module_key, is_enabled, enabled_at)
SELECT t.id, 'hrms', TRUE, now() FROM tenant t
ON CONFLICT (tenant_id, module_key) DO NOTHING;
