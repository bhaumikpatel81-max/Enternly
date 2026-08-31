# Platform Admin — Mapping Doc

Status: DRAFT (Step 0 research complete, implementation not yet started). This
file is the single source of truth for endpoint → table → UI mapping and is
kept updated as each work-order step lands.

All facts below were confirmed by reading the actual repo (not assumed) on
2026-08-31. Where the task brief's assumed names differ from reality, the real
name is used and the correction is noted.

---

## 0. Corrections vs. the task brief's assumptions

| Brief assumed | Reality | Correction applied |
|---|---|---|
| `require_platform_admin` "just aliases full-admin reach" | Confirmed: `role in ("admin","platform_admin")`, no tenant scoping, no separate audience. | Matches brief — no correction, just confirmation. |
| A "College" tenant type / `placement_officer` or `campus_recruiter` role already exists somewhere, brief says "confirm the exact role string via campus_bulk_api.py" | **Does not exist anywhere in the backend.** `campus_bulk_api.py` gates on `("recruiter","ta_manager","admin")` only; "campus" there means an Excel candidate-intake channel, not a tenant type or a login role. Grepped repo-wide for `placement_officer`/`campus_recruiter`/`college_admin` — zero hits. | This is genuinely new work, not a pattern to clone. Flagged in §Ambiguous below; a decision is proposed there so work isn't blocked. |
| `/api/admin/system-health` is an "operator dashboard" implying infra heartbeats | It reports **business metrics only** (user counts, ticket stats, pipeline snapshot, login trend) — reads `app_user`, `support_ticket`, `requisition`, `application`, `candidate`, `login_log`. It never touches `system_status`. Actual infra heartbeats/kill-switches (`cv_enricher_heartbeat`, `bg_lock_last_error`, `bg_task_status:*`) live in `system_status`, written/read by `cv_api.py` and various background workers. | Feature H's System Health screen needs to combine **both** sources — see §Feature H. |
| `activity_log`, `support_ticket`, `login_log` are tenant-scoped like other tables | None of the three carry a `tenant_id` column. Migration 96 (`database/59_tenant_isolation.sql`) tenant-scoped 14 tables but explicitly did not touch these three. Scoping is done today by joining through `app_user.tenant_id` (raised_by / actor_id / user_id). | Platform-admin cross-tenant reads over these tables don't need a `tenant_id` filter removed (there was never one) — they need a `JOIN app_user` for tenant attribution in the response, same pattern as the existing `/logins` endpoint (`activity_log_api.py`). |
| `application` (candidate pipeline record) is tenant-scoped | `candidate` **is** tenant-scoped (`tenant_id` added by Migration 96, unique index `(tenant_id, LOWER(email))`). `application` is **not** — it has no `tenant_id` column; scope via `application.candidate_id → candidate.tenant_id` or `application.requisition_id → requisition.tenant_id`. | `totalCandidates` (decision 4) should `COUNT(*) FROM candidate` — it already carries `tenant_id` directly, no join needed. |
| `role_labels` seeded with default values | `tenant.role_labels` defaults to `'{}'::jsonb` (empty) — defaults come from a Python dict (`_ROLE_LABEL_DEFAULTS` in `admin_users.py`), not a DB seed. | No correction needed for implementation, just noting for accuracy. |
| Migration numbering starts fresh at "62" | `database/*.sql` snapshot files (57–61) mirror `main.py` migrations 94–98; the highest migration comment in `main.py` is **Migration 99**. New `main.py` migrations continue as **Migration 100, 101, …**; new snapshot files continue as `database/62_*.sql, 63_*.sql, …` per the brief's own numbering (the two numbering schemes are offset by design — snapshot-file N mirrors migration N+37 today — new work keeps appending to both sequences independently, doesn't need to preserve the offset). | Documented so nobody "fixes" the offset. |
| `services/email_templates.py` is the email templating layer | The actual set-password / invite email path uses `services/email_layout.py`'s `build_branded_email()` (called from `password_api.py`), not `email_templates.py`. `services/connectors.py::send_email()` is the transport (SMTP via per-tenant config in `system_settings`, with a non-prod safety gate `EMAIL_REAL_SEND_ENABLED`). | New platform-admin emails (tenant welcome, renewal reminder) should follow the `password_api.py` pattern: build HTML via `email_layout.build_branded_email()`, send via `connectors.send_email(..., tenant_id=...)`. |

