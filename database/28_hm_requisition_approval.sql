-- Migration 28: HM Requisition Approval Workflow (2026-06)
-- Adds approval lifecycle columns to the requisition table.
-- Existing rows default to 'approved' — zero behaviour change for them.

ALTER TABLE requisition ADD COLUMN IF NOT EXISTS approval_status TEXT DEFAULT 'approved';
ALTER TABLE requisition ADD COLUMN IF NOT EXISTS created_by_role TEXT;

-- Add CHECK constraint only if it does not already exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'requisition'::regclass
          AND conname  = 'requisition_approval_status_check'
    ) THEN
        EXECUTE $sql$
            ALTER TABLE requisition ADD CONSTRAINT requisition_approval_status_check
            CHECK (approval_status IN ('approved','pending_ta_approval','rejected'))
        $sql$;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_req_approval_status ON requisition (approval_status);
