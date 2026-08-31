-- module_catalog display-label renames (2026-08). Doc-only snapshot of
-- "Migration 106" in main.py's _auto_migrate(), which is what actually
-- runs on every boot.
--
-- Display text only -- module_catalog.key ('vendors', 'kpi_dashboard') and
-- every route/identifier that depends on it are untouched. A plain UPDATE
-- rather than editing Migration 102's INSERT rows (database/64_*.sql) in
-- place, since those rows already shipped and must stay a truthful record
-- of what actually ran.

UPDATE module_catalog SET label='Recruitment Consultant / Agency Module' WHERE key='vendors';
UPDATE module_catalog SET label='Dashboard & Analytics' WHERE key='kpi_dashboard';
