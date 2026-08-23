-- Migration 29: widen cv_repository.source CHECK to add 'application'
-- Guarded: cv_repository is created by the app's auto-migrate, which runs
-- AFTER these init SQL files on a fresh volume. So skip if the table is absent.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'cv_repository'
    ) THEN
        -- drop any existing source CHECK constraint
        DECLARE r RECORD;
        BEGIN
            FOR r IN SELECT conname FROM pg_constraint
                     WHERE conrelid = 'cv_repository'::regclass
                       AND contype = 'c'
                       AND pg_get_constraintdef(oid) ILIKE '%source%'
            LOOP
                EXECUTE 'ALTER TABLE cv_repository DROP CONSTRAINT ' || quote_ident(r.conname);
            END LOOP;
        END;
        ALTER TABLE cv_repository ADD CONSTRAINT cv_repository_source_check
            CHECK (source IN ('bulk_folder','upload','watcher','email','application'));
    END IF;
END $$;