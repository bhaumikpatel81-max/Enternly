-- Platform-managed tenant fields + placement_officer role (2026-08).
-- Doc-only snapshot of "Migration 101" in main.py's _auto_migrate(), which
-- is what actually runs on every boot.
--
-- The platform console (Feature C) needs to create, classify (Company vs
-- College), brand, and lifecycle-manage tenants -- none of tenant_type/
-- tenant_code/logo_url/primary_colour/subscription dates/grace period/
-- is_deleted existed before this. tenant.plan is intentionally NOT touched
-- here (kept as the single subscription-plan column per design decision,
-- just read/written under the alias "subscription_plan" in Python, never
-- duplicated in SQL). is_deleted is a soft-delete flag only -- tenant rows
-- are never hard-deleted.
--
-- Also widens app_user.role to add placement_officer (a College tenant's
-- own campus recruiting contact, scoped to that one tenant like any other
-- role -- no such role existed anywhere in the codebase before this).

ALTER TABLE tenant ADD COLUMN IF NOT EXISTS tenant_type TEXT NOT NULL DEFAULT 'Company'
    CHECK (tenant_type IN ('Company','College'));
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS tenant_code TEXT;
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS logo_url TEXT;
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS primary_colour TEXT;
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS subscription_start_date DATE;
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS subscription_end_date DATE;
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS grace_period_days INT NOT NULL DEFAULT 0;
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE tenant SET tenant_code = 'ET_0001' WHERE tenant_code IS NULL AND slug = 'enternstech';

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_code ON tenant(tenant_code)
    WHERE tenant_code IS NOT NULL;

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
                     'interviewer','hrbp','placement_officer'));
