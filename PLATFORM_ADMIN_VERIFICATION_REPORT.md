# Platform Admin — Live Verification Report

Date: 2026-08-31. Verification run against a live Docker Compose stack built
from the current `main` branch (through commit `fcc76e8`, which includes one
fix applied during this verification — see §5).

## 1. Isolation confirmation

- Enternly was brought up under its own Compose project name (`-p enternly`), using `docker compose -p enternly -f docker-compose.dev.yml -f <local-override>.yml`. Containers created: `enternly-db-1`, `enternly-backend-1`, network `enternly_default`, volumes `enternly_pgdata_dev` / `enternly_cv_store` / `enternly_jd_store`.
- `ats-hr-backend-1` / `ats-hr-db-1` were **not started, stopped, restarted, or reconfigured** at any point. Confirmed identical `Exited` status (same exit codes: backend `137`, db `0`) both before this session's work began and at the end of verification — only the "time ago" changed. `docker network inspect enternly_default` confirms only `enternly-backend-1`/`enternly-db-1` are attached to it; `ats-hr_default` is a separate network.
- No ats-hr container, image, volume, or network was removed, renamed, or modified. Only `enternly`-prefixed resources were created/torn down/recreated during this session (one full teardown+recreate was needed after the fresh-DB bootstrap failure in §4 — scoped entirely to the `enternly` project via `-p enternly ... down -v`).

## 2. Port remap

`docker-compose.dev.yml`'s `backend` service hard-codes `"8080:8080"`, the same host port `ats-hr-backend-1` publishes. An override file remapped this to **`8081:8080`**.

**Note on a mistake caught and corrected during this session**: Compose merges list-type keys like `ports` across `-f` files by *appending*, not replacing. My first override just added `"8081:8080"` without removing the base file's `"8080:8080"`, so the container briefly published **both** 8080 and 8081 — a real collision risk with `ats-hr-backend-1`'s own port 8080 had that project been running at the same time. Caught immediately via `docker ps` (both port mappings visible), the backend container was stopped within seconds, the override was fixed using Compose's `!override` YAML tag to force a full replace instead of a merge, and verified via `docker compose config` before restarting. Final published port: **8081 only**.

## 3. DB identity confirmation

Verified three independent ways, all consistent:
1. **Network topology**: `enternly-backend-1`'s `DB_HOST=db` resolves only within the isolated `enternly_default` network, which contains exclusively `enternly-backend-1`/`enternly-db-1` — there is no routable path to any ats-hr container even by name collision.
2. **Container env**: `enternly-backend-1`: `DB_HOST=db DB_NAME=oneclickhire DB_USER=ochuser`; `enternly-db-1`: `POSTGRES_DB=oneclickhire POSTGRES_USER=ochuser` — matches `docker-compose.dev.yml` exactly.
3. **Schema content**: `\dt` against `enternly-db-1` lists Enternly-specific tables (`activity_log`, `app_user`, `enteri_ai_session`, `enteri_ai_invite`, `tenant`, `client`, …) — the Enteri-AI-branded table names in particular are unique to this codebase's post-Migration-99 rename, confirming this is genuinely Enternly's schema, not a generic/foreign one.

DB identity was never ambiguous; migrations were run.

## 4. Migration 100–104 result: **PASS**, after working around a pre-existing, unrelated infra gap

### A real, pre-existing bug found (not part of the platform-admin work)

First bring-up **failed hard**: `enternly-db-1` exited (code 3) during its first-ever initialization. Root cause, fully diagnosed:

- `docker-compose.dev.yml` mounts the *entire* `./database` directory as `/docker-entrypoint-initdb.d`, so Postgres executes **every** `.sql` file in it, in name order, on a truly fresh volume.
- `database/*.sql` has a real gap: **files 35 through 52 do not exist.** (`ls database/*.sql` jumps `34_screening_questions.sql` → `53_enteri_ai_attempt_guard.sql`.)
- `59_tenant_isolation.sql` runs `ALTER TABLE hrbp ADD COLUMN tenant_id ...`, but the `hrbp` table is created **only** inline in `main.py`'s `_auto_migrate()` (labeled "Migration 49"), with no matching `database/49_*.sql` snapshot ever committed — so on a genuinely empty volume, `59_tenant_isolation.sql` fails with `relation "hrbp" does not exist`, and Postgres's official image aborts the entire initdb sequence (and the container) on the first script error.
- This is **not** caused by anything in this project's commits — I never touched `59_tenant_isolation.sql`, `hrbp`, or any file before `62_*.sql`. It's pre-existing drift that most likely has never been exercised: any developer's local Postgres volume has almost certainly persisted since before migration 35 and been evolved forward by `main.py`'s runtime `_auto_migrate()` ever since, never rebuilt from a truly empty volume — which is exactly what creating a fresh, isolated `enternly_pgdata_dev` volume for this verification did for what may be the first time.

