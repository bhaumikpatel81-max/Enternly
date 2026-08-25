-- Tenant isolation completion (2026-08). Doc-only snapshot of "Migration 96"
-- in main.py's _auto_migrate(), which is what actually runs on every boot.
--
-- Migrations 94/95 added tenant_id to app_user/group_company/client only.
-- Every other tenant-owned table (requisitions, candidates, vendors, bands,
-- HRBPs, templates, config) was still one shared pool across every
-- customer. This adds tenant_id to each of them, backfills every existing
-- row onto the single seeded tenant, and re-scopes every unique constraint
-- that could otherwise collide across two different customers.

-- requisition
ALTER TABLE requisition ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE requisition SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE requisition ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE requisition ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_requisition_tenant ON requisition(tenant_id);

-- candidate (email uniqueness re-scoped per tenant)
ALTER TABLE candidate ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE candidate SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE candidate ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE candidate ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_candidate_tenant ON candidate(tenant_id);
DROP INDEX IF EXISTS uidx_candidate_email;
CREATE UNIQUE INDEX IF NOT EXISTS uidx_candidate_email ON candidate (tenant_id, LOWER(email));

-- candidate_user
ALTER TABLE candidate_user ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE candidate_user SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE candidate_user ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE candidate_user ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE candidate_user DROP CONSTRAINT IF EXISTS candidate_user_email_key;
ALTER TABLE candidate_user ADD CONSTRAINT candidate_user_tenant_email_key UNIQUE (tenant_id, email);

-- vendor / vendor_user
ALTER TABLE vendor ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE vendor SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE vendor ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE vendor ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_vendor_tenant ON vendor(tenant_id);

ALTER TABLE vendor_user ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE vendor_user SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE vendor_user ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE vendor_user ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE vendor_user DROP CONSTRAINT IF EXISTS vendor_user_email_key;
ALTER TABLE vendor_user ADD CONSTRAINT vendor_user_tenant_email_key UNIQUE (tenant_id, email);

-- band
ALTER TABLE band ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE band SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE band ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE band ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE band DROP CONSTRAINT IF EXISTS band_code_key;
ALTER TABLE band ADD CONSTRAINT band_tenant_code_key UNIQUE (tenant_id, code);

-- hrbp
ALTER TABLE hrbp ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE hrbp SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE hrbp ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE hrbp ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE hrbp DROP CONSTRAINT IF EXISTS hrbp_email_key;
ALTER TABLE hrbp ADD CONSTRAINT hrbp_tenant_email_key UNIQUE (tenant_id, email);

-- former_employee
ALTER TABLE former_employee ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE former_employee SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE former_employee ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE former_employee ALTER COLUMN tenant_id SET NOT NULL;
DROP INDEX IF EXISTS idx_former_employee_email;
CREATE UNIQUE INDEX IF NOT EXISTS idx_former_employee_email ON former_employee(tenant_id, LOWER(email));

-- no_poach_company
ALTER TABLE no_poach_company ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE no_poach_company SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE no_poach_company ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE no_poach_company ALTER COLUMN tenant_id SET NOT NULL;
DROP INDEX IF EXISTS idx_no_poach_normalized;
CREATE UNIQUE INDEX IF NOT EXISTS idx_no_poach_normalized ON no_poach_company(tenant_id, normalized_name);

-- email_template
ALTER TABLE email_template ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE email_template SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE email_template ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE email_template ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE email_template DROP CONSTRAINT IF EXISTS email_template_template_key_key;
ALTER TABLE email_template ADD CONSTRAINT email_template_tenant_key_key UNIQUE (tenant_id, template_key);

-- offer_chain_template
ALTER TABLE offer_chain_template ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE offer_chain_template SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE offer_chain_template ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE offer_chain_template ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_offer_chain_template_tenant ON offer_chain_template(tenant_id);

-- feedback_form
ALTER TABLE feedback_form ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE feedback_form SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE feedback_form ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE feedback_form ALTER COLUMN tenant_id SET NOT NULL;
DROP INDEX IF EXISTS idx_feedback_form_name_ci;
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_form_name_ci ON feedback_form (tenant_id, LOWER(name));

-- cv_repository
ALTER TABLE cv_repository ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE cv_repository SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE cv_repository ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE cv_repository ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cv_repository_tenant ON cv_repository(tenant_id);
ALTER TABLE cv_repository DROP CONSTRAINT IF EXISTS cv_repository_file_hash_key;
ALTER TABLE cv_repository ADD CONSTRAINT cv_repository_tenant_hash_key UNIQUE (tenant_id, file_hash);

-- hiring_plan_rows
ALTER TABLE hiring_plan_rows ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE hiring_plan_rows SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE hiring_plan_rows ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE hiring_plan_rows ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_hiring_plan_rows_tenant ON hiring_plan_rows(tenant_id);

-- gamification_event / gamification_badge
ALTER TABLE gamification_event ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE gamification_event SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE gamification_event ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE gamification_event ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_gev_tenant ON gamification_event(tenant_id);

ALTER TABLE gamification_badge ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE gamification_badge SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE gamification_badge ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE gamification_badge ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE gamification_badge DROP CONSTRAINT IF EXISTS gamification_badge_subject_type_subject_id_badge_key_key;
ALTER TABLE gamification_badge ADD CONSTRAINT gamification_badge_tenant_subject_key
    UNIQUE (tenant_id, subject_type, subject_id, badge_key);

-- google_calendar_connection (was implicitly a single row for the whole
-- deployment -- one row per tenant from here on)
ALTER TABLE google_calendar_connection ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE google_calendar_connection SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE google_calendar_connection ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE google_calendar_connection ALTER COLUMN tenant_id SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_gcal_conn_tenant ON google_calendar_connection(tenant_id);

-- system_settings / sla_config / gamification_config (were single
-- shared-platform-wide config rows)
ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE system_settings SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE system_settings ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE system_settings ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE system_settings DROP CONSTRAINT IF EXISTS system_settings_pkey;
ALTER TABLE system_settings ADD CONSTRAINT system_settings_pkey PRIMARY KEY (tenant_id, key);

ALTER TABLE sla_config ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE sla_config SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE sla_config ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE sla_config ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE sla_config DROP CONSTRAINT IF EXISTS sla_config_config_key_key;
ALTER TABLE sla_config ADD CONSTRAINT sla_config_tenant_key_key UNIQUE (tenant_id, config_key);

ALTER TABLE gamification_config ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE gamification_config SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE gamification_config ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE gamification_config ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE gamification_config DROP CONSTRAINT IF EXISTS gamification_config_pkey;
ALTER TABLE gamification_config ADD CONSTRAINT gamification_config_pkey PRIMARY KEY (tenant_id, key);

-- group_company.name was globally unique despite already carrying tenant_id
ALTER TABLE group_company DROP CONSTRAINT IF EXISTS group_company_name_key;
ALTER TABLE group_company ADD CONSTRAINT group_company_tenant_name_key UNIQUE (tenant_id, name);
