-- CAPEX/OPEX classification on the requisition form, settable by any role
-- that can create/edit a requisition (recruiter, hiring_manager, hrbp,
-- ta_manager, admin).
ALTER TABLE requisition
    ADD COLUMN capex_opex TEXT NOT NULL DEFAULT 'na'
        CHECK (capex_opex IN ('capex','opex','na'));
