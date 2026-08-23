-- ============================================================
-- Migration 25: Offer Approvals — per-requisition approval chains,
--               extended offer table, and step-level audit log.
-- Idempotent: safe to re-run (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
-- ============================================================

-- ── 1. Extend offer table with approval + Darwinbox handoff fields ────────────

ALTER TABLE offer ADD COLUMN IF NOT EXISTS bonus_ctc      NUMERIC;
ALTER TABLE offer ADD COLUMN IF NOT EXISTS designation    TEXT;
ALTER TABLE offer ADD COLUMN IF NOT EXISTS joining_date   DATE;
ALTER TABLE offer ADD COLUMN IF NOT EXISTS notes          TEXT;
ALTER TABLE offer ADD COLUMN IF NOT EXISTS revise_note    TEXT;
ALTER TABLE offer ADD COLUMN IF NOT EXISTS darwin_ref     TEXT;
ALTER TABLE offer ADD COLUMN IF NOT EXISTS current_step   INT  NOT NULL DEFAULT 1;
ALTER TABLE offer ADD COLUMN IF NOT EXISTS submitted_by   UUID REFERENCES app_user(id);
ALTER TABLE offer ADD COLUMN IF NOT EXISTS submitted_at   TIMESTAMPTZ;
ALTER TABLE offer ADD COLUMN IF NOT EXISTS updated_at     TIMESTAMPTZ;

-- ── 2. Widen offer.status to include workflow states ──────────────────────────
-- Drop the old inline CHECK (name varies by Postgres version) and replace it.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'offer'::regclass
          AND contype  = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE offer DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$;

ALTER TABLE offer ADD CONSTRAINT offer_status_check
    CHECK (status IN (
        'draft', 'pending_approval', 'approved', 'rejected',
        'revising', 'on_hold', 'cancelled', 'sent_to_darwinbox',
        'released', 'accepted', 'declined'
    ));

-- ── 3. Widen application.status to reflect offer hold/cancel states ───────────
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'application'::regclass
          AND contype  = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$;

ALTER TABLE application ADD CONSTRAINT application_status_check
    CHECK (status IN (
        'applied', 'screening', 'screen_passed', 'screen_rejected',
        'interviewing', 'selected', 'rejected',
        'offer_stage', 'offered', 'offer_on_hold', 'offer_cancelled',
        'joined', 'dropped'
    ));

-- ── 4. Per-requisition offer approval chain ───────────────────────────────────
-- Each row is one step: a specific user (not just a role) in a specific order.
-- Different requisitions can have entirely different chains.
CREATE TABLE IF NOT EXISTS req_offer_approver (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requisition_id  UUID NOT NULL REFERENCES requisition(id) ON DELETE CASCADE,
    approver_id     UUID NOT NULL REFERENCES app_user(id),
    sequence        INT  NOT NULL,          -- 1 = first to approve
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (requisition_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_req_offer_approver_req ON req_offer_approver(requisition_id);

-- ── 5. Offer approval step log ────────────────────────────────────────────────
-- One row per step per offer. Created (status=pending) when the offer is made
-- or resubmitted.  Flipped to approved/rejected as the chain progresses.
CREATE TABLE IF NOT EXISTS offer_approval_step (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id    UUID NOT NULL REFERENCES offer(id) ON DELETE CASCADE,
    approver_id UUID NOT NULL REFERENCES app_user(id),
    sequence    INT  NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','rejected','skipped')),
    notes       TEXT,
    acted_at    TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_offer_step_offer    ON offer_approval_step(offer_id);
CREATE INDEX IF NOT EXISTS idx_offer_step_approver ON offer_approval_step(approver_id);
