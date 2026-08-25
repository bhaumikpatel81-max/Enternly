-- Session freshness, per-tenant role labels, and a client/RPO provision
-- (2026-08). Doc-only snapshot of "Migration 95" in main.py's
-- _auto_migrate(), which is what actually runs on every boot.
--
-- 1) token_version on app_user -- lets a role/company change or an
--    admin-forced logout invalidate an already-issued JWT immediately,
--    instead of waiting up to TOKEN_HOURS for it to expire naturally.
-- 2) tenant.role_labels -- every customer uses different job titles for the
--    same underlying role; this only overrides what a role is CALLED for
--    one tenant, never the role key itself.
-- 3) client table + requisition.client_id -- some customers are
--    staffing/RPO agencies hiring on behalf of external clients, not only
--    for their own internal roles.

ALTER TABLE app_user ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0;

ALTER TABLE tenant ADD COLUMN IF NOT EXISTS role_labels JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS client (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenant(id),
    name        TEXT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

ALTER TABLE requisition ADD COLUMN IF NOT EXISTS client_id UUID REFERENCES client(id);

CREATE INDEX IF NOT EXISTS idx_requisition_client ON requisition(client_id);
CREATE INDEX IF NOT EXISTS idx_client_tenant ON client(tenant_id);
