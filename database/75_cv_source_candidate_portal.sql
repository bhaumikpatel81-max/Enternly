-- 75_cv_source_candidate_portal.sql
--
-- Doc-only snapshot of backend/app/main.py's Migration 115 (which is what
-- actually runs against an already-initialized database -- see
-- 73_tenant_scoping_hardening.sql's comment for why both copies exist).
--
-- routers/candidate_portal_api.py's portal_update_resume has always
-- inserted into cv_repository with source='candidate_portal', but the
-- cv_repository_source_check CHECK constraint never allowed that value
-- (only bulk_folder/upload/watcher/email/application/email_ingest, per
-- Migrations 36 and 74 below) -- every candidate resume re-upload via the
-- portal has always failed the INSERT. candidate_portal is the correct,
-- descriptive source value; the constraint was what was wrong, so this
-- widens it rather than changing the INSERT.
--
-- Same dynamic-lookup-then-recreate pattern as Migrations 36 and 74 (drop
-- whatever constraint on cv_repository currently mentions "source" by
-- inspecting pg_constraint, then recreate it under the expected name)
-- rather than a hardcoded DROP CONSTRAINT IF EXISTS <name>, so this still
-- works even if the constraint's actual name ever drifted.

DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'cv_repository'::regclass
               AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%source%'
    LOOP
        EXECUTE 'ALTER TABLE cv_repository DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'cv_repository'::regclass
          AND conname = 'cv_repository_source_check'
    ) THEN
        EXECUTE $sql$
            ALTER TABLE cv_repository ADD CONSTRAINT cv_repository_source_check
            CHECK (source IN ('bulk_folder','upload','watcher','email','application','email_ingest','candidate_portal'))
        $sql$;
    END IF;
END $$;
