-- module_catalog display-label renames (2026-08/09). Doc-only snapshot of
-- "Migration 106" and "Migration 108" in main.py's _auto_migrate(), which
-- is what actually runs on every boot.
--
-- Display text only -- module_catalog.key ('vendors', 'kpi_dashboard',
-- 'req_approvals') and every route/identifier that depends on it are
-- untouched. A plain UPDATE rather than editing Migration 102's INSERT
-- rows (database/64_*.sql) in place, since those rows already shipped and
-- must stay a truthful record of what actually ran.

UPDATE module_catalog SET label='Recruitment Consultant / Agency Module' WHERE key='vendors';
UPDATE module_catalog SET label='Dashboard & Analytics' WHERE key='kpi_dashboard';

-- Migration 108 (2026-09): Migration 106 renamed NAV_DEF/DELEGABLE_MODULES'
-- req_approvals label to the singular "Requisition Approval" but missed
-- module_catalog's own copy of that label -- this closes that drift.
UPDATE module_catalog SET label='Requisition Approval' WHERE key='req_approvals' AND label='Requisition Approvals';