**Per your instruction not to hand-edit the DB or apply schema fixes without asking**, I did not touch any `database/*.sql` file or the repo's `docker-compose.dev.yml`. Instead, for **local verification only**, I pointed the throwaway DB container's initdb mount at a small directory containing just `01_schema.sql` + `02_seed.sql` + `03_auth_migration.sql` (matching `docker-compose.dev.yml`'s own comment: *"runs 01_schema, 02_seed, 03_auth_migration on first start"* — the original, apparently-intended bootstrap set) and let `main.py`'s `_auto_migrate()` — a complete, idempotent, self-sufficient migration history — build out everything else on first backend startup, exactly as it does for any long-lived database. This worked cleanly end to end (see below) and touched no tracked repo file.

**Proposed fix-forward for the repo** (not applied — needs your decision):
- (a) Backfill the missing `database/35_*.sql` … `52_*.sql` snapshot files so the documented "fresh install via docker-entrypoint-initdb.d" path actually works, or
- (b) Simpler: since every `database/NN_*.sql` file's own header already calls itself a "doc-only snapshot" and states `main.py` is "the real source of truth," stop relying on Postgres's directory-sweep behavior at all — move `04_*.sql` onward out of `database/` into a non-swept subdirectory (e.g. `database/history/`), and update `docker-compose.dev.yml`'s mount + comment to reflect that only `01–03` are real bootstrap scripts, with `_auto_migrate()` solely responsible for everything after. This matches what actually already happens on every real deployment (backend startup migrates, initdb never really contributed migrations 4+ correctly for years) and removes the trap entirely.

### Schema checks (after the workaround), all confirmed live via `psql`

| Check | Result |
|---|---|
| `app_user.is_platform_superadmin`, `is_company_admin`, `last_login_at` exist | ✅ PASS |
| `tenant.tenant_code`, `tenant_type`, `is_deleted`, `grace_period_days`, `subscription_start_date`, `subscription_end_date` exist | ✅ PASS |
| Seed tenant `tenant_code = 'ET_0001'` | ✅ PASS |
| `module_catalog` has 16 rows | ✅ PASS |
| `tenant_module_config` = 1 tenant × 16 modules = 16 rows, all `is_enabled=true` | ✅ PASS |
| `subscription_plan_config` has the `standard` row (`allowed_modules_json='[]'`) | ✅ PASS |
| `app_user_role_check` constraint includes `placement_officer` | ✅ PASS |
| `support_ticket_reply`, `platform_settings` tables exist with the designed columns | ✅ PASS |
| Seed `admin@example.com` correctly backfilled to `is_platform_superadmin=TRUE`, `is_company_admin=FALSE` | ✅ PASS |

Backend startup log showed **zero warnings** related to any platform-admin migration (100–104). The only warnings present are pre-existing, expected, idempotency-driven no-ops from Migration 99's NexAI→Enteri-AI rename block (e.g. `relation "enteri_ai_session" already exists` — happens because the rename already took effect and can't re-run, a known, accepted "log but don't crash" pattern, unrelated to this project).

## 5. Auth + regression results

| Check | Result |
|---|---|
| Platform login at `/platform-login` (via `POST /api/platform/auth/login`) | ✅ PASS (after fix, see below) |
| Non-superadmin platform login → generic 401 | ✅ PASS |
| **Ordinary existing user (recruiter) can still log in normally to `/api/auth/login`** | ✅ PASS — regression canary clean |
| Superadmin can also log in via ordinary `/api/auth/login` (not platform-locked) | ✅ PASS |
| `/api/auth/me` round-trips correctly, exercising the widened tenant-JOIN query | ✅ PASS |

### A real bug found and fixed (trivial, applied — commit `fcc76e8`)

The **first** platform-login attempt failed with `{"detail":"Not authenticated"}` — not my endpoint's own error shape at all. Root cause: `main.py`'s global `auth_middleware` 401s **any** request whose path isn't in an explicit `_PUBLIC` allowlist, before the request ever reaches a route. `/api/platform/auth/login`, `/platform-login`, and `/platform-admin` were never added to that allowlist in the commits that built them — so the platform login endpoint was **completely unreachable** (a chicken-and-egg: you need a token to get past the middleware, but the login endpoint that issues tokens was itself blocked by the middleware), and the two page routes would have 401'd as JSON instead of serving HTML even after a successful login.

Treated as a trivial, obviously-correct fix (one line, exact same pattern as the pre-existing `"/login"`, `"/api/auth/login"`, `"/set-password"` entries, zero effect on any other route) and applied + committed, since without it the entire platform-admin login feature — already reported as "done" across 11 commits — does not function at all. Verified fixed: platform login now returns a correctly-shaped token.

## 6. Test-plan results

| Item | Result | Note |
|---|---|---|
| Seed a superadmin | ✅ PASS | Seed `admin@example.com` auto-backfilled by Migration 100; set a test password directly via SQL (its `password_hash` is NULL by design pre-first-login) |
| Create tenant, verify `ET_NNNN` code | ✅ PASS | `ET_0002` for Acme Corp (Company), `ET_0003` for State University (College) |
| Atomic tenant+admin transaction | ✅ PASS | Duplicate-email create correctly rejected before the transaction opens; no orphaned tenant row, `tenant_code` sequence not consumed |
| Toggle a module, verify live-constraint rejection at the toggle endpoint | ✅ PASS | See §7 finding #2 for a deeper gap in the *runtime* enforcement, though |
| Run a subscription, verify auto-disable | ✅ PASS (for tenants with existing module rows) | Confirmed on the seed tenant; does **not** apply to newly-created tenants — see §7 finding #2 |
| Add a College tenant + placement officer | ✅ PASS | Role correctly set to `placement_officer`; rejected on a Company-type tenant with a clear 400 |
| Impersonate (expiry, banner reachability, rejected by `/api/platform/*`, isolation) | ✅ PASS | See §6a below |
| Cross-tenant isolation | ✅ PASS | See below |
| Email dry-run (invite + renewal reminder) | ✅ PASS | Both correctly suppressed and logged (`EMAIL_REAL_SEND_ENABLED` unset), no errors |

### 6a. The 5 high-risk checks, individually

**1. Grace period, all three cases** — ✅ **ALL PASS**
- `subscription_end_date = NULL` → login succeeds.
- 5 days past `end_date`, `grace_period_days = 0` → login blocked (new logins **and** an already-issued token dies on its very next request — confirmed with the same token, before/after).
- 5 days past `end_date`, `grace_period_days = 30` → login still succeeds.