---

## 1. Confirmed existing schema (composite, post Migration 99)

**`tenant`** (57, 58): `id, name, slug, status ('active'|'trial'|'suspended'), plan, primary_contact_email, created_at, role_labels jsonb`.
No `tenant_type`, `tenant_code`, `logo_url`, `primary_colour`, `subscription_start_date`, `subscription_end_date`, `is_deleted` yet — all net-new in Feature C/E migrations.

**`app_user`**: `id, full_name, email (globally unique), role, is_active, created_at, password_hash, reset_token, reset_token_expires, tenant_id, token_version`.
No `is_platform_superadmin` / `is_company_admin` / `last_login_at` yet — net-new in Step 2 / Feature A.

**`client`**: `id, tenant_id, name, is_active, created_at` — this is Enternly's staffing-agency "external client" concept (who a requisition is hired *for*), unrelated to platform tenant CRUD. Not touched by this project except as a read-only fact in tenant detail if useful.

**`support_ticket`**: `id, raised_by (FK app_user), category, subject, description, status ('open'|'in_progress'|'resolved'), resolved_by, reply, created_at, updated_at, resolved_at`. No `tenant_id` — join via `raised_by`.

**`activity_log`**: `id, occurred_at, entity_type, entity_id, requisition_id, application_id, action, actor_id (FK app_user), actor_role, actor_label, from_value, to_value, detail jsonb, ip_address`. No `tenant_id` — join via `actor_id`. **Not present in any `database/*.sql` snapshot** (pre-existing drift, inline-only in `main.py` ~line 1279 as "Migration 56" with no matching snapshot file — noted, not fixed by this project since it's out of scope).

**`system_status`**: `key (PK), value, updated_at` — deliberately global KV store for background-worker heartbats/kill-switches, reused informally by `activity_log`'s best-effort-write failure marker.

**`candidate`**: has `tenant_id` (NOT NULL, FK, default seeded tenant) since Migration 96 — the count source for `totalCandidates`.

**`login_log`**: `id, user_id (FK app_user), user_role, logged_at, ip_address`. No `tenant_id` — join via `user_id`.

**`login_attempt`**: `email, ip_address, success, attempted_at` — rate-limit table used by `routers/auth.py`.

---

## 2. Auth internals (confirmed)

- One shared `SECRET_KEY`/`HS256`, three audiences: `AUD_STAFF`, `AUD_VENDOR`, `AUD_CANDIDATE`. `create_token()` (auth_utils.py) is staff-only, claims: `sub, email, role, name, tenant_id, tver, aud=AUD_STAFF, exp`.
- `_refresh_staff_claims()` re-reads `role, tenant_id, token_version, is_active, full_name` from `app_user` **on every request** and 401s on `tver` mismatch or inactive account — this is the existing "force logout" rail. New `is_platform_superadmin`/`is_company_admin`/`platform` claims must be threaded through this same function to get live revocation.
- `require_platform_admin` / `require_company_admin` / `require_ta_manager` (auth_utils.py:196-221) are pure `role in (...)` checks, **no tenant scoping of their own** (callers do that manually).
- `routers/auth.py`'s `/api/auth/login`: rate-limited (5/ip+email, 20/ip per 15min via `login_attempt`), generic-401 anti-enumeration, **no tenant-status check today** (gap — Feature C's suspend/delete enforcement needs to add one, in both `auth.py` login and `_refresh_staff_claims`), **no `last_login_at` write today** (gap — Feature A/F need to add one).
- `module_access.py`: `DELEGABLE_MODULES` = 7 keys (`vendors, form_fields, req_approvals, organisation, sla_settings, chain_templates, email_templates`), `effective_module_access(user)` — blanket true for `admin/platform_admin/company_admin`, per-grant for `recruiter`, false otherwise. This is the **inner gate** Feature D layers a tenant-level **outer gate** on top of.
- `main.py` has 6 sites (`db-stats`, `cv-database`, `sys-logs`, `/api/schedule`, `serve_resume`, req-form-data helper) that check `role == "admin"` / role-tuples **without** `platform_admin`/`company_admin` in the allow-list at all — a pre-existing gap, not introduced by this project. Left untouched (still gated by legacy `admin`, unaffected by the boolean-flag migration) and flagged here per Step 1's "if unsure, leave it and flag it."

