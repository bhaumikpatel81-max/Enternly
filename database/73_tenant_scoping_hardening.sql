-- 73_tenant_scoping_hardening.sql
--
-- v_recruiter_load (03_reports.sql) predates multi-tenancy (57/59) and has no
-- tenant_id column, so any consumer can only aggregate across ALL tenants at
-- once. It's used by pipeline_api.dashboard()'s "recruiter_load" panel for
-- ta_manager/admin, which was leaking cross-tenant recruiter load stats until
-- this fix. Add tenant_id to the view so callers can filter by it.
--
-- Inserting tenant_id ahead of full_name changes the view's column order/
-- names, which a bare CREATE OR REPLACE VIEW cannot do ("cannot change name
-- of view column \"full_name\" to \"tenant_id\""; Postgres only allows
-- CREATE OR REPLACE VIEW to append trailing columns, not reorder/rename
-- existing ones) -- drop and recreate instead. Confirmed via pg_depend that
-- nothing else references this view, so CASCADE has nothing else to drop.
-- Mirrored into backend/app/main.py's _auto_migrate() (Migration 113) since
-- this file only applies to a brand-new database's initdb pass, not an
-- already-initialized one -- see that migration's comment for why both copies
-- exist.

DROP VIEW IF EXISTS v_recruiter_load CASCADE;

CREATE OR REPLACE VIEW v_recruiter_load AS
SELECT
    u.id          AS recruiter_id,
    u.tenant_id   AS tenant_id,
    u.full_name,
    COUNT(DISTINCT rr.requisition_id) FILTER (WHERE r.status = 'open') AS open_reqs,
    COUNT(DISTINCT a.id)              AS total_applications
FROM app_user u
LEFT JOIN requisition_recruiter rr ON rr.recruiter_id = u.id
LEFT JOIN requisition r            ON r.id = rr.requisition_id
LEFT JOIN application a            ON a.requisition_id = r.id
WHERE u.role IN ('recruiter','ta_manager')
GROUP BY u.id, u.tenant_id, u.full_name;