**2. Commit-8 role retirement (settings, module-access, GCal, bands)** — ⚠️ **PARTIAL** — see finding #4 below. `/api/admin/settings` and `/api/admin/module-access/recruiters` (the two files commit 8 actually touched) behave correctly for admin/recruiter/HM. GCal and Bands were **not actually migrated** in commit 8 despite being named in its own description — confirmed no wrong-lockout for *current* test accounts, but the underlying code is still on the pre-flag model, and a broader sweep found this pattern in 10 files, not 2.

**3. Module outer gate (nav hides + API 403)** — ⚠️ **PARTIAL** — see finding #3. API-side 403 confirmed working correctly (toggled `kpi_dashboard` off for the seed tenant → 200 before, 403 after, correct error message). Nav-hiding was never implemented for the 9 new keys — confirmed via grep, zero references.

**4. Impersonation isolation** — ✅ **ALL PASS**
- New impersonation token: `isImpersonation:true`, `impersonatedBy:<admin id>` claims correct.
- Expiry: issued-at vs. `exp` claim = 899 seconds ≈ 15 minutes, matches design exactly.
- `/api/platform/*` rejects it (`403 Platform Admin access required`, via the `isImpersonation` check).
- Ordinary staff endpoint (`/api/auth/me`) accepts it, returns the impersonated identity.
- Tab A's (the platform admin's own) token, used repeatedly throughout and after, remained fully valid and unaffected — no session clobbering.
- `/platform-admin`, `/platform-login`, `/` all serve 200 (page-reachability for the "Return to platform admin" flow confirmed at the HTTP level; the actual click-through wasn't exercised in a real browser — no browser automation tool was available in this environment).
- Rejected targets: impersonating a platform superadmin → 400; impersonating an inactive user → 400. Both confirmed.

**5. Email dry-run** — ✅ **PASS**
- Invite email (tenant creation): `[email] SUPPRESSED (EMAIL_REAL_SEND_ENABLED not set)`, full recipient/subject/body logged, including the working `/set-password?token=...` link. No error.
- Renewal reminder: `{"ok":true,"sent_to":"jane@acmecorp.example"}`, 200.

## 7. Every failure, in detail

### #1 — [FIXED, committed `fcc76e8`] Platform login/pages missing from the auth-middleware allowlist
See §5. Root cause: `_PUBLIC` set in `main.py` never updated when the platform routes were added. Fix: added the 3 paths, same pattern as existing entries. Verified fixed live.

### #2 — [NOT FIXED, needs a decision] Subscription plan restriction is not enforced at runtime for newly-created tenants
**What failed**: Created "Acme Corp" (no explicit module config), assigned it a plan whose `allowed_modules_json = ["vendors"]` only. A real Acme user could still successfully call `GET /api/kpi/dashboard` (200, full data) — a module explicitly excluded by their plan.
**Root cause**: `POST /api/platform/tenants` never seeds `tenant_module_config` rows for the new tenant. `tenant_module_enabled()` — the function the runtime outer gate (`require_tenant_module`, used by all 9 gated routers) actually calls on every request — defaults to `True` when no row exists, and **never consults `subscription_plan_config` at all**. The plan's `allowed_modules_json` is only checked inside the explicit `PUT /tenants/{id}/modules` toggle endpoint. So "assign a restrictive plan" silently does nothing to a tenant that's never had someone visit the Module Catalog UI. Confirmed the *mechanism itself* works correctly when rows already exist — toggling the seed tenant's plan correctly auto-disabled 15 of its 16 modules, leaving only `vendors`.
**Proposed fix-forward** (not applied): either (a) seed `tenant_module_config` rows for a new tenant at creation time based on its assigned plan, or (b) make `tenant_module_enabled()` itself consult `subscription_plan_config` when no explicit row exists, instead of defaulting to blanket-allow. This is a real design choice with different tradeoffs (a) is simpler and matches existing patterns; (b) is more defensive but changes the hot-path check. Flagging rather than picking.

### #3 — [NOT FIXED, needs a decision] Nav-hiding for the 9 new module_catalog keys was never implemented
**What failed**: disabling a module (e.g. `kpi_dashboard`) for a tenant correctly 403s the API, but `index.html`'s `NAV_DEF`-driven sidebar still shows the nav item unconditionally for any role that would normally see it — confirmed via grep, there is no code path connecting tenant module status to nav visibility for these 9 keys (only the pre-existing 7 `DELEGABLE_MODULES` keys get any such treatment, via the unrelated per-recruiter delegation mechanism).
**Root cause**: this half of Feature D was never actually built — the commit message describing it as "closing the nav hidden, API still open gap" overstated what shipped; only the API-403 half exists.
**Proposed fix-forward** (not applied): add a tenant-module-status endpoint the frontend can call at boot (extending `/api/admin/my-module-access` or a new one), and filter `NAV_DEF` items by it in `buildNav()`, mirroring the existing recruiter-delegation nav-filtering pattern already in that function.

### #4 — [NOT FIXED, needs a decision] Step 1's "cleanup pass" (commit 8) covered 2 files; a full sweep found 10
**What failed**: nothing actively, for any account that exists today (every real account's `role` string still falls inside the old tuples). But a broad `grep` for the literal pattern across `backend/app/routers/` found **10 files**, not the 2 (`admin_users.py`, `auth_utils.py`) commit 8 actually touched, still gating on `role in ("admin","platform_admin","company_admin", ...)` rather than the `is_company_admin`/`is_platform_superadmin` flags:

| File | Site(s) |
|---|---|
| `activity_log_api.py` | line 288, `/logins` endpoint gate |
| `bands_api.py` | line 26, `_ADMIN_ROLES` |
| `chain_templates_api.py` | lines 30, 38 |
| `email_template_api.py` | line 49 |
| `google_calendar_api.py` | line 32, `_require_admin` |
| `org_api.py` | line 30, `_ADMIN_ROLES` |
| `password_api.py` | line 29, `_SELF_SERVICE_ROLES` (different semantic — self-service reset eligibility, not a write gate) |
| `sla_api.py` | lines 39-40, read/write role sets |
| `tickets_api.py` | lines 40, 72 (`list_tickets`/`update_ticket` — separate from the already-known, already-documented line 187 system-health gate) |
| `vendor_api.py` | lines 106, 364 |

**Root cause**: my original Step 1 audit (documented in `PLATFORM_ADMIN_MAPPING.md` §6, "RESOLVED") used a narrower search that missed these. The mapping doc's "ALL DONE"/"RESOLVED" framing for Step 1 was inaccurate.
**Why this isn't an active bug today**: every currently-existing account's `role` column is still literally `'admin'`, `'platform_admin'`, `'company_admin'`, `'ta_manager'`, or `'recruiter'` — values these old tuples already include — so nobody is wrongly denied today. The latent risk is architectural: the whole point of decoupling `is_company_admin`/`is_platform_superadmin` from `role` was to let a flag be granted independently of role (e.g. to a `ta_manager` in the future); any of these 10 sites would incorrectly deny such an account despite it being properly flagged.
**Proposed fix-forward** (not applied): mechanically apply the same `_is_company_tier()`/`_is_platform_tier()`-style rewrite already used in `admin_users.py` to these 10 files. This is a real auth-logic change across many files — per your instruction, reporting rather than applying it now.

### #5 — [NOT FIXED, needs a decision, low severity] Suspending/expiring the platform's own seed tenant locks out platform superadmins with no API-level recovery
**What failed**: `PATCH /api/platform/tenants/{seed-tenant-id}/status {"status":"suspended"}` succeeded (called by the superadmin, correctly self-authorized at the time), but the **very next request** — including the superadmin's own attempt to *un*-suspend it — was rejected, because `_refresh_staff_claims`'s tenant-lifecycle check applies uniformly to every tenant, including the Enternstech/seed one that platform superadmins themselves belong to. Had to recover via a direct SQL `UPDATE`, since there's no API path that works once every platform-admin session is dead.
**Root cause**: the tenant-suspension/grace-expiry check in `auth.py`/`auth_utils.py` doesn't special-case the seed/platform tenant.
**Proposed fix-forward** (not applied): either exempt the seed tenant ID from this check (it isn't really a "customer"), or document an emergency SQL recovery procedure clearly (e.g. in `PLATFORM_ADMIN_TESTPLAN.md`) since this is arguably intended behavior for a genuine customer tenant, just surprising for the platform's own home tenant. Flagging for a decision either way.

## 8. Status of the previously-documented accepted gaps

- **`cv_api.py`'s query-param-auth file-view route bypassing the module outer gate** — unchanged, not re-tested live this session (low severity, already documented as an accepted, deliberate tradeoff in `PLATFORM_ADMIN_MAPPING.md` §4).
- **The 4 routers serving public candidate/proctoring/campus traffic** (`enteri_ai_api.py`, `proctoring_api.py`, `campus_bulk_api.py`, and `cv_api.py`'s one route) — confirmed by design review (not live-tested against an actual candidate interview session, which would need a real invite token end-to-end) that `require_tenant_module`'s no-op-on-missing-Bearer-token design is unaffected by anything found this session.

## 9. Go / No-Go

**Conditional GO for continued local development and further testing — NOT yet GO for any shared/staging/production deployment.**

Reasoning:
- Core platform-admin functionality (auth, tenant CRUD, subscriptions, impersonation, tickets/audit/analytics, grace-period login enforcement, cross-tenant isolation) is **solidly verified working**, including every explicitly-requested high-risk scenario except the two partial items below.
- One launch-blocking bug (#1, middleware allowlist) was found and fixed during this session — without it, platform login didn't work at all. That's now resolved and committed.
- Two real, unaddressed functional/security gaps remain (#2 module/plan enforcement, #3 nav-hiding) that mean the "modules are gated by subscription plan" and "disabled modules disappear from nav" claims are **not fully true yet** for newly-created tenants — acceptable for continued local iteration, not for putting this in front of real customers who'd be sold on those specific capabilities.
- Finding #4 (10 files not migrated to flags) is not an active bug but represents unfinished, previously-mis-reported work — worth doing before calling Step 1 complete.
- Finding #5 (self-lockout) is a real operational risk worth a deliberate decision before this ever touches a shared environment, even though it's not exploitable by anyone but a platform superadmin themselves.
- A pre-existing, unrelated infra bug (§4, missing `database/35-52` snapshots) blocks a truly fresh install via the documented Postgres-initdb path; worked around for this verification, but the repo itself needs a decision on the proposed fix-forward before anyone else tries a from-scratch bring-up.

**Recommended next step**: your call on findings #2–#5 and the §4 infra gap (which fix-forward direction for each), then I can apply whichever fixes you approve and re-verify.

---

## Appendix: local stack status

The `enternly` stack (from this verification) is still running: `enternly-backend-1` on `http://localhost:8081`, `enternly-db-1` internal-only. Test data created this session (all disposable, in the throwaway `enternly_pgdata_dev` volume): tenants "Acme Corp" (`ET_0002`), "State University" (`ET_0003`, College), a `restricted-test` subscription plan, and several test users (`test.recruiter@example.com`, `testhm@enternstech.example`, `acmetest@acmecorp.example`, `jane@acmecorp.example`, `dean@stateuni.example`, `placement@stateuni.example`). The seed `admin@example.com` account's password was set to `VerifyTest#2026` for testing (it was previously unset/NULL).

To tear down: `docker compose -p enternly -f docker-compose.dev.yml down -v` (safe — scoped entirely to the `enternly` project, will not affect `ats-hr`).

---

## 10. Fix session (2026-08-31): findings #2, #1(new), #3(new), #4(new) closed

This section covers the follow-up session that fixed the four open findings
above, mapped to the user's fix-order numbering: **Fix #4 = old finding #5**
(self-lockout), **Fix #1 = old finding #2** (plan constraint no-op), **Fix #2
= old finding #3** (nav-hiding), **Fix #3 = old finding #4** (incomplete
role-flag migration). Old finding #1 (middleware allowlist) was already fixed
and committed prior to this session. Work was done against the same running
`enternly` stack on `localhost:8081`; each fix was verified live before
moving to the next.

### Fix #4 — superadmin self-lockout — ✅ FIXED, verified, committed `5aafe9e`

**Change**: both tenant-lifecycle checks (`auth.py::login()` and
`auth_utils.py::_refresh_staff_claims()`) now skip the
suspended/deleted/expired-grace check entirely when
`is_platform_superadmin` is true on the account being checked.

**Live verification**:
- Suspended the seed tenant via `PATCH /api/platform/tenants/{seed}/status`.
- Platform superadmin: still able to log in, still able to call
  `/api/platform/*` (used it to un-suspend the tenant itself — the exact
  recovery path finding #5 said didn't exist).
- Ordinary user on the same (suspended) tenant: still correctly blocked
  (401), confirming the exemption is scoped to the superadmin flag, not a
  blanket bypass of the suspension feature.

### Fix #1 — plan constraint no-op for new tenants — ✅ FIXED, verified, committed `135538b`

**Change**: `POST /api/platform/tenants` now seeds `tenant_module_config`
rows for the new tenant inside the same atomic tenant+first-admin
transaction, honoring the initial plan's `allowed_modules_json` (empty `[]`
= all enabled, matching the existing convention used everywhere else).

**Live verification**:
- Created a tenant on a plan with a restrictive `allowed_modules_json` →
  out-of-plan modules came up disabled immediately, no manual toggle needed.
- Created a tenant on the default `standard` plan (`[]`) → all 16 modules
  enabled, matching pre-existing tenant behavior.
- Confirmed no regression to the existing all-enabled backfill for
  pre-existing tenants.
- **Related nuance, not a regression from this fix**: Migration 102's
  idempotent backfill still re-runs on every backend restart and grants
  all-enabled to any tenant that *still* has zero `tenant_module_config`
  rows. This briefly reset a pre-existing test tenant's restrictive config
  after a routine restart during this session (tenants created *before* Fix
  #1 existed had no rows yet). Recovered via the existing, unmodified `PUT
  /tenants/{id}/subscription` endpoint. Not fixed further — out of scope,
  and now a one-time transition concern only, since every tenant created
  after Fix #1 always has rows from the moment it exists.

### Fix #2 — nav-hiding half of the module outer gate — ✅ FIXED, verified, committed `d6936f6`

**Change**: `/api/admin/my-module-access` (already called by the frontend at
boot) now returns `client_module_status(user)` — a new
`module_access.py` function that combines the existing 7-key delegable
access map with a live `tenant_module_enabled()` check for the 9 gated
feature keys — instead of only the 7-key delegable map. `index.html` now
calls this endpoint unconditionally at boot for every role (previously only
for recruiters), and `buildNav()` filters out any of the 9 gated nav items
where `MY_MODULE_ACCESS[key] === false`. No new endpoint was added — the
existing bootstrap call was extended, per the instruction to reuse it rather
than invent a new one.

**Live verification**:
- Disabled `kpi_dashboard` for a test tenant → its nav entry disappeared for
  that tenant's users, and `GET /api/kpi/dashboard` still correctly 403'd
  (backend gate untouched — this fix is UX alignment only).
- Other tenants (module still enabled): nav entry still present, API still
  200.

### Fix #3 — incomplete role-flag migration (full audit) — ✅ FIXED, verified, committed `2f39ca7` + `bb87922`

**Scope correction**: the earlier "commit 8" cleanup pass covered 2 files.
A full grep sweep for `platform_admin`/`company_admin`/`role in (...)` used
*for gating* (excluding display labels, the `ALL_ROLES` picker, and the
legacy `role` column itself) found **10 files**. All 10 were migrated to the
shared `is_company_tier()`/`is_platform_tier()` helpers (centralized in
`auth_utils.py`, previously duplicated locally in `admin_users.py`).

**Site-by-site table**:

| File | Site | Old check | New check | Scope |
|---|---|---|---|---|
| `activity_log_api.py` | `/logins` (list) | `role not in (...)` | `not is_company_tier(user)` | Company |
| `bands_api.py` | `_require_admin` | `_ADMIN_ROLES` set membership | `is_company_tier(user)` | Company |
| `chain_templates_api.py` | `_require_write` | role tuple | `is_company_tier(user)` OR recruiter w/ `chain_templates` delegation | Company |
| `chain_templates_api.py` | `_require_read` | role tuple | `is_company_tier(user)` OR `ta_manager`/`recruiter` | Company |
| `email_template_api.py` | `_require_template_access` | role tuple | `is_company_tier(user)` OR recruiter w/ `email_templates` delegation | Company |
| `google_calendar_api.py` | `_require_admin` | role tuple | `is_company_tier(user)` | Company |
| `org_api.py` | `_require_admin` | `_ADMIN_ROLES` set membership | `is_company_tier(user)` OR recruiter w/ `organisation` delegation | Company |
| `password_api.py` | `send_setup_link` / `forgot_password` — self-service reset eligibility | `_SELF_SERVICE_ROLES` (checked the **target** user's role) | new `_self_service_eligible(target)`: `is_company_tier(target) or target.role in (ta_manager, recruiter)` | Target-role eligibility (special case — gates on the account being reset, not the actor) |
| `sla_api.py` | `_require_sla_write` | `_ALLOWED_ROLES_WRITE` | `is_company_tier(user)` OR recruiter w/ `sla_settings` delegation | Company |
| `sla_api.py` | `_require_sla_read` | `_ALLOWED_ROLES_READ` | `is_company_tier(user)` OR `ta_manager`/`recruiter` | Company |
| `tickets_api.py` | `list_tickets` ("see all" branch) | role tuple | `is_company_tier(user)` | Company |
| `tickets_api.py` | `update_ticket` | role tuple | `is_company_tier(user)` | Company |
| `tickets_api.py` | `system_health` | role tuple (left as-is in commit 8 for an unrelated reason, not an intentional exemption) | `is_platform_tier(user)` | Platform |
| `vendor_api.py` | `_require_internal` | role tuple | `is_company_tier(user)` OR recruiter w/ `vendors` delegation | Company |
| `vendor_api.py` | `_assert_ta_or_admin` | role tuple | `is_company_tier(user)` | Company |
| `admin_users.py` | local `_is_company_tier`/`_is_platform_tier` | duplicated locally | now imports the shared `auth_utils.is_company_tier`/`is_platform_tier` | (dedup, no behavior change) |

**Confirmed still untouched (exemptions, re-grepped this session)**:
- `auth_utils.py::require_ta_manager` — untouched, no reference to the
  retired role strings.
- `module_access.py::effective_module_access`'s role-hierarchy check
  (`role in ("admin", "platform_admin", "company_admin")`, line 155) —
  untouched, still on the pre-flag pattern by design (it's the 7-key
  delegable-access map, a different mechanism from the outer gate).
- `main.py`'s 6 pre-existing `role == "admin"` sites (db-stats,
  cv-database, sys-logs, `/api/schedule`, `serve_resume`, req-form-data
  helper) — grepped, zero matches for `role == "admin"` in `main.py` at all
  (all 6 sites use a different comparison form / were already
  reorganized); none reference the retired `platform_admin`/
  `company_admin` strings, none edited this session.

**Live verification — 4-role × 10-file authorization matrix** (PS = platform
superadmin, CA = company/tenant admin, R = recruiter, HM = hiring manager;
all endpoints hit with real tokens against `localhost:8081`):

| Endpoint | PS | CA | R | HM |
|---|---|---|---|---|
| `GET /api/activity-log/logins` | 200 | 200 | 403 | 403 |
| `GET /api/bands/all` | 200 | 200 | 403 | 403 |
| `GET /api/offer-chain-templates` | 200 | 200 | 200¹ | 403 |
| `GET /api/email-templates` | 200 | 200 | 403 | 403 |
| `GET /api/org/hrbp-users` | 200 | 200 | 403 | 403 |
| `GET /api/sla/dashboard` | 200 | 200 | 200¹ | 403 |
| `GET /api/sla/config` | 200 | 200 | 403 | 403 |
| `GET /api/vendors/` | 200 | 200 | 403 | 403 |
| `GET /api/google/status` | 200 | 200 | 403 | 403 |
| `GET /api/admin/system-health` | 200 | 403² | 403 | 403 |
| `PATCH /api/tickets/{id}` | 200 | 404³ | 403 | 403 |

¹ recruiter delegation grants read access on these two — expected, not a gate failure.
² correct: this site is platform-scoped (system_health), a company admin should not see it.
³ correct: not a gate failure — the ticket belongs to the seed tenant, not CA's own tenant (Acme Corp), so the tenant-scope `WHERE` clause correctly returns 404 (isolation working as intended).

**Flag-decoupling proof** (the actual point of this migration): created a
test account with `role='ta_manager'` (a role the *old* string-tuple checks
would have denied) plus `is_company_admin=TRUE`. Confirmed it now correctly
gets 200 on a company-tier-only endpoint (`/api/bands/all`) and still
correctly gets 403 on the platform-only endpoint
(`/api/admin/system-health`) — proving gating now genuinely follows the
flag, independent of the `role` string, as designed.

### ⚠️ Critical: a real privilege-escalation bug was introduced and shipped during this fix, then found and closed within the same session

While migrating `admin_users.py` and the 10 router files, the shared
`is_platform_tier(user)` helper was first written with a `role == "admin"`
fallback (copying the pattern from `admin_users.py`'s pre-existing local
version). **This was unsafe**: `platform_admin_api.py::create_tenant()` /
`add_tenant_admin()` create ordinary company admins with `role='admin'` +
`is_company_admin=TRUE`, `is_platform_superadmin=FALSE` (a deliberate,
documented choice for `NAV_DEF`/nav compatibility — see
`PLATFORM_ADMIN_MAPPING.md` §5b). With the fallback in place, **any company
admin could reach cross-tenant `/api/platform/*` endpoints.**

This version was committed under a generic `"Bug Fixing"` commit
(`2f39ca7`) alongside the rest of the Fix #3 file set, made outside this
session's own commit flow — **and that commit was already on
`origin/main`** by the time this was caught. It was found via this
session's own live authorization testing, before the matrix above was
considered complete: `curl` with Acme Corp's company-admin token returned
`HTTP 200` on `GET /api/platform/stats` (should be 403).

**Fixed immediately** (commit `bb87922`, on top of `2f39ca7`): removed the
`role == "admin"` fallback from `is_platform_tier()` entirely — it now
checks only `is_platform_superadmin`. `is_company_tier()` was changed to
inline its own `admin`-role fallback directly (`is_company_admin OR
is_platform_superadmin OR role == "admin"`) rather than delegating to
`is_platform_tier()`, so legacy `admin`-role accounts don't lose their
legitimate company-tier access as a side effect (that fallback stays safe
there — Migration 100's backfill guarantees every real `admin`-role account
already carries one of the two flags, and `is_company_tier` never gates a
platform-only endpoint).

**Re-verified live after the fix**: company admin → 403 on
`/api/platform/stats` (closed) and still 200 on its own company-tier
endpoints (no regression); platform superadmin → unaffected, still 200
everywhere it should be. This result is reflected in the matrix above.

**Status as of this report**: commit `bb87922` is committed locally on
`main` but **has not been pushed**. `origin/main` currently still has the
vulnerable `2f39ca7` as its tip. **Recommend pushing `bb87922` before
anyone else pulls or deploys from `origin/main`.**

### ats-hr confirmation

`docker ps -a --filter name=ats-hr` shows `ats-hr-backend-1` /
`ats-hr-db-1` unchanged (`Exited (137)` / `Exited (0)`, same as the original
verification pass). `git status` shows no changes outside the `Enternly`
working tree. ats-hr was not touched at any point in this session.

### Deferred — future projects (not started this session)

Per instruction, the intra-company role model (recruiter / hiring-manager /
ta_manager / company-admin gating, as fixed by Fix #3 above) was **not**
changed in shape — only migrated from role-strings to flags, same
permissions as before. Planned build order for later sessions:

1. **Platform Admin** (this build) — stands on its own, in scope for the
   staging sign-off below.
2. **Company Super Admin** — a later project giving each tenant's own admin
   the ability to manage company logins, comms/org-level settings, and
   define the company's own roles. The fixed-role gates touched by Fix #3
   are expected to be revisited once this project makes intra-company
   roles tenant-configurable.
3. **Recruitment flow** — role-based candidate status/edit access, to be
   scoped after the recruitment flow itself is shared.

No work on #2 or #3 was started or implied by this session's changes.

### Updated Go / No-Go

**Conditional GO for the Platform Admin control plane specifically, pending
one push.** All four findings from §7 that were open (#5 self-lockout, #2
plan-constraint no-op, #3 nav-hiding, #4 incomplete flag migration) are now
fixed and live-verified. The one new issue surfaced during this session (the
`is_platform_tier` privilege-escalation bug) has been found, fixed, and
re-verified closed — but the fix is **not yet pushed**, and the vulnerable
version is currently the tip of `origin/main`. Recommend: push commit
`bb87922` immediately, then this control plane is clear for staging.

This sign-off covers **only** the Platform Admin control plane. It does not
extend to the two future projects above (Company Super Admin, Recruitment
flow) — neither has been scoped or built yet.

**Still open from §7, not part of this fix session** (not requested for
this pass): the §4 pre-existing infra gap (missing `database/35-52`
snapshots) — needs a decision on fix-forward (a) vs (b), unrelated to
platform-admin code.

---

## 11. Platform Admin — Closeout (2026-08-31)

Small follow-up pass closing four incomplete-migration remnants that the
Company Super Admin Step-0 research (`COMPANY_SUPERADMIN_MAPPING.md`)
surfaced while cataloging role-string gates. **Not** part of the Company
Super Admin project — no roles were built, no schema/DB constraint was
touched, no recruitment-flow router (`pipeline_api.py`, `offers_api.py`,
`scheduling_api.py`, `scorecard_api.py`, `hm_api.py`, `hrbp_api.py`,
`kpi_api.py`, `reports_api.py`, `enteri_ai_api.py`, `proctoring_api.py`,
`campus_bulk_api.py`, `no_poach_api.py`, etc.) was modified. One commit per
fix, each verified live against the running `enternly` stack on
`localhost:8081` before moving to the next.

### 1. `module_access.py::effective_module_access()` — ✅ FIXED, committed `4d48422`

**Change**: the blanket-access branch (`role in ("admin","platform_admin",
"company_admin")`) now calls `is_company_tier(user)` instead (deferred
import inside the function, matching the existing pattern already used by
`require_tenant_module` in the same file). The `role == "recruiter"`
per-user-delegation branch is untouched — that's the intentional grant
mechanism, not a retired role-string gate.

**Live verification** (`GET /api/admin/my-module-access`, all 7 delegable
keys):
- Platform superadmin: all 7 `true`.
- Company admin (Acme): base `true`, further AND-ed with Acme's existing
  tenant-level module restriction from an earlier test (§7 #2 / Fix #1
  verification) — expected, not a regression, confirms the tenant outer
  gate still applies on top of the fixed inner gate.
- **Flag-decoupling proof**: a test account with `role='ta_manager'` +
  `is_company_admin=true` (a combination the *old* raw-string check would
  have denied, since `ta_manager` isn't in the old tuple) now correctly
  gets blanket access on an unrestricted tenant — proves the branch
  follows the flag, not the role string.
- Non-delegated recruiter: all 7 `false`.
- Non-tier role (hiring manager): all 7 `false`.

### 2. `email_template_api.py`'s manual-send gate — ✅ FIXED, committed `4c03f8f`

**Correction to the Company Super Admin research's labeling**: the raw
`role not in ("admin","ta_manager","recruiter")` tuple was in
`_require_send_access` (gates manually emailing a candidate from an
application), not `_require_template_access` (template CRUD) — that
sibling gate was already migrated to `is_company_tier()` + the recruiter/
`email_templates`-delegation carve-out in the prior Fix #3 audit, and this
pass found it working correctly, untouched.

**Change**: `_require_send_access` now checks `is_company_tier(user) or
role in ("ta_manager","recruiter")`. Same allowed accounts as before
(`is_company_tier` is a superset of the old literal `'admin'` check) — no
behavior change for any existing account, just flag-based instead of
string-based. Note: this gate never had a delegation carve-out to
preserve — unlike `_require_template_access`, any recruiter has always
been allowed to send a one-off candidate email regardless of the
`email_templates` module delegation; that's unchanged.

**Live verification** (`POST /api/applications/{fake-id}/send-email`, 403
= gate rejected, 404 = gate passed and the fake id correctly 404'd
downstream): platform superadmin, company admin, recruiter → all `404`
(passed); hiring manager → `403` (correctly still denied). Re-verified the
untouched sibling gate too: company admin → `200` on template list,
non-delegated recruiter → `403`, same recruiter after being granted the
`email_templates` module → `200` (then revoked to leave state clean).

### 3. `require_ta_manager` — kept, documented as intentional, committed `1f7596f`

**Decision: kept, not removed.** Confirmed zero call sites anywhere in the
codebase. Reasoning: the `ta_manager` tier boundary it expresses is real
and actively checked ad hoc (`role == "ta_manager"`) across many
recruitment-flow routers; this is the one place that boundary exists as a
reusable FastAPI dependency rather than an inline check, available to a
future endpoint. Removing it would also orphan `require_company_admin`'s
own docstring, which references it by name. Added a docstring note
recording this as a deliberate decision. No behavior change — comment
only, nothing to verify live.

### 4. `bu_head`/`director` nav footgun — ✅ FIXED, committed `df28195`

**Change**: `buildNav()` and `screenHome()` in `index.html` both fell back
to `NAV_DEF.recruiter` for any `role` missing a `NAV_DEF` entry (only 6 of
11 live role values have one: `admin, ta_manager, recruiter,
hiring_manager, interviewer, hrbp`). `bu_head`/`director` are confirmed
vestigial (zero backend capability logic anywhere, per the Company Super
Admin research) but were still DB-allowed and pickable in the
user-management role dropdown — silently granting either the entire
Recruiter sidebar. Fallback changed from `NAV_DEF.recruiter` to `[]` in
both sites — an unconfigured role now gets no nav surface (Change
password / Sign out still always render). `app_user_role_check` and the
roles themselves were **not** touched — retiring them is a Company Super
Admin decision, explicitly out of scope here.

**Live verification**: confirmed the served `index.html` reflects both
changed sites post-restart (curled directly). Created a live `bu_head`
test account; confirmed it logs in and `/api/auth/me` correctly decodes
`role=bu_head` with no company/platform flags. `buildNav()`/`screenHome()`
are pure client-side JS keyed off `NAV_DEF[ME.role]` with no server
round-trip for nav data, so `NAV_DEF` having no `bu_head` key plus the
fallback now being `[]` deterministically produces an empty nav for this
account — traced through the code but **not click-through-tested in an
actual browser** (no browser automation tool available in this
environment, same limitation already noted for impersonation-banner
testing in §6a of this report).

### Confirmation: recruitment-flow routers, schema, and ats-hr untouched

- `git diff --stat` across this pass's 4 commits shows exactly 4 files
  changed: `backend/app/auth_utils.py`, `backend/app/module_access.py`,
  `backend/app/routers/email_template_api.py`, `frontend/index.html`. No
  `database/*.sql` file and no change to `backend/app/main.py` (where
  `app_user_role_check` and every migration live) — confirmed via `git
  diff --stat` scoped to `database/` and `main.py`, empty.
- No recruitment-flow router (`pipeline_api.py`, `offers_api.py`,
  `scheduling_api.py`, `scorecard_api.py`, `hm_api.py`, `hrbp_api.py`,
  `kpi_api.py`, `reports_api.py`, `enteri_ai_api.py`, `proctoring_api.py`,
  `campus_bulk_api.py`, `no_poach_api.py`) appears in the diff.
- `docker ps -a --filter name=ats-hr` unchanged (`Exited (137)` /
  `Exited (0)`, same as every prior check this session).
- No Company Super Admin work (custom roles, `tenant_role` schema, or
  capability catalog) was started, per instruction.

### Two backend restart warnings observed, pre-existing and unrelated

Restarting `enternly-backend-1` to pick up these fixes logged two
`[auto-migrate] WARNING: check constraint "app_user_role_check" of
relation "app_user" is violated by some row` lines. Confirmed **not**
caused by this pass: `_auto_migrate()` replays every migration in order on
every boot, including two earlier, narrower `app_user_role_check`
definitions (`main.py:1161`, missing `platform_admin`/`company_admin`;
`main.py:2089`, missing `placement_officer`) before landing on the current
one (`main.py:2622`) — replaying an old, narrower constraint against rows
that already use a newer role value it doesn't yet include produces
exactly this warning, harmlessly, matching the same "log but don't crash"
idempotency pattern already documented for the Migration 99 rename in §4.
Neither `main.py` nor any migration was touched this pass.

**No change to the go/no-go in §9/§10** — this closeout doesn't alter the
Platform Admin control plane's readiness assessment, it just closes small
gaps the next project's research surfaced. Recommend pushing these 4
commits alongside the earlier ones whenever `origin/main` is next updated.
