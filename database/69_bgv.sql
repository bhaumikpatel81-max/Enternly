-- bgv_check_type_config / bgv_case / bgv_check (2026-08). Doc-only
-- snapshot of "Migration 107" in main.py's _auto_migrate(), which is what
-- actually runs on every boot.
--
-- ATS spec §10.1, Background Verification: a tenant-configurable list of
-- BGV check types, one bgv_case per candidate verification run, and the
-- individual bgv_check rows within it. Mirrors Migration 105's Document
-- Collection shape (config-over-code check types + case/check split) and
-- gates the same way via require_tenant_module('bgv'). Brand-new tables
-- (not a pre-tenancy retrofit), so tenant_id is declared NOT NULL with a
-- default straight from creation rather than needing the add-column/
-- backfill/set-not-null sequence in 59_tenant_isolation.sql.

CREATE TABLE IF NOT EXISTS bgv_check_type_config (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    key        TEXT NOT NULL,
    label      TEXT NOT NULL,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, key)
);

CREATE INDEX IF NOT EXISTS idx_bgv_check_type_config_tenant ON bgv_check_type_config(tenant_id);

INSERT INTO bgv_check_type_config (tenant_id, key, label) VALUES
  ('00000000-0000-0000-0000-000000000001','employment','Employment Verification'),
  ('00000000-0000-0000-0000-000000000001','education','Education Verification'),
  ('00000000-0000-0000-0000-000000000001','identity','Identity Verification'),
  ('00000000-0000-0000-0000-000000000001','criminal','Criminal Record Check'),
  ('00000000-0000-0000-0000-000000000001','reference','Reference Check'),
  ('00000000-0000-0000-0000-000000000001','address','Address Verification')
ON CONFLICT (tenant_id, key) DO NOTHING;

CREATE TABLE IF NOT EXISTS bgv_case (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    candidate_id    UUID NOT NULL REFERENCES candidate(id),
    application_id  UUID REFERENCES application(id),
    provider        TEXT NOT NULL DEFAULT 'manual',
    external_ref    TEXT,
    overall_status  TEXT NOT NULL DEFAULT 'pending'
                    CHECK (overall_status IN ('pending','in_progress','flagged','approved','rejected')),
    initiated_by    UUID REFERENCES app_user(id),
    initiated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_bgv_case_tenant ON bgv_case(tenant_id);
CREATE INDEX IF NOT EXISTS idx_bgv_case_candidate ON bgv_case(candidate_id);

CREATE TABLE IF NOT EXISTS bgv_check (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    bgv_case_id     UUID NOT NULL REFERENCES bgv_case(id) ON DELETE CASCADE,
    check_type      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','in_progress','flagged','approved','rejected')),
    result_summary  TEXT,
    evidence_url    TEXT,
    updated_by      UUID,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bgv_check_case ON bgv_check(bgv_case_id);

INSERT INTO module_catalog (key, label, "group", default_route, icon) VALUES
  ('bgv','Background Verification','Pipeline','bgv','🛡️')
ON CONFLICT (key) DO NOTHING;

INSERT INTO tenant_module_config (tenant_id, module_key, is_enabled, enabled_at)
SELECT t.id, 'bgv', TRUE, now() FROM tenant t
ON CONFLICT (tenant_id, module_key) DO NOTHING;