## 3. Frontend internals (confirmed)

- `frontend/index.html`: `ME` is decoded **client-side directly from the JWT** in `localStorage['enternly_token']` — no `/api/auth/me` round trip, synchronous at top of `<script>`, `throw ''` halts execution and redirects to `/login` if absent/invalid.
- `api()` (line 1629): relative-path fetch, `Authorization: Bearer ${_tok}`, 401 → `logout()`, returns `null` + sets `_lastApiError` on any failure (never throws). `flash(msg, ok=true)` (line 1675) is the toast helper (not literally named `toast`).
- No generic `openModal()`/`closeModal()` — every screen declares its own static `<div class="modal-bg" id="…">` + `open{X}Modal()`/`close{X}Modal()` pair toggling a `.open` class, using the `modalBgMouseDown`/`modalBgClick` guard against native-`<select>`-click-through.
- `.pill` variants are keyed by literal backend status strings, not semantic names — new platform statuses (`active`/`suspended`/`trial` etc.) already match existing `.pill.active`/`.pill.inactive` classes; a few new ones (`expired`, `deleted`) will need new CSS rules alongside the existing block.
- `hasModuleAccess(key)` = `['admin','platform_admin','company_admin'].includes(ME.role) || !!MY_MODULE_ACCESS[key]` — **note**: `platform_admin`/`company_admin` are legacy role strings still referenced in exactly 3 places in index.html (lines 1940, 6964, 6965) plus the `ALL_ROLES` role-picker array (line 5776). Per decision 5 these stay as harmless legacy strings (accounts can still hold them) but gating moves to the new boolean flags — see §Step-1 audit below.
- `NAV_DEF` has **no entry for `platform_admin`/`company_admin`** — today a user with either role silently gets the `recruiter` nav (`buildNav()` falls back to `NAV_DEF[role] || NAV_DEF.recruiter`). Confirms there is no real platform console today.
- `login.html` / `set-password.html`: both fully self-styled (own inline `<style>`, not shared), POST to `/api/auth/login` and `/api/auth/reset-password` respectively, token stored as `localStorage['enternly_token']`. `set-password.html` redirects post-success based on `account_type` returned by `/api/auth/reset-token/validate` (`staff→/login`, `vendor→/vendor-portal`, `candidate→/candidate-portal`).

**Design decision — platform token storage key**: `platform-admin.html`/`platform-login.html` will use a **separate** `localStorage` key, `enternly_platform_token`, instead of reusing `enternly_token`. Reasons: (1) avoids the `NAV_DEF` fallback trap above if a platform admin ever opens `index.html` directly with a platform-flagged token; (2) lets an operator have both a platform-admin session and an impersonated/ordinary staff session open in different tabs without clobbering each other; (3) the impersonation token issued by Feature F is written to the *ordinary* `enternly_token` key when opening `index.html` as the target user, so the two keys never collide.

---

## 4. Endpoint → Table → UI Screen mapping

All endpoints below live in new `backend/app/routers/platform_admin_api.py` (prefix `/api/platform`, `Depends(require_platform_admin)` on every route) except login, in new `platform_auth_api.py` (prefix `/api/platform`, no auth dependency on the login route itself). `require_platform_admin` is rewritten in Step 2 to check `is_platform_superadmin = TRUE`.

