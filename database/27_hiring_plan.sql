-- Migration 32: Hiring Plan module
-- Budget sheet rows: fiscal demand planning linked to requisitions.

CREATE TABLE IF NOT EXISTS hiring_plan_rows (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fiscal_year             TEXT,
    quarter                 TEXT,
    company_entity          TEXT,
    finance_onboarding_date DATE,
    planned_onboarding_date DATE,
    requisition_id          UUID REFERENCES requisition(id) ON DELETE SET NULL,
    link_status             TEXT NOT NULL DEFAULT 'unlinked'
                            CHECK (link_status IN ('unlinked','suggested','confirmed')),
    role_name               TEXT,
    bu                      TEXT,
    function                TEXT,
    sub_bu                  TEXT,
    project_name            TEXT,
    employment_type         TEXT,
    billable                TEXT,
    sow_received            TEXT,
    capex_opex              TEXT,
    capex_opex_on_track     TEXT,
    on_off_roll             TEXT,
    headcount               INT  NOT NULL DEFAULT 1,
    priority                TEXT,
    band                    TEXT,
    experience              TEXT,
    market_salary_range     TEXT,
    location                TEXT,
    budgeted_fixed          NUMERIC NOT NULL DEFAULT 0,
    budgeted_variable       NUMERIC NOT NULL DEFAULT 0,
    asset                   TEXT,
    salary_budgeted_till    DATE,
    hiring_status           TEXT NOT NULL DEFAULT 'Open Position'
                            CHECK (hiring_status IN (
                                'Open Position','Offered','Joined','Hold','Internal Employee'
                            )),
    replacement_for         TEXT,
    aipl_code               TEXT,
    employee_name           TEXT,
    offered_fixed           NUMERIC NOT NULL DEFAULT 0,
    offered_variable        NUMERIC NOT NULL DEFAULT 0,
    ta_owner                TEXT,
    source_of_hire          TEXT,
    candidate_email         TEXT,
    offer_date              DATE,
    tentative_doj           DATE,
    remarks                 TEXT,
    created_by              UUID REFERENCES app_user(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_hp_rows_fy     ON hiring_plan_rows(fiscal_year);
CREATE INDEX IF NOT EXISTS idx_hp_rows_bu     ON hiring_plan_rows(bu);
CREATE INDEX IF NOT EXISTS idx_hp_rows_req    ON hiring_plan_rows(requisition_id);
CREATE INDEX IF NOT EXISTS idx_hp_rows_status ON hiring_plan_rows(hiring_status);
