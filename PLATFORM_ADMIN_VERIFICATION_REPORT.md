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
