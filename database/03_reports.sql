-- ============================================================
-- ONE CLICK HIRE  -  Reporting views (Phase 1)
-- Every report you asked for, as a query that runs on the schema.
-- These become the dashboards and the scheduled report emails
-- in Phase 5. Run AFTER 01_schema.sql.
-- ============================================================

-- ------------------------------------------------------------
-- 1. TAT PER OPEN POSITION
-- Time each application spent moving between stages, plus total
-- time-to-fill per requisition.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_application_tat AS
SELECT
    se.application_id,
    a.requisition_id,
    se.from_status,
    se.to_status,
    se.occurred_at,
    EXTRACT(EPOCH FROM (
        se.occurred_at - LAG(se.occurred_at)
        OVER (PARTITION BY se.application_id ORDER BY se.occurred_at)
    )) / 86400.0 AS days_in_previous_stage
FROM stage_event se
JOIN application a ON a.id = se.application_id;

CREATE OR REPLACE VIEW v_req_time_to_fill AS
SELECT
    r.id              AS requisition_id,
    r.title,
    r.status,
    r.opened_at,
    MIN(o.created_at) FILTER (WHERE o.status = 'released') AS first_offer_at,
    EXTRACT(EPOCH FROM (
        MIN(o.created_at) FILTER (WHERE o.status = 'released') - r.opened_at
    )) / 86400.0 AS days_to_first_offer
FROM requisition r
LEFT JOIN application a ON a.requisition_id = r.id
LEFT JOIN offer o       ON o.application_id = a.id
GROUP BY r.id, r.title, r.status, r.opened_at;

-- ------------------------------------------------------------
-- 2. MULTIPLE RECRUITERS PER REQUISITION
-- Who is working on what, and the load per recruiter.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_recruiter_load AS
SELECT
    u.id          AS recruiter_id,
    u.full_name,
    COUNT(DISTINCT rr.requisition_id) FILTER (WHERE r.status = 'open') AS open_reqs,
    COUNT(DISTINCT a.id)              AS total_applications
FROM app_user u
LEFT JOIN requisition_recruiter rr ON rr.recruiter_id = u.id
LEFT JOIN requisition r            ON r.id = rr.requisition_id
LEFT JOIN application a            ON a.requisition_id = r.id
WHERE u.role IN ('recruiter','ta_manager')
GROUP BY u.id, u.full_name;

-- ------------------------------------------------------------
-- 3. DIVERSITY (gender) BIFURCATION
-- Male/female/undisclosed split at every stage, per requisition.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_gender_split AS
SELECT
    r.id   AS requisition_id,
    r.title,
    c.gender,
    a.status,
    COUNT(*) AS candidate_count
FROM application a
JOIN candidate c   ON c.id = a.candidate_id
JOIN requisition r ON r.id = a.requisition_id
GROUP BY r.id, r.title, c.gender, a.status;

-- ------------------------------------------------------------
-- 4. TOTAL vs ACTUAL OPEN POSITIONS  (by fiscal year)
-- "Budgeted" openings vs how many are still open/closed.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_positions_by_fy AS
SELECT
    fiscal_year,
    COUNT(*)                                   AS total_requisitions,
    SUM(openings)                              AS total_openings,
    COUNT(*) FILTER (WHERE status = 'open')    AS open_requisitions,
    COUNT(*) FILTER (WHERE status = 'closed')  AS closed_requisitions
FROM requisition
GROUP BY fiscal_year;

-- ------------------------------------------------------------
-- 5. BUDGETED CTC vs OFFERED CTC
-- Where offers landed against the budget set on the requisition.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_budget_vs_offered AS
SELECT
    r.id   AS requisition_id,
    r.title,
    r.fiscal_year,
    r.budgeted_ctc,
    AVG(o.total_ctc) AS avg_offered_ctc,
    MAX(o.total_ctc) AS max_offered_ctc,
    COUNT(o.id) FILTER (WHERE o.status IN ('released','accepted')) AS offers_made
FROM requisition r
LEFT JOIN application a ON a.requisition_id = r.id
LEFT JOIN offer o       ON o.application_id = a.id
GROUP BY r.id, r.title, r.fiscal_year, r.budgeted_ctc;

-- ------------------------------------------------------------
-- 6. BUSINESS UNIT-WISE BIFURCATION
-- Requisitions and hires rolled up by BU and group company.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_bu_summary AS
SELECT
    gc.name AS group_company,
    bu.name AS business_unit,
    COUNT(DISTINCT r.id)                                AS requisitions,
    COUNT(DISTINCT r.id) FILTER (WHERE r.status='open')  AS open_reqs,
    COUNT(a.id) FILTER (WHERE a.status = 'joined')       AS joined_count
FROM business_unit bu
JOIN group_company gc   ON gc.id = bu.company_id
LEFT JOIN requisition r ON r.bu_id = bu.id
LEFT JOIN application a ON a.requisition_id = r.id
GROUP BY gc.name, bu.name;

-- ------------------------------------------------------------
-- 7. ON-ROLL vs OFF-ROLL BIFURCATION
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_roll_split AS
SELECT
    roll_type,
    fiscal_year,
    COUNT(*)        AS requisitions,
    SUM(openings)   AS total_openings
FROM requisition
GROUP BY roll_type, fiscal_year;
