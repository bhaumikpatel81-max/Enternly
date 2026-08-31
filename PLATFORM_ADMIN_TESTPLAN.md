# Platform Admin — Test Plan

Manual test plan (no automated test suite exists in this repo). Run against
a local dev stack (`docker-compose.dev.yml` or however this repo is
normally run) with a Postgres instance the auto-migrations can run against.

## 0. Regression guard (do this first)

- [ ] `grep -c "^@router\." backend/app/routers/platform_admin_api.py` and confirm every one of those routes is registered on the router built with `dependencies=[Depends(require_platform_admin)]` (`router = APIRouter(prefix="/api/platform", ...)` — one gate, applied once, at construction; no per-route opt-out exists in this file).
- [ ] `grep -rn '"/api/platform' backend/app/main.py backend/app/routers/*.py` and confirm the only two matches are `platform_admin_api.py`'s router prefix (gated) and `platform_auth_api.py`'s router prefix (deliberately ungated — it's the login route).
- [ ] Existing tenant-admin flows still work: log in as a pre-existing seeded `admin`/`company_admin` account, hit `/api/admin/users`, `/api/admin/role-labels`, `/api/admin/settings` — all should behave exactly as before Step 2/commit 8's flag migration.
- [ ] Existing recruiter/HM flows unaffected: log in as a recruiter, confirm `/api/admin/my-module-access` still returns the expected per-recruiter grants (now additionally AND-ed with the tenant-level toggle from Feature D — should be unaffected since new tenants default all-enabled).
- [ ] Public candidate/vendor/proctoring flows unaffected: start (or resume) a real Enteri AI interview via `/interview.html`'s invite-token flow, and confirm proctoring media-chunk/heartbeat calls still succeed — these hit `enteri_ai_api.py`/`proctoring_api.py`, which now carry `require_tenant_module` at the router level; it must remain a no-op for these token-authenticated, non-staff requests (see PLATFORM_ADMIN_MAPPING.md §4 for why this is safe by design, not just by luck).

## 1. Seed a platform superadmin

The existing Enternstech seed tenant (`00000000-0000-0000-0000-000000000001`) has its pre-existing `admin`/`platform_admin` accounts backfilled onto `is_platform_superadmin = TRUE` by Migration 100 automatically on boot — no manual step needed for an existing dev DB. To mint a brand-new one instead:

```sql
-- only if you need a fresh account rather than using the backfilled seed admin
UPDATE app_user SET is_platform_superadmin = TRUE WHERE email = 'you@enternstech.example';
```

- [ ] Log in at `/platform-login` with that account. Confirm redirect to `/platform-admin` and the left nav renders all 10 items.
- [ ] Log in at `/platform-login` with an account that does **not** have `is_platform_superadmin` — confirm a generic 401, not a distinguishable error.

## 2. Create a tenant, verify `ET_0001`-style code

- [ ] Companies → "New company" → fill in a Company-type tenant + first admin details → Create.
- [ ] Confirm the returned `tenant_code` follows `ET_NNNN`, sequential from whatever the highest existing code is (the seed tenant is backfilled to `ET_0001`, so the first new one should be `ET_0002` on a fresh dev DB).
- [ ] Confirm the invite email path fires (check server logs if `EMAIL_REAL_SEND_ENABLED` isn't set — it should log/stub rather than error) and that the new admin can complete `/set-password` and log into `/login` normally, landing in `index.html` with the full `admin`-role nav (not the recruiter fallback — this is why company admins are created with `role='admin'`, see mapping doc §5b).
- [ ] Deliberately trigger a failed admin-insert (e.g. reuse an existing email as the admin email) and confirm **no tenant row was left behind** (`SELECT * FROM tenant WHERE tenant_code = '<the code that would have been allocated>'` returns nothing) — the transaction rolled back atomically.

## 3. Toggle a module — verify live-constraint rejection

- [ ] Company detail → Modules tab → toggle any module off, confirm it persists on reload.
- [ ] Confirm the corresponding screen in `index.html` for a user of that tenant now shows the restricted-access card (for one of the 7 `DELEGABLE_MODULES` keys) or a 403 from the module's own router (for one of the 9 new keys — e.g. try `GET /api/kpi/dashboard` as a staff user of that tenant while `kpi_dashboard` is disabled).
- [ ] Assign a restrictive plan to the test tenant directly via SQL: `UPDATE subscription_plan_config SET allowed_modules_json = '["vendors"]'::jsonb WHERE plan_name = 'standard';` then attempt to enable a module not in that list via the Modules tab — confirm 400.
- [ ] Confirm a public candidate/proctoring/campus-resume-upload endpoint still works for that same tenant regardless of module state (see §0's public-flow check) — the outer gate must never have touched these.

## 4. Run a subscription — verify auto-disable

- [ ] Companies → detail → Subscription → assign the restrictive plan from step 3 to a tenant that currently has an out-of-plan module enabled.
- [ ] Confirm that module flips to disabled automatically (`GET /api/platform/tenants/{id}/modules`) and an `activity_log` row with `action='auto_disabled_by_plan_change'` was written.
- [ ] Subscriptions list → "Send reminder" on a tenant with an active company admin → confirm the endpoint returns the admin's email and (per the email-send gate) either a real send or a clear stub/log.

## 5. Add a College tenant + placement officer

- [ ] Create a tenant with `tenant_type: College`.
- [ ] Company detail → confirm the "+ Add placement officer" button only appears for College tenants.
- [ ] Add a placement officer — confirm `role='placement_officer'` in the DB, confirm the same action attempted against a Company-type tenant returns 400.

## 6. Impersonate — verify expiry, banner, no session clobber

- [ ] All Users → pick an active, non-superadmin user → Impersonate → confirm a new tab opens at `/` with the impersonation banner visible and the correct name.
- [ ] In that new tab, confirm `sessionStorage['enternly_impersonation_token']` is set and `localStorage['enternly_token']` is **untouched** (check devtools, or simply confirm any other already-open ordinary index.html tab for a different user is unaffected).
- [ ] Confirm the impersonation token, if used against any `/api/platform/*` route, is rejected (`require_platform_admin` checks `isImpersonation` first).
- [ ] Wait past 15 minutes (or manually craft/verify the `exp` claim) and confirm the session dies and forces a re-auth.
- [ ] Click "Return to platform admin" — confirm it lands back in a working `/platform-admin` console (using the original, still-valid `enternly_platform_token`), not a logged-out state.
- [ ] Attempt to impersonate a platform superadmin or an inactive user — confirm both are rejected with 400.

## 7. Verify cross-tenant isolation

- [ ] As a company admin of Tenant A, confirm every existing `/api/admin/*`, `/api/tickets`, `/api/activity-log/*` endpoint still returns only Tenant A's data (unchanged from before this project).
- [ ] As the platform superadmin, confirm `/api/platform/tenants`, `/api/platform/users`, `/api/platform/tickets`, `/api/platform/audit` all deliberately span every tenant — this is the one place that's supposed to.
- [ ] Suspend Tenant A (`PATCH /api/platform/tenants/{id}/status {"status":"suspended"}`) and confirm a Tenant A user gets a generic 401 on `/api/auth/login`, and an already-logged-in Tenant A user's next request also 401s (`_refresh_staff_claims`'s live tenant check).
- [ ] Soft-delete a tenant and confirm the same login-blocking behavior, and confirm `GET /api/platform/tenants` no longer lists it (while the row still exists in the DB, `is_deleted=TRUE`).
- [ ] Set a tenant's `subscription_end_date` to yesterday with `grace_period_days=0` and confirm login is blocked; set `grace_period_days=30` instead and confirm login still succeeds (grace = full access, per decision 2).
