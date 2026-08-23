-- ============================================================
-- Migration 31: Vendor Management (Sourcing Partners)
-- ============================================================
-- Creates vendor, vendor_user, requisition_vendor tables.
-- Extends password_reset_token with account_type so one token
-- table serves staff, vendor, and candidate logins without
-- storing three separate FK columns.  Approach: drop the FK
-- on user_id (making it a generic UUID) and add an
-- account_type discriminator.  This is the least-invasive
-- change — no new token tables, no extra columns elsewhere.
-- Existing rows default to 'staff' automatically.
-- Adds application.source for source-of-hire reporting.
-- ============================================================

-- 1. Vendor (partner / sourcing company) ---------------------
CREATE TABLE IF NOT EXISTS vendor (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name           TEXT NOT NULL,
    contact_email  TEXT,
    contact_phone  TEXT,
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','suspended')),
    created_by     UUID REFERENCES app_user(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Vendor user accounts (mirrors app_user auth columns) ----
-- Use is_active for soft-delete — never hard-delete a vendor
-- user who has submitted CVs (applications reference them via source tag).
CREATE TABLE IF NOT EXISTS vendor_user (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_id      UUID NOT NULL REFERENCES vendor(id) ON DELETE CASCADE,
    full_name      TEXT NOT NULL,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vendor_user_vendor ON vendor_user(vendor_id);
CREATE INDEX IF NOT EXISTS idx_vendor_user_email  ON vendor_user(LOWER(email));

-- 3. Requisition ↔ vendor access mapping ---------------------
CREATE TABLE IF NOT EXISTS requisition_vendor (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requisition_id  UUID NOT NULL REFERENCES requisition(id) ON DELETE CASCADE,
    vendor_id       UUID NOT NULL REFERENCES vendor(id) ON DELETE CASCADE,
    opened_by       UUID REFERENCES app_user(id),
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (requisition_id, vendor_id)
);

CREATE INDEX IF NOT EXISTS idx_req_vendor_req    ON requisition_vendor(requisition_id);
CREATE INDEX IF NOT EXISTS idx_req_vendor_vendor ON requisition_vendor(vendor_id);

-- 4. Extend password_reset_token with account_type -----------
-- Guarded: password_reset_token is created by the app's
-- auto-migrate (not this init file) so it may not exist yet
-- on a fresh-volume first-startup sequence.  The app's own
-- auto-migrate also runs this block idempotently.
DO $$
DECLARE r RECORD;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'password_reset_token'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'password_reset_token'
          AND column_name = 'account_type'
    ) THEN
        -- Drop all FK constraints on the table so user_id can
        -- hold vendor_user.id or candidate_user.id values.
        FOR r IN SELECT conname FROM pg_constraint
                 WHERE conrelid = 'password_reset_token'::regclass
                   AND contype = 'f'
        LOOP
            EXECUTE 'ALTER TABLE password_reset_token DROP CONSTRAINT '
                    || quote_ident(r.conname);
        END LOOP;
        -- Allow NULL so vendor/candidate tokens need not reference app_user.
        ALTER TABLE password_reset_token ALTER COLUMN user_id DROP NOT NULL;
        -- Discriminator: which account type owns this token.
        ALTER TABLE password_reset_token
            ADD COLUMN account_type TEXT NOT NULL DEFAULT 'staff'
            CHECK (account_type IN ('staff','vendor','candidate'));
    END IF;
END $$;

-- 5. application.source — flexible text, no CHECK constraint
-- because vendor submissions use the dynamic 'vendor:<uuid>' pattern.
-- Values in practice: direct | naukri | linkedin | referral |
--                     career_site | vendor:<vendor_id>
ALTER TABLE application ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'direct';
