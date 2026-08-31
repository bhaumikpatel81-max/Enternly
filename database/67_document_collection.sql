-- document_type_config / document_request / candidate_document (2026-08).
-- Doc-only snapshot of "Migration 105" in main.py's _auto_migrate(), which
-- is what actually runs on every boot.
--
-- ATS spec §10, Document Collection & Verification: a tenant-configurable
-- list of required document types, per-candidate requests against that
-- list, and the uploaded/verified/rejected file rows themselves. Gated
-- tenant-wide via require_tenant_module('documents'), like the other
-- GATED_NAV_MODULES routers -- no per-recruiter delegation concept.
-- Brand-new tables (not a pre-tenancy retrofit), so tenant_id is declared
-- NOT NULL with a default straight from creation rather than needing the
-- add-column/backfill/set-not-null sequence in 59_tenant_isolation.sql.

CREATE TABLE IF NOT EXISTS document_type_config (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    key         TEXT NOT NULL,
    label       TEXT NOT NULL,
    is_required BOOLEAN NOT NULL DEFAULT TRUE,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, key)
);

CREATE INDEX IF NOT EXISTS idx_document_type_config_tenant ON document_type_config(tenant_id);

INSERT INTO document_type_config (tenant_id, key, label) VALUES
  ('00000000-0000-0000-0000-000000000001','pan','PAN Card'),
  ('00000000-0000-0000-0000-000000000001','aadhaar','Aadhaar Card'),
  ('00000000-0000-0000-0000-000000000001','passport','Passport'),
  ('00000000-0000-0000-0000-000000000001','education','Education Certificates'),
  ('00000000-0000-0000-0000-000000000001','relieving_letter','Relieving Letter'),
  ('00000000-0000-0000-0000-000000000001','experience_letter','Experience Letter'),
  ('00000000-0000-0000-0000-000000000001','salary_slip','Salary Slip'),
  ('00000000-0000-0000-0000-000000000001','bank_details','Bank Details')
ON CONFLICT (tenant_id, key) DO NOTHING;

CREATE TABLE IF NOT EXISTS document_request (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    candidate_id         UUID NOT NULL REFERENCES candidate(id),
    requested_doc_types  TEXT[] NOT NULL,
    requested_by         UUID REFERENCES app_user(id),
    message              TEXT,
    status               TEXT NOT NULL DEFAULT 'sent'
                         CHECK (status IN ('sent','partial','complete')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_document_request_tenant ON document_request(tenant_id);
CREATE INDEX IF NOT EXISTS idx_document_request_candidate ON document_request(candidate_id);

CREATE TABLE IF NOT EXISTS candidate_document (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    candidate_id             UUID NOT NULL REFERENCES candidate(id),
    application_id           UUID REFERENCES application(id),
    doc_type                 TEXT NOT NULL,
    file_path                TEXT,
    file_name                TEXT,
    uploaded_by              UUID,
    uploaded_at              TIMESTAMPTZ,
    status                   TEXT NOT NULL DEFAULT 'requested'
                             CHECK (status IN ('requested','uploaded','verified','rejected','compliance_review')),
    hr_verified_by           UUID,
    hr_verified_at           TIMESTAMPTZ,
    compliance_reviewed_by   UUID,
    compliance_reviewed_at   TIMESTAMPTZ,
    rejection_reason         TEXT,
    notes                    TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_candidate_document_tenant ON candidate_document(tenant_id);
CREATE INDEX IF NOT EXISTS idx_candidate_document_candidate ON candidate_document(candidate_id);

INSERT INTO module_catalog (key, label, "group", default_route, icon) VALUES
  ('documents','Document Collection & Verification','Pipeline','documents','📄')
ON CONFLICT (key) DO NOTHING;

INSERT INTO tenant_module_config (tenant_id, module_key, is_enabled, enabled_at)
SELECT t.id, 'documents', TRUE, now() FROM tenant t
ON CONFLICT (tenant_id, module_key) DO NOTHING;
