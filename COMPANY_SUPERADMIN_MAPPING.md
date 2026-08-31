# Company Super Admin — Step 0 Research (Mapping Doc)

**Status: research only. No code, schema, or migration changes were made
producing this document.** Platform Admin (`is_platform_superadmin`,
`is_company_admin`, `is_company_tier()`/`is_platform_tier()`, and every
router migrated in the prior Fix #3 audit) is treated as **done and
stable** — nothing below proposes touching it.

Date: 2026-08-31. Scope per instruction: company-level login/user
management, communication and org-level settings, and defining the
company's own roles. Recruitment-flow-specific permissions are explicitly
**out of scope** until that flow is shared — §2's capability inventory is
marked provisional for exactly that reason.

---

## 1. Gap list — what Company Super Admin needs vs. what `is_company_admin` already grants

`require_company_admin` / `is_company_tier()` (`backend/app/auth_utils.py`)
already gates a real, working set of company-level capabilities. Today:

```python
def is_company_tier(user: dict) -> bool:
    return bool(user.get("is_company_admin") or user.get("is_platform_superadmin") or user.get("role") == "admin")
```

**Already granted to every `is_company_admin` user** (see §4 for the full
table): staff CRUD + password reset + email-scanning creds
(`admin_users.py`), role display-label renaming (`tenant.role_labels`),
per-recruiter module delegation (`module_access.py`), SMTP/comms/general
settings (`system_settings` KV table), application form-field config, org
structure (Group Companies / Business Units, `org_api.py`), client roster
(`client_api.py`), vendor/SLA/chain-template/email-template management.

**Genuinely missing, in scope for this project:**

1. **Tenant-configurable roles.** `app_user.role` is a fixed, DB
   `CHECK`-constrained enum (11 values, all tenants share the same set —
   see §3). There is no schema or endpoint letting a tenant define a new
   role key or redefine what capabilities an existing key carries.
   `tenant.role_labels` only renames the *display* of 7 fixed keys — it
   changes nothing about what those keys can do. This is the core gap the
   project exists to close.
2. **No distinct "Company Super Admin" identity above ordinary
   `is_company_admin`.** A tenant can have multiple `is_company_admin=TRUE`
   users today, but nothing designates one as senior to another — e.g.
   there's no existing concept of "only the super admin may define custom
   roles or manage other company admins." If the project wants exactly
   that split, it's new, not a repurposing of an existing flag.
3. **No self-service "manage other company admins" flow.**
   `_assert_can_act_on_user` (`admin_users.py`) currently blocks any
   non-platform-tier actor from acting on another `admin`/`platform_admin`/
   `company_admin`-role account — only a Platform Admin can touch another
   company admin's account today. A Company Super Admin managing peer
   company-admin accounts in their own tenant isn't supported.
4. **`bu_head` and `director` are vestigial role values** — allowed by the
   DB constraint and pickable in the `ALL_ROLES` UI dropdown, but **zero**
   backend capability checks branch on either string anywhere in the
   codebase, and neither has a `NAV_DEF`/`HOME_TILES` entry in
   `index.html` (see §2). Assigning either role today silently falls back
   to the full Recruiter nav (`NAV_DEF[role] || NAV_DEF.recruiter`,
   `index.html`) — a live UX footgun, and a candidate first target for
   either retirement or a custom-role proof of concept.
5. **`module_access.py`'s `effective_module_access()` still has its own,
   separate raw role-string check** (`role in ("admin","platform_admin",
   "company_admin")` → blanket access), never touched by the prior Fix #3
   audit because it predates/parallels the flag system rather than
   duplicating a retired pattern. Adjacent technical debt, worth a decision
   in this project since it's the closest existing analog to a
   capability-grant mechanism (see §3).
6. **`tenant.role_labels` is confirmed unused in production** — all 5 live
   tenants have `role_labels = '{}'`. No live-data migration risk from
   redefining its shape.

---

## 2. Capability inventory — every intra-company role-string gate (PROVISIONAL)

**This is the core deliverable, and it is explicitly provisional.** The
final capability set a tenant-defined role needs to express will only be
confirmed once the recruitment flow is shared — this is a catalog of what
exists today, not a proposed permission model. Do not treat groupings below
as a finished design.

Already-migrated sites (out of scope, listed only so the inventory reads as
complete): `main.py:109`, `admin_users.py` (`require_users_read`,
`require_user_write`, `_require_settings_access`, `_require_admin_settings`,
`_require_module_access`), `client_api.py`, `password_api.py:148,255`,
`org_api.py:34`, `chain_templates_api.py:31/37`, `sla_api.py:42/48`,
`vendor_api.py:107` — all now use `is_company_tier()`/`is_platform_tier()`.

### Dominant pattern across most operational routers

A repeated 3–5 way tier split: **`admin`/`ta_manager`** (full TA-management,
`admin` here is the legacy Company-Admin alias) → **`recruiter`**
(execution, usually scoped to owned requisitions via
`_recruiter_owns_req`) → **`hiring_manager`** (own requisitions only) →
**`hrbp`** (read-only, BU/company-scoped) → **`interviewer`** (own
interview/scorecard only).

### Per-file inventory

| File | Lines (representative) | Check | Protects | Capability |
|---|---|---|---|---|
| `activity_log_api.py` | 214, 227, 251, 268 | `role == "recruiter"` + `_recruiter_owns_req` | Requisition timeline | View own-requisition activity history |
| `campus_bulk_api.py` | 254, 517, 568, 599, 660, 747, 991, 1030, 1066, 1096 | `role not in ("recruiter","ta_manager","admin")` | Campus Hiring bulk endpoints | Manage campus hiring drives |
| `cv_api.py` | 504, 528, 580, 761, 788, 800, 1158, 1277, 1310 | `role not in ("ta_manager","admin")` (761 has owner carve-out) | CV Repository | Manage CV repository (narrower than most — excludes plain `recruiter`) |
| `documentation_api.py` | 34, 43 | `role not in ("recruiter","ta_manager","admin")`; 43 further narrows to `recruiter` | Offer-letter/doc generation | Generate/manage recruiter documentation |
| `email_template_api.py` | 56 | `role not in ("admin","ta_manager","recruiter")` | Email templates | Manage email templates — **base gate itself was never migrated to `is_company_tier`, unlike its sibling files** |
| `gamification_api.py` | 37, 39 | `role in ("ta_manager","recruiter","admin")`; 39: `role == "hiring_manager"` | Leaderboard | Leaderboard visibility scope |
| `enteri_ai_api.py` | 315,317,340,392,448,491,502,508,583,620,655,677,682,743,746,1066,1101,1107,1170,1986,2024,2065 | mixed tuples + `hiring_manager`/`hrbp` narrowing; `2277`: `role != "admin"` | AI interview invites/transcripts/appeals | Send/manage AI interviews; line 2277 is the tightest (admin-only config) |
| `hiring_plan_api.py` | 760 | `role in ("recruiter","ta_manager")` | Hiring plan edit | Edit hiring plan — **excludes `admin`, inconsistent with every other file's admin-inclusive pattern** |
| `hrbp_api.py` | 59 (`_require_hrbp`) | `role != "hrbp"` | HRBP dashboard | Entire HRBP-only surface, scoped further by `scope_requisitions_for_hrbp()` |
| `hm_api.py` | 67, 72, 74 | 67: `role not in ("hiring_manager","admin")`; 72: `role in ("ta_manager","admin")`; 74: recruiter+delegation | HM dashboard / req-approvals | HM's own dashboard vs. TA-manager approval view |
| `kpi_api.py` | 51, 195, 309 | `role == "hrbp"` / `role == "recruiter"` narrowing | KPI dashboard | Scope KPI pivot to own BU/numbers |
| `offers_api.py` | `_assert_offer_role` 103-142, `_assert_recruiter_owns_req` 145-159, + 115,118,126,153,308,335,372,505,510,513,636,680,755,851,1034,1091 | tiered: admin/ta_manager full, recruiter/HM own-req only, else must be a named approval-chain step | Offer CRUD, approvals | View/create/approve/resend offers; `851`/`1034` restrict approval-step actioning to the named approver unless `admin` |
| `pipeline_api.py` | ~45 sites, 121-2826 | `_is_recruiter_scoped`, `_deny_hrbp`, tiered tuples, `hiring_manager`/`interviewer`/`hrbp` branches; tightest at 2525/2545 (`admin`/`ta_manager` only) | Core requisition/candidate pipeline | Create requisitions, move candidates through stages, delete applications — the largest single surface |
| `proctoring_api.py` | 100-633 (15 sites) | `role not in ("recruiter","ta_manager","admin")`; 327 narrows to owned req | Proctoring review | Review AI-interview proctoring flags |
| `reports_api.py` | 19,128,206,234,303,370,396 | `recruiter` narrows to own numbers; `role not in ("ta_manager","admin")` for team exports; `role not in ("recruiter","ta_manager")` for "My Reports" (excludes admin/HM/HRBP — another outlier); `role != "hiring_manager"` for HM reports | Reports/exports | Own vs. team-wide reporting |
| `scheduling_api.py` | ~20 sites, 565-1864 | tiered + `_recruiter_owns_req`; 969: HM availability; 980/1046/1114: combined tier+ownership; 1838-1864: broadest read (`recruiter,ta_manager,admin,hiring_manager,hrbp`) | Interview scheduling | Schedule/edit/cancel interviews |
| `scorecard_api.py` | `_is_panelist`/`_check_visibility` 154/174, 210,213,221,354,395-396,694,871-899 | panel-membership + tiered visibility, `hrbp` gets a distinct branch at 891 | Scorecards | Who can see/submit a scorecard |
| `no_poach_api.py` | `_VIEW_ROLES={ta_manager,recruiter,admin}` (29), `_MANAGE_ROLES={ta_manager,admin}` (30) | set-based, via `_require_view`/`_require_manage` | No-Poach list | View (recruiter+) vs. manage (ta_manager/admin only) |
| `custom_reports_api.py` | `_ALLOWED_ROLES=(ta_manager,admin,recruiter,hiring_manager)` (29), via `_check_role` (86-88) | Custom report builder | Who can build custom reports |
| `tickets_api.py` | — | no raw role-tuple gate ("all roles raise"); resolution already migrated to `is_platform_tier` | Support tickets | n/a — already clean |

**Files confirmed with no raw role-string gates**: `notifications_api.py`
(auth rule is "own notifications", not role), `bands_api.py`,
`google_calendar_api.py`, `candidate_portal_api.py`, `platform_admin_api.py`,
`platform_auth_api.py`, `client_api.py`, `cv_match_api.py`.

**Already-migrated base gate + intentional recruiter-delegation carve-out**
(not part of this inventory's target, listed for completeness):
`vendor_api.py:107`, `sla_api.py:42/48`, `chain_templates_api.py:31/37`,
`org_api.py:34` — base gate is `is_company_tier()`; the only remaining raw
string is `role == "recruiter" and recruiter_has_module(...)`, which is the
existing, intentional per-user delegation mechanism (§3), not a retired
pattern.

### `module_access.py`'s own gate (adjacent, not a router)

`effective_module_access()` (module_access.py:154-160): `role in ("admin",
"platform_admin","company_admin")` → blanket access; `role == "recruiter"`
→ per-user grants; anyone else → all `False`. Predates the flag system,
was not touched by the prior audit (not a "retired role-string check" in
the same sense — it's a parallel mechanism). Flagged in §1 as adjacent
technical debt.

### `frontend/index.html`

- **`ALL_ROLES`** (line 5858): `['platform_admin','company_admin','admin',
  'ta_manager','recruiter','hiring_manager','bu_head','director',
  'interviewer','hrbp']` — populates the role picker in the new/edit-user
  modals (5901, 5932). Deliberately excludes `placement_officer` (created
  only via the platform console).
- **`NAV_DEF`** (2022-2125): only **6 keys** — `admin`, `ta_manager`,
  `recruiter`, `hiring_manager`, `interviewer`, `hrbp`. No entry for
  `bu_head`, `director`, `company_admin`, or `platform_admin`.
- **`buildNav()`** (2283-2287) and the router-title lookup (2642):
  `NAV_DEF[role] || NAV_DEF.recruiter` — any role missing from the 6-key
  map silently gets the **full Recruiter nav**. Confirms §1 item 4 (`bu_head`
  /`director` footgun) at the frontend layer too.
- **`HOME_TILES`** (2620-2627): same 6 keys only.
- `isCompanyTier()` (2007-2009) / `hasModuleAccess()` (2010-2012): frontend
  mirrors of the backend flag helpers, already migrated, purely UX (backend
  still enforces).
- Dozens of inline `[...].includes(ME.role)` UI-visibility checks (e.g.
  3443, 3470, 3580, 3935, 3971, 3995, 4040, 4392, 4437, 4468, 4776, 4928,
  5127, 5131-5132, 5247, 6284) — these mirror the backend inventory above
  for show/hide purposes only, not independent authorization.

### Confirmed vestigial: `bu_head`, `director`

Zero functional/permission logic anywhere in the backend for either string
outside the DB constraint, the `ALL_ROLES`/`_VALID_ROLES` pickers, and
`_ROLE_LABEL_DEFAULTS` display labels. No `NAV_DEF`/`HOME_TILES` entries.
Zero live rows with either role (§5). No approval-chain "approver type"
concept references them either. Strong candidate for retirement or reuse
as a first custom-role proof of concept — flagging for a decision, not
deciding here.

---

## 3. What already exists to build on

### `tenant.role_labels` (JSONB, added Migration 95, `main.py:2117`)
```sql
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS role_labels JSONB NOT NULL DEFAULT '{}'::jsonb
```
Design intent (documented at `main.py:2104-2108`): role *keys* are fixed
system identifiers permission checks key off of; this column only renames
their *display* label. `GET /api/admin/role-labels` (any authenticated
user, merges with `_ROLE_LABEL_DEFAULTS`) / `PUT /api/admin/role-labels`
(`require_company_admin`) — write path filters to exactly the 7
non-admin-tier keys (`ta_manager, recruiter, hiring_manager, bu_head,
director, interviewer, hrbp`), deliberately excluding `admin`/
`platform_admin`/`company_admin` as "structural to the SaaS itself"
(`admin_users.py:432-434`). Confirmed unused in production (§5).

### `module_access.py` — the closest existing analog to a capability-grant system
- `DELEGABLE_MODULES` (23-31): 7 fixed keys, each mapped to a label.
- `GATED_NAV_MODULES` (39-42): 9 tenant-level-only keys, enforced via
  `require_tenant_module(module_key)` — a dependency-factory pattern
  applied at `APIRouter(dependencies=[...])` level. Clean template for a
  future "does this tenant/role have capability X" gate.
- `set_recruiter_grant` / `get_recruiter_grants` / `recruiter_has_module`
  (45-92): per-`(recruiter_id, module)` boolean in
  `recruiter_module_access(recruiter_id, module, enabled, granted_by,
  granted_at)`, written + audit-logged atomically inside one
  `transaction()` (55-84) — the concrete template for "grant capability X
  to user Y," worth copying for tenant-defined role→capability grants.
- `effective_module_access` / `client_module_status` (144-179): two-layer
  gate — tenant-level toggle AND per-user grant. Exactly the shape a
  tenant-configurable-roles feature needs, just currently hard-coded to
  16 module keys instead of open-ended capabilities.
- **Explicit existing boundary** (module_access.py:12-14 docstring):
  "Users & Access" (account/role management) is intentionally *never*
  delegable — a recruiter can never be granted the power to create
  accounts or change roles. A Company-Super-Admin-defines-roles feature
  needs an equivalent hard boundary: a tenant-defined role must never be
  able to grant itself company-admin or platform tier.

### `app_user.role` + `app_user_role_check`
Redefined three times as roles were added, each time via a
`DO $$ ... DROP CONSTRAINT ... $$` + re-`ADD CONSTRAINT` block in
`_auto_migrate()` (`main.py:1161-1163` → `2089-2092` → `2622-2625`,
current/live). **Current live allowed set (11 values, one shared enum
across every tenant):** `platform_admin, company_admin, admin, ta_manager,
recruiter, hiring_manager, bu_head, director, interviewer, hrbp,
placement_officer`. Migrations only ever widen this set, never narrow it.
A per-tenant custom-role feature cannot be expressed as a single global
`CHECK (role IN (...))` string literal — it needs either a separate table
(`tenant_role`) with `app_user.role` becoming a foreign key, or a parallel
column layered alongside the existing fixed `role`.

### `ALL_ROLES` (`index.html:5858`)
Populates the role `<select>` in the new/edit-user modals only, via
`roleLabel(r)` (reads `ROLE_LABELS` from `/api/admin/role-labels`,
falling back to a title-cased key). Missing `placement_officer`
intentionally (created only via the platform console).

### `is_company_tier()` / `is_platform_tier()` (`auth_utils.py:260-300`, current code)
```python
def is_platform_tier(user: dict) -> bool:
    return bool(user.get("is_platform_superadmin"))

def is_company_tier(user: dict) -> bool:
    return bool(user.get("is_company_admin") or user.get("is_platform_superadmin") or user.get("role") == "admin")
```
`require_company_admin` / `require_platform_admin` (auth_utils.py:303-322)
wrap these as FastAPI dependencies. **Not to be changed by this project.**

**Adjacent finding**: `require_ta_manager` (auth_utils.py:325-330) is a
third dependency, still string-based (`role not in ("admin",
"platform_admin","company_admin","ta_manager")`), confirmed to have **zero
call sites** anywhere in the codebase outside its own definition — dead
code, or a dependency meant for an endpoint that was never built. Worth a
decision (retire vs. keep for a near-future use), not urgent.

---

## 4. Company-level settings inventory

| Area | File | Endpoints | Gate | What a company admin can do today |
|---|---|---|---|---|
| User/login management | `admin_users.py` | `GET/POST /api/admin/users`, `PATCH .../{id}`, `PATCH .../{id}/full`, `DELETE .../{id}`, `DELETE .../{id}/permanent`, `POST .../{id}/reset-password` | `require_company_admin` / `require_users_read` / `require_user_write` | Create/edit/deactivate/hard-delete staff, reset passwords, assign roles (bounded by `_assert_can_assign_role` / `_assert_can_act_on_user`) |
| Per-user email-scan creds | `admin_users.py:279-337` | `GET/PUT/DELETE /api/admin/users/{id}/email-config` | `require_company_admin` | Set/clear a staff member's Gmail + App Password for inbox scanning |
| Role display labels | `admin_users.py:446-477` | `GET/PUT /api/admin/role-labels` | GET: any staff; PUT: `require_company_admin` | Rename the 7 non-admin-tier role keys' labels |
| Per-recruiter module delegation | `admin_users.py:486-563` + `module_access.py` | `.../module-access/recruiters`, `.../module-access/{id}`, `.../my-module-access` | `_require_settings_access` | Grant/revoke 1 of 7 modules to 1 recruiter |
| Comms/general settings | `admin_users.py:399-619` | `GET/POST /api/admin/settings`, `POST /api/admin/settings/test-email` | `_require_admin_settings` | SMTP config, from-name, base URL, "about company" text, auto-JD-email toggle, company name, TA signature — stored in generic `system_settings(tenant_id, key, value)` |
| Application form fields | `admin_users.py:622-664` | `GET/POST /api/admin/form-field-config` | `_require_form_field_access` (company tier or delegated recruiter) | Which fields are required on the public application form |
| Org structure | `org_api.py` | `/api/org/group-companies`, `/api/org/business-units` | `_require_admin` (company tier or delegated recruiter) | CRUD Group Companies / Business Units, assign HRBPs |
| Clients (RPO/staffing) | `client_api.py` | `POST/PATCH/DELETE /api/clients` | `require_company_admin` | Manage client roster for external-hire requisitions |
| Vendors / SLA / chain templates | `vendor_api.py`, `sla_api.py`, `chain_templates_api.py` | (base-gated) | `is_company_tier` + recruiter delegation | Vendor mgmt, SLA thresholds, approval-chain templates |
| Email templates | `email_template_api.py:56` | (still raw tuple) | `role not in ("admin","ta_manager","recruiter")` | Manage email templates — base gate never migrated (§1 item 5, §2) |

**No dedicated org-level/branding settings surface beyond the above** —
tenant branding (`logo_url`, `primary_colour`, `tenant_code`) exists on the
`tenant` table but is writable only via the *platform* console
(`platform_admin_api.py`), not by a company admin today. Worth a decision:
should Company Super Admin get tenant-branding self-service, or does that
stay platform-only?

---

## 5. Live role distribution (informs backfill risk, §6)

```
role                | is_company_admin | is_platform_superadmin | count
---------------------+------------------+-------------------------+------
admin                | true             | false                   | 5
admin                | false            | true                    | 1
platform_admin       | false            | true                    | 1
ta_manager           | true             | false                   | 1
hiring_manager       | false            | false                   | 1
placement_officer    | false            | false                   | 1
recruiter            | false            | false                   | 1
```
(11 app_user rows total across 5 tenants — mostly test data from the
Platform Admin verification session.) **Zero live rows** for
`company_admin`, `bu_head`, `director`, `interviewer`, `hrbp` as raw string
values. `tenant.role_labels` is `{}` on all 5 tenants. In this environment
a backfill would only need to remap a handful of `admin`/`ta_manager`
rows — real production data volumes are unknown and should be checked
before relying on this as representative.

---

## 6. Candidate schema sketch (NOT FINAL) + backfill strategy

Sketched only to make the scope of the schema problem concrete — not a
design to build from. Two shapes worth comparing once the recruitment flow
is known:

```sql
-- illustrative only, not proposed for implementation yet
CREATE TABLE tenant_role (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenant(id),
    role_key     TEXT NOT NULL,        -- tenant-scoped, not globally unique
    label        TEXT NOT NULL,
    is_system    BOOLEAN NOT NULL DEFAULT FALSE,  -- true for the 11 fixed roles, un-deletable
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, role_key)
);

CREATE TABLE tenant_role_capability (
    tenant_role_id UUID NOT NULL REFERENCES tenant_role(id),
    capability_key TEXT NOT NULL,      -- open question: fixed catalog vs free text, see §7
    PRIMARY KEY (tenant_role_id, capability_key)
);

-- app_user.role would need to become either:
--   (a) a FK to tenant_role.id (breaking change to every `user["role"]` string comparison), or
--   (b) left as-is, with a NEW app_user.tenant_role_id nullable column that,
--       when set, overrides/supplements the fixed role for capability checks
--       (additive, lower blast radius, but two sources of truth to keep in sync)
```

**Highest-risk part of this whole project: the backfill.** Every existing
`app_user.role` value (11 possible today) must map onto an equivalent
tenant-defined-or-system role with *zero* net capability change on deploy,
across every one of the ~150+ call sites inventoried in §2 — not just the
company-level ones scoped for this project, since `role` is a shared column
read by the recruitment-flow routers too. Two ways this can go wrong:
1. A tenant that never touches the new "define roles" UI must keep working
   exactly as today — implies system roles need to ship pre-seeded per
   tenant (or the fixed-role code paths need to keep working unchanged
   until a tenant opts in), not a flag day.
2. Any capability inventoried in §2 that's missed in the system-role seed
   is a silent lockout or silent privilege grant depending on which
   direction the miss goes — the same class of risk the Fix #3 audit just
   spent a full session catching and closing for the company/platform-tier
   flags. This will need the same "grep everything, verify both directions
   live" rigor, but across a much larger surface (§2 lists 20 files vs.
   Fix #3's 10).

No backfill approach is proposed here — flagging the shape of the risk so
it's weighed before an implementation plan is written, per instruction.

---

## 7. Open questions / decisions before an implementation plan

1. **Option A vs. Option B for role composition:**
   - **Option A — fixed capability catalog, tenant composes roles from it**
     (tenant picks which of a fixed, Enternly-defined capability list each
     of their custom roles gets). Lower risk: capabilities stay a closed,
     versioned set the backend already knows how to check (extends the
     `module_access.py` pattern in §3 directly — capability keys instead of
     module keys). Matches how `DELEGABLE_MODULES`/`GATED_NAV_MODULES`
     already work.
   - **Option B — free-form/open-ended roles** (tenant defines arbitrary
     role/permission combinations without a fixed catalog constraining
     them). More flexible, much higher engineering cost (every one of the
     ~150+ sites in §2 would need to resolve a dynamic capability set at
     request time instead of a fixed enum check) and higher risk of gaps.
   - **Recommendation: Option A.** The existing delegation mechanism in
     `module_access.py` is proof this pattern already works in this
     codebase, at smaller scale. Recommend extending that pattern
     (fixed capability catalog, tenant-composed roles) rather than building
     an open-ended permission engine, at least for the first version.
2. **Which capabilities in §2 depend on the not-yet-shared recruitment
   flow?** Likely candidates, pending confirmation: everything in
   `pipeline_api.py`, `offers_api.py`, `scheduling_api.py`,
   `scorecard_api.py`, `hm_api.py`, `hrbp_api.py`, `kpi_api.py`,
   `reports_api.py`, `enteri_ai_api.py`, `proctoring_api.py`,
   `campus_bulk_api.py`, `no_poach_api.py` — i.e. most of §2. Only the
   company-level settings in §4 are confirmed in-scope for this project
   independent of the recruitment flow. Recommend treating §2 as a
   reference inventory, not a target list, until the flow is shared.
3. **Does a Company Super Admin get tenant-branding self-service**
   (`logo_url`/`primary_colour`/`tenant_code`, currently platform-console-only)?
4. **Should `bu_head`/`director` be retired, or reused as the first
   custom-role proof of concept?**
5. **Does a distinct "Company Super Admin" tier get introduced above
   ordinary `is_company_admin`** (per §1 item 2), or does "define company
   roles" become a capability any `is_company_admin` user already has?
6. **`app_user.role` schema direction** (§6): FK-to-`tenant_role` (clean,
   breaking) vs. additive `tenant_role_id` alongside the existing fixed
   `role` (lower blast radius, two sources of truth) — needs a decision
   before any migration is drafted.
7. **`module_access.py::effective_module_access()`'s un-migrated raw
   role-string check** (§1 item 5, §3) — fold into this project's cleanup,
   or track separately as leftover Fix #3 debt?
8. **`require_ta_manager`** (§3) — confirmed dead code (no call sites).
   Retire, or keep for a near-future endpoint?

---

## Explicitly not done in this pass

No code, migration, or schema change. No implementation plan. The Platform
Admin control plane, `ats-hr`, and every fixed-role gate inventoried in §2
were read-only for this research — nothing was modified.