| # | Endpoint | Method | Tables touched | UI Screen | Notes |
|---|---|---|---|---|---|
| A1 | `/api/platform/auth/login` | POST | `app_user` (read + `last_login_at` write, new col), `login_attempt` (reuse existing rate-limit helper) | `platform-login.html` | Rejects unless `is_platform_superadmin=TRUE`; issues staff JWT with `platform=true`. |
| B1 | `/api/platform/stats` | GET | `tenant`, `app_user`, `candidate` | Dashboard | Cross-tenant counts; excludes superadmins from `totalUsers`; `totalCandidates` = `COUNT(*) FROM candidate` (pool only). |
| C1 | `/api/platform/tenants?type=` | GET | `tenant`, `app_user` (employee count) | Companies list | No `tenant_id` clause (deliberate cross-tenant read). |
| C2 | `/api/platform/tenants` | POST | `tenant` (insert + `tenant_code` alloc), `app_user` (insert first company admin), `password_reset_token` + email | Companies → "New tenant" modal | `tenant_code` = `ET_` + next 4-digit seq, see §Tenant code algorithm. |
| C3 | `/api/platform/tenants/{id}` | GET | `tenant`, `app_user` | Company detail | Includes user list. |
| C4 | `/api/platform/tenants/{id}` | PUT | `tenant` | Company detail edit | name/domain/logo/colour/status fields only (not code, not soft-delete). |
| C5 | `/api/platform/tenants/{id}/status` | PATCH | `tenant`, `activity_log` (audit) | Company detail | activate/suspend (+ optional plan/end-date); ties into login enforcement. |
| C6 | `/api/platform/tenants/{id}` | DELETE | `tenant.is_deleted`, `activity_log` | Companies list | Soft delete only; blocks login for that tenant's users. |
| C7 | `/api/platform/tenants/{id}/users` | GET | `app_user` | Company detail → Users tab | |
| C8 | `/api/platform/tenants/{id}/admins` | POST | `app_user`, email | Company detail → Users tab | Creates an additional company admin for an existing tenant. |
| C9 | `/api/platform/tenants/{id}/placement-officers` | POST | `app_user`, email | Company detail (College tenants only) | Role string TBD — see §Ambiguous #1. Rejects with 400 if `tenant.tenant_type != 'College'`. |
| D1 | `/api/platform/modules` | GET/POST/PUT/DELETE | `module_catalog` | Module Catalog | DELETE = soft-disable (`is_active=false`), never hard-delete. |
| D2 | `/api/platform/tenants/{id}/modules` | GET/PUT | `tenant_module_config`, `module_catalog` | Company detail → Modules tab | Outer gate; existing `effective_module_access`/`hasModuleAccess` become the inner gate. |
| E1 | `/api/platform/subscription-plans` | GET/POST/PUT/DELETE | `subscription_plan_config` | Subscriptions → Plans tab | |
| E2 | `/api/platform/subscriptions` | GET | `tenant` (aliased `plan`→`subscription_plan`), `app_user` (admin email) | Subscriptions list | `days_remaining` computed from `subscription_end_date`. |
| E3 | `/api/platform/tenants/{id}/subscription` | PUT | `tenant` (`plan` + new date cols) | Company detail → Subscription tab | Writes the existing `plan` column, not a new one. |
| E4 | `/api/platform/tenants/{id}/send-renewal-reminder` | POST | `app_user` (find active company admin), email | Subscriptions list | Via `connectors.send_email` / `email_layout`. |
| E5 | `/api/platform/tenants/{id}/grace-config` | GET/PUT | `tenant` (new grace cols) | Company detail → Subscription tab | |
| E6 | `/api/subscription/status` | GET | `tenant` | index.html (tenant-facing, own-tenant only) | Not under `/api/platform` — company-admin/self-serve, `require_company_admin` scoped to own tenant. |
| F1 | `/api/platform/users?tenantId=&search=` | GET | `app_user`, `tenant` | All Users | Excludes `is_platform_superadmin=TRUE` rows. |
| F2 | `/api/platform/users/{id}/status` | PATCH | `app_user` (`is_active`, `token_version+1`) | All Users | Immediate session kill via existing `_refresh_staff_claims` `tver` check. |
| F3 | `/api/platform/impersonate/{userId}` | POST | `app_user` (read), `activity_log` (write) | All Users → "Impersonate" | 15-min token, `aud=AUD_STAFF`, `impersonatedBy`, `isImpersonation=true`; written to `enternly_token` (ordinary key) when opening `index.html`. |
| G1 | `/api/platform/tickets` | GET | `support_ticket` JOIN `app_user` | Issues & Tickets | Cross-tenant, filterable. |
| G2 | `/api/platform/tickets/{id}` | PATCH | `support_ticket` | Issues & Tickets | Status update, reuses `tickets_api.py` shape. |
| G3 | `/api/platform/tickets/{id}/replies` | GET/POST | `support_ticket_reply` (new table) | Issues & Tickets → thread view | |
| H1 | `/api/platform/audit` | GET | `activity_log` JOIN `app_user`, `login_log` JOIN `app_user` | Audit Logs | Filters: tenant, user, action, date range. |
| H2 | `/api/platform/analytics` | GET | `app_user`, `candidate`, `requisition`, `login_log` | Usage Analytics | Per-tenant counts; candidate = pool count only. |
| H3 | `/api/platform/system-health` | GET | `app_user`, `support_ticket`, `requisition`, `application`, `candidate`, `login_log` (existing `/api/admin/system-health` query set) **+ `system_status`** (heartbeats/kill-switches) | System Health | Superset of the existing `tickets_api.py` endpoint; regated to `is_platform_superadmin`. See §Feature H below — existing endpoint is extended in place, not duplicated. |
| I1 | `/api/platform/settings/superadmins` | GET/POST/DELETE | `app_user` (`is_platform_superadmin`) | Settings → Superadmin roster | Grant/revoke; create-fresh path reuses Feature A's `app_user` insert + invite email. |
| I2 | `/api/platform/settings/defaults` | GET/PUT | new `platform_settings` KV table (or reuse `system_status`) | Settings → Defaults | Default new-tenant plan + default enabled modules. |

