-- 74_cv_enricher_claim.sql
--
-- Doc-only snapshot of backend/app/main.py's Migration 114 (which is what
-- actually runs against an already-initialized database -- see
-- 73_tenant_scoping_hardening.sql's comment for why both copies exist).
--
-- cv_enricher's in-process loop assumed only one process would ever run it
-- ("pending" was picked with no claim). That assumption breaks once
-- REDIS_URL is set and enrichment runs as an Arq queued job, where two
-- overlapping ticks could pick the same row before either updates it.
-- Adds a claim-then-timestamp state (same pattern as
-- enteri_ai_render_worker's render_claimed_at) so claiming is a
-- compare-and-swap and a crash mid-enrichment self-heals via the grace
-- window in cv_enricher.py's claim query.

ALTER TABLE cv_repository ADD COLUMN IF NOT EXISTS enrich_claimed_at TIMESTAMPTZ;
ALTER TABLE cv_repository DROP CONSTRAINT IF EXISTS cv_repository_enrich_status_check;
ALTER TABLE cv_repository ADD CONSTRAINT cv_repository_enrich_status_check
  CHECK (enrich_status IN ('pending','processing','done','failed'));
