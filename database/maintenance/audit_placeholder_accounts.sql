-- Read-only audit: find candidate/vendor/staff rows created with a
-- placeholder/example email domain (see backend/app/services/email_validation.py
-- for the canonical blocklist — keep this list in sync with that file).
-- Run this FIRST and review the output before running cleanup_placeholder_accounts.sql.

\pset border 2

\echo '=== app_user (staff) on placeholder domains ==='
SELECT id, full_name, email, role, is_active, created_at
FROM app_user
WHERE lower(split_part(email, '@', 2)) IN (
    'example.com','example.org','example.net','example.edu',
    'test.com','testing.com','sample.com','samplemail.com',
    'acme.com','acmestaffing.com','acmecorp.com',
    'domain.com','yourcompany.com','company.com','mycompany.com',
    'foo.com','foobar.com','dummy.com','mydomain.com','invalid','localhost','test'
);

\echo '=== candidate rows on placeholder domains ==='
SELECT id, full_name, email, source, created_at
FROM candidate
WHERE lower(split_part(email, '@', 2)) IN (
    'example.com','example.org','example.net','example.edu',
    'test.com','testing.com','sample.com','samplemail.com',
    'acme.com','acmestaffing.com','acmecorp.com',
    'domain.com','yourcompany.com','company.com','mycompany.com',
    'foo.com','foobar.com','dummy.com','mydomain.com','invalid','localhost','test'
);

\echo '=== candidate_user logins tied to those candidates ==='
SELECT cu.id, cu.candidate_id, cu.email, cu.created_at
FROM candidate_user cu
WHERE lower(split_part(cu.email, '@', 2)) IN (
    'example.com','example.org','example.net','example.edu',
    'test.com','testing.com','sample.com','samplemail.com',
    'acme.com','acmestaffing.com','acmecorp.com',
    'domain.com','yourcompany.com','company.com','mycompany.com',
    'foo.com','foobar.com','dummy.com','mydomain.com','invalid','localhost','test'
);

\echo '=== vendor_user logins on placeholder domains ==='
SELECT vu.id, vu.vendor_id, v.name AS vendor_name, vu.full_name, vu.email, vu.created_at
FROM vendor_user vu
JOIN vendor v ON v.id = vu.vendor_id
WHERE lower(split_part(vu.email, '@', 2)) IN (
    'example.com','example.org','example.net','example.edu',
    'test.com','testing.com','sample.com','samplemail.com',
    'acme.com','acmestaffing.com','acmecorp.com',
    'domain.com','yourcompany.com','company.com','mycompany.com',
    'foo.com','foobar.com','dummy.com','mydomain.com','invalid','localhost','test'
);

\echo '=== applications belonging to those fake candidates ==='
SELECT a.id, a.candidate_id, a.requisition_id, a.status, a.applied_at
FROM application a
JOIN candidate c ON c.id = a.candidate_id
WHERE lower(split_part(c.email, '@', 2)) IN (
    'example.com','example.org','example.net','example.edu',
    'test.com','testing.com','sample.com','samplemail.com',
    'acme.com','acmestaffing.com','acmecorp.com',
    'domain.com','yourcompany.com','company.com','mycompany.com',
    'foo.com','foobar.com','dummy.com','mydomain.com','invalid','localhost','test'
);

\echo '=== orphaned password_reset_token rows for the fake app_user/candidate_user/vendor_user ids above ==='
SELECT prt.*
FROM password_reset_token prt
WHERE prt.user_id IN (
    SELECT id FROM app_user WHERE lower(split_part(email,'@',2)) IN (
        'example.com','example.org','example.net','example.edu','test.com','testing.com',
        'sample.com','samplemail.com','acme.com','acmestaffing.com','acmecorp.com',
        'domain.com','yourcompany.com','company.com','mycompany.com','foo.com','foobar.com',
        'dummy.com','mydomain.com','invalid','localhost','test')
    UNION ALL
    SELECT id FROM candidate_user WHERE lower(split_part(email,'@',2)) IN (
        'example.com','example.org','example.net','example.edu','test.com','testing.com',
        'sample.com','samplemail.com','acme.com','acmestaffing.com','acmecorp.com',
        'domain.com','yourcompany.com','company.com','mycompany.com','foo.com','foobar.com',
        'dummy.com','mydomain.com','invalid','localhost','test')
    UNION ALL
    SELECT id FROM vendor_user WHERE lower(split_part(email,'@',2)) IN (
        'example.com','example.org','example.net','example.edu','test.com','testing.com',
        'sample.com','samplemail.com','acme.com','acmestaffing.com','acmecorp.com',
        'domain.com','yourcompany.com','company.com','mycompany.com','foo.com','foobar.com',
        'dummy.com','mydomain.com','invalid','localhost','test')
);