### Tenant code algorithm (decision 1)

```sql
SELECT tenant_code FROM tenant WHERE tenant_code ~ '^ET_[0-9]{4}$' ORDER BY tenant_code DESC LIMIT 1
```
Parse the numeric suffix, `+1`, zero-pad to 4 digits, prefix `ET_`. Wrapped in the same transaction as the tenant INSERT to avoid a race between two concurrent tenant creates (`SELECT ... FOR UPDATE` on a small sequence-holder row, or rely on Postgres serializable retry — final approach decided at implementation time in Step 3).

### Feature D module catalog seed list

**Blocked on reading `frontend/index.html`'s full `NAV_DEF` object** (~line 1951) to enumerate the real per-role nav/module list — the 7 `DELEGABLE_MODULES` keys are a subset (org-config screens only), not the full module surface (Requisitions, Pipeline, Candidates, Reports, Interviews, Offers, etc. are core nav items with no existing "module key" identifier). This read will happen at the start of Feature D implementation; noted here so the catalog seed isn't guessed.

---

## 5. Ambiguous — needs human decision

1. **Placement-officer role string (Feature C, decision 3).** No `placement_officer`/`campus_recruiter`/college-tenant concept exists anywhere in the backend today (confirmed by full-repo grep). Proposed default, pending confirmation: add `placement_officer` as a new valid `app_user.role` string (extend the `app_user_role_check` CHECK constraint and `_VALID_ROLES` in `admin_users.py`), scoped like any other tenant-bound role, with no special module access beyond what a College tenant's modules allow. Will proceed with this default if not redirected, since it's additive and reversible.
2. **`module_catalog` seed list.** Needs the full `NAV_DEF` read (see above) before Feature D can seed real module keys/routes. Will read and update this doc before implementing Feature D.
3. **Grace-period semantics (Feature E).** Brief says "Store grace settings on tenant" but doesn't specify the exact columns/behavior (e.g., does a tenant become `read-only` or fully blocked during grace?). Proposed default: `grace_period_days INT DEFAULT 0` on `tenant`; a tenant is only login-blocked once `now() > subscription_end_date + grace_period_days`; during the grace window itself, login still succeeds (matches "grace" meaning "still working, past due"). Will implement this default and flag it as adjustable.
4. **`platform_settings` defaults table (I2).** Brief doesn't specify a table name for "default new-tenant plan / default enabled modules." Proposed: new small table `platform_settings(key PK, value jsonb, updated_at)`, distinct from the already-overloaded `system_status` (which is for background-worker state, not admin-configurable settings — see §0 correction).
5. **Subscription plan `allowed_modules_json` vs. `tenant_module_config` overlap (Feature D+E) — RESOLVED, live constraint.** Confirmed with the user: a tenant can never have a module enabled that its current plan's `allowed_modules_json` doesn't include (empty `[]` = no restriction, so pre-existing plans don't lock tenants out). Changing a tenant's plan auto-disables any currently-enabled module the new plan doesn't cover; the module-toggle endpoint rejects enabling an out-of-plan module with 400. Implemented in commit 4 (`subscription_plan_config` created alongside `module_catalog`/`tenant_module_config` so the constraint ships complete, not stubbed).
5b. **Company admins created by the platform console get `role='admin'`, not `role='company_admin'`, plus `is_company_admin=TRUE`.** `frontend/index.html`'s `NAV_DEF` (the nav-menu lookup keyed by `role`) has entries for `admin`/`ta_manager`/`recruiter`/`hiring_manager`/`interviewer`/`hrbp` but **no** `company_admin`/`platform_admin` entry — `buildNav()` falls back to the recruiter nav for either of those role strings (a pre-existing gap, confirmed in Step 0 research, out of scope to fully fix here). Setting the new first-company-admin/added-company-admin accounts to `role='admin'` avoids handing a customer's own admin a broken recruiter-only nav on first login, while `is_company_admin=TRUE` is what actually authorizes them under the new boolean-flag gates. Platform-admin accounts (Settings → Superadmins) use `role='platform_admin'` instead, since they operate the platform console (no `NAV_DEF` involvement), not `index.html`.

---

## 6. Step 1 audit — role-string gate sites (to be filled in during implementation)

Table of every `role in (...)`/`role ==` site across `backend/app/routers/*.py` and `frontend/index.html` referencing `platform_admin`/`company_admin`, with the company-scoped-vs-platform-scoped decision for each, will be appended here as Step 1's cleanup pass executes (work-order step 9). Sites already inventoried by research:
- `auth_utils.py`: `require_platform_admin`, `require_company_admin`, `require_ta_manager` — rewritten in Step 2.
- `module_access.py::effective_module_access` — role check stays as a fallback for legacy accounts; boolean flags take precedence once Step 2 lands (exact precedence rule TBD at Step 2 implementation, will update here).
- `admin_users.py`: `_PLATFORM_ONLY_ROLES`, `_VALID_ROLES`, `_require_settings_access`/`_require_admin_settings`/`_require_module_access` — all currently role-string based, migrate per-site.
- `index.html` lines 1940, 5776, 6964, 6965 — see §3 above.

---

## 7. Deliverables checklist

- [x] PLATFORM_ADMIN_MAPPING.md (this file) — will be updated after each work-order step.
- [ ] `platform_admin_api.py`, `platform_auth_api.py` + `main.py` wiring
- [ ] `auth_utils.py`, `module_access.py` updates
- [ ] Migrations 100+ in `main.py` + `database/62_*.sql`, `63_*.sql`, …
- [ ] `platform-login.html`, `platform-admin.html`, impersonation banner in `index.html`
- [ ] `PLATFORM_ADMIN_TESTPLAN.md`
