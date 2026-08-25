-- Multi-tenant platform roles (2026-08). Doc-only snapshot of the migration
-- main.py's _auto_migrate() actually runs on every boot (see "Migration 94"
-- there) -- this file exists so a FRESH install ends up at the same schema.
--
-- Adds a `tenant` table (one row per customer company) and a tenant_id
-- column on app_user/group_company, backfilled onto a single seeded tenant
-- so an existing single-customer deployment is unaffected. Also widens
-- app_user.role to add platform_admin (Enternstech -- manages the tenant
-- roster) and company_admin (a customer's own super admin), and fixes the
-- pre-existing drift where 'hrbp' (Migration 49) was never added to a
-- committed snapshot of this constraint.

CREATE TABLE IF NOT EXISTS tenant (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                  TEXT NOT NULL,
    slug                  TEXT NOT NULL UNIQUE,
    status                TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','trial','suspended')),
    plan                  TEXT NOT NULL DEFAULT 'standard',
    primary_contact_email TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO tenant (id, name, slug, status, plan)
VALUES ('00000000-0000-0000-0000-000000000001',
        'EnternsTech Pvt. Ltd.', 'enternstech', 'active', 'standard')
ON CONFLICT (slug) DO NOTHING;

ALTER TABLE app_user ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE app_user SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE app_user ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app_user ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE group_company ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenant(id);
UPDATE group_company SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
ALTER TABLE group_company ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE group_company ALTER COLUMN tenant_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_app_user_tenant ON app_user(tenant_id);
CREATE INDEX IF NOT EXISTS idx_group_company_tenant ON group_company(tenant_id);

DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'app_user'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%role%'
    LOOP
        EXECUTE 'ALTER TABLE app_user DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$;

ALTER TABLE app_user ADD CONSTRAINT app_user_role_check
    CHECK (role IN ('platform_admin','company_admin','admin','ta_manager',
                     'recruiter','hiring_manager','bu_head','director',
                     'interviewer','hrbp'));
