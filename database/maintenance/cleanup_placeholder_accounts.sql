-- Removes candidate/vendor-login/staff rows created with a placeholder/
-- example email domain (see backend/app/services/email_validation.py for
-- the canonical blocklist). Run audit_placeholder_accounts.sql FIRST and confirm
-- the rows it lists are exactly the test data you expect to remove.
--
-- IMPORTANT: run this interactively (psql), not piped through `psql -f`
-- unattended. Read each RETURNING output, and only run COMMIT at the very
-- end if everything looks right. Type ROLLBACK instead to abort safely.
--
--   psql -h 127.0.0.1 -p 2433 -U postgres -d oneclickhire
--   \i database/maintenance/cleanup_placeholder_accounts.sql   -- runs up to COMMIT prompt
--
-- This does NOT delete vendor companies themselves — only vendor_user
-- logins on a fake domain — since the vendor entity may have other,
-- legitimate contacts.

BEGIN;

-- Applications belonging to fake candidates (must go before the candidate
-- row itself; interview/application_stage_history cascade off application).
DELETE FROM application a
USING candidate c
WHERE a.candidate_id = c.id
  AND lower(split_part(c.email, '@', 2)) IN (
      'example.com','example.org','example.net','example.edu',
      'test.com','testing.com','sample.com','samplemail.com',
      'acme.com','acmestaffing.com','acmecorp.com',
      'domain.com','yourcompany.com','company.com','mycompany.com',
      'foo.com','foobar.com','dummy.com','mydomain.com','invalid','localhost','test'
  )
RETURNING a.id, a.candidate_id, a.requisition_id;

-- candidate_user cascades automatically via ON DELETE CASCADE when the
-- candidate row is removed.
DELETE FROM candidate c
WHERE lower(split_part(c.email, '@', 2)) IN (
    'example.com','example.org','example.net','example.edu',
    'test.com','testing.com','sample.com','samplemail.com',
    'acme.com','acmestaffing.com','acmecorp.com',
    'domain.com','yourcompany.com','company.com','mycompany.com',
    'foo.com','foobar.com','dummy.com','mydomain.com','invalid','localhost','test'
)
RETURNING c.id, c.full_name, c.email;

-- vendor_user logins on a fake domain (vendor company itself is untouched).
DELETE FROM vendor_user vu
WHERE lower(split_part(vu.email, '@', 2)) IN (
    'example.com','example.org','example.net','example.edu',
    'test.com','testing.com','sample.com','samplemail.com',
    'acme.com','acmestaffing.com','acmecorp.com',
    'domain.com','yourcompany.com','company.com','mycompany.com',
    'foo.com','foobar.com','dummy.com','mydomain.com','invalid','localhost','test'
)
RETURNING vu.id, vu.vendor_id, vu.full_name, vu.email;

-- Fake staff accounts, if any.
DELETE FROM app_user u
WHERE lower(split_part(u.email, '@', 2)) IN (
    'example.com','example.org','example.net','example.edu',
    'test.com','testing.com','sample.com','samplemail.com',
    'acme.com','acmestaffing.com','acmecorp.com',
    'domain.com','yourcompany.com','company.com','mycompany.com',
    'foo.com','foobar.com','dummy.com','mydomain.com','invalid','localhost','test'
)
RETURNING u.id, u.full_name, u.email;

-- password_reset_token has no enforced FK (it's polymorphic across staff/
-- vendor/candidate tables), so tokens for the rows above won't cascade —
-- clean up anything now orphaned (user_id no longer exists in any table).
DELETE FROM password_reset_token prt
WHERE prt.user_id IS NOT NULL
  AND prt.user_id NOT IN (SELECT id FROM app_user)
  AND prt.user_id NOT IN (SELECT id FROM candidate_user)
  AND prt.user_id NOT IN (SELECT id FROM vendor_user)
RETURNING prt.id, prt.user_id, prt.account_type, prt.purpose;

-- Review the RETURNING output above for every statement. If it matches
-- exactly what audit_placeholder_accounts.sql showed you, run:
--   COMMIT;
-- Otherwise:
--   ROLLBACK;
