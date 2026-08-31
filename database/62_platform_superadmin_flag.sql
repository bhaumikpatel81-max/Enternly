-- Platform-superadmin / company-admin boolean flags (2026-08). Doc-only
-- snapshot of "Migration 100" in main.py's _auto_migrate(), which is what
-- actually runs on every boot.
--
-- Enternly's platform_admin/company_admin roles today are pure aliases
-- layered onto the single app_user.role string -- no dedicated platform
-- console, tenant CRUD, or deliberate cross-tenant reach exists yet. This
-- adds two explicit, authoritative boolean flags so gating no longer
-- depends on parsing/enumerating role strings: an account can be flagged
-- is_platform_superadmin (Enternstech staff managing the whole tenant
-- roster) and/or is_company_admin (a customer's own super admin)
-- independently of whatever `role` value it also carries for
-- labeling/nav purposes. Existing platform_admin/company_admin/admin
-- accounts are backfilled onto the flags so no one is locked out; going
-- forward, new gating reads the flags, not the role string.
--
-- Also adds app_user.last_login_at -- no prior column tracked this.

ALTER TABLE app_user ADD COLUMN IF NOT EXISTS is_platform_superadmin BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS is_company_admin BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;

UPDATE app_user SET is_platform_superadmin = TRUE
  WHERE role IN ('platform_admin','admin')
    AND tenant_id = '00000000-0000-0000-0000-000000000001';

UPDATE app_user SET is_company_admin = TRUE
  WHERE role IN ('company_admin','admin')
    AND tenant_id <> '00000000-0000-0000-0000-000000000001';
