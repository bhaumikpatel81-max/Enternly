"""
Platform-admin control plane: the ONLY place in Enternly that deliberately
reads/writes across tenants. Every route here requires require_platform_admin
(is_platform_superadmin = TRUE), applied once at the router level so no
individual route can accidentally be added without it.

Feature C (this file, commit 3): tenant dashboard stats + tenant CRUD,
including first-company-admin creation and College-tenant placement
officers. Later commits extend this same file with modules, subscriptions,
all-users/impersonation, tickets, audit, analytics, system health, and
settings -- see PLATFORM_ADMIN_MAPPING.md for the full endpoint map.
"""
import json
import re
from datetime import date
from typing import List, Optional

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import query, query_one, transaction, tx_exec
from ..auth_utils import hash_password, require_platform_admin
from ..services.activity_log import log_activity

router = APIRouter(prefix="/api/platform", tags=["platform-admin"], dependencies=[Depends(require_platform_admin)])

_SEED_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "tenant"


def _unique_slug(base: str) -> str:
    slug = base
    n = 2
    while query_one("SELECT id FROM tenant WHERE slug = %s", [slug]):
        slug = f"{base}-{n}"
        n += 1
    return slug


def _allocate_tenant_code(cur) -> str:
    """Next ET_NNNN in sequence -- re-run inside the CURRENT transaction
    attempt every time this is called (including on retry after a collision)
    so a retry never re-derives the same colliding code from stale state."""
    rows = tx_exec(cur, "SELECT tenant_code FROM tenant WHERE tenant_code ~ '^ET_[0-9]{4}$' ORDER BY tenant_code DESC LIMIT 1")
    last = rows[0]["tenant_code"] if rows else None
    n = int(last[3:]) + 1 if last else 1
    return f"ET_{n:04d}"


def _tenant_row_out(row: dict) -> dict:
    """Shape a `tenant` row for the API: `plan` is aliased to
    `subscription_plan` here (decision 2 -- the column itself is never
    renamed/duplicated in SQL)."""
    if row is None:
        return None
    out = dict(row)
    out["subscription_plan"] = out.pop("plan", None)
    return out


_TENANT_COLS = """id, name, slug, status, plan, primary_contact_email, created_at,
    tenant_type, tenant_code, logo_url, primary_colour,
    subscription_start_date, subscription_end_date, grace_period_days, is_deleted"""


# ── Dashboard stats (Feature B) ───────────────────────────────────────────────

@router.get("/stats")
def platform_stats():
    total_tenants = query_one("SELECT COUNT(*) AS n FROM tenant WHERE NOT is_deleted")["n"]
    active_tenants = query_one(
        "SELECT COUNT(*) AS n FROM tenant WHERE NOT is_deleted AND status = 'active'"
    )["n"]
    # Platform superadmins are Enternstech staff, not a customer's headcount.
    total_users = query_one(
        "SELECT COUNT(*) AS n FROM app_user WHERE NOT is_platform_superadmin"
    )["n"]
    new_this_month = query_one(
        "SELECT COUNT(*) AS n FROM tenant WHERE NOT is_deleted "
        "AND created_at >= date_trunc('month', now())"
    )["n"]
    # Overview pool count only (decision 4) -- no per-candidate detail
    # anywhere in the platform console. `candidate` carries tenant_id
    # (Migration 96) so this is a plain cross-tenant count.
    total_candidates = query_one("SELECT COUNT(*) AS n FROM candidate")["n"]
    return {
        "totalTenants": total_tenants,
        "activeTenants": active_tenants,
        "totalUsers": total_users,
        "newTenantsThisMonth": new_this_month,
        "totalCandidates": total_candidates,
    }


# ── Tenant CRUD (Feature C) ───────────────────────────────────────────────────

class CreateTenantIn(BaseModel):
    name: str
    tenant_type: str = "Company"
    primary_contact_email: Optional[str] = None
    plan: str = "standard"
    admin_full_name: str
    admin_email: str
    admin_password: Optional[str] = None
    send_setup_email: bool = True


class UpdateTenantIn(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    logo_url: Optional[str] = None
    primary_colour: Optional[str] = None
    primary_contact_email: Optional[str] = None


class TenantStatusIn(BaseModel):
    status: str  # 'active' | 'trial' | 'suspended'
    plan: Optional[str] = None
    subscription_end_date: Optional[date] = None


class TenantAdminIn(BaseModel):
    full_name: str
    email: str
    password: Optional[str] = None
    send_setup_email: bool = True


@router.get("/tenants")
def list_tenants(type: Optional[str] = Query(None, alias="type")):
    conds = ["NOT t.is_deleted"]
    params: List = []
    if type:
        conds.append("t.tenant_type = %s")
        params.append(type)
    where = "WHERE " + " AND ".join(conds)
    rows = query(
        f"""SELECT {_TENANT_COLS},
                   (SELECT COUNT(*) FROM app_user u WHERE u.tenant_id = t.id) AS employee_count
            FROM tenant t {where}
            ORDER BY t.created_at DESC""",
        params,
    )
    return [_tenant_row_out(r) for r in rows]


@router.post("/tenants", status_code=201)
def create_tenant(body: CreateTenantIn, actor=Depends(require_platform_admin)):
    if body.tenant_type not in ("Company", "College"):
        raise HTTPException(400, "tenant_type must be 'Company' or 'College'")
    if body.admin_password and len(body.admin_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    from ..services.email_validation import assert_real_email
    try:
        admin_email = assert_real_email(body.admin_email)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if query_one("SELECT id FROM app_user WHERE email = %s", [admin_email]):
        raise HTTPException(400, "A user with that email already exists")

    base_slug = _slugify(body.name)
    pwd_hash = hash_password(body.admin_password) if body.admin_password else None

    # Tenant + its first company admin are created atomically -- a failure
    # partway through (e.g. a last-second email collision) must never leave
    # an orphaned tenant holding a consumed ET_NNNN code.
    tenant_row = None
    admin_id = None
    for attempt in (1, 2):
        slug = _unique_slug(base_slug) if attempt == 1 else _unique_slug(f"{base_slug}-{attempt}")
        try:
            with transaction() as cur:
                tenant_code = _allocate_tenant_code(cur)
                tenant_rows = tx_exec(
                    cur,
                    f"""INSERT INTO tenant (name, slug, plan, primary_contact_email, tenant_type, tenant_code)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING {_TENANT_COLS}""",
                    [body.name.strip(), slug, body.plan, body.primary_contact_email,
                     body.tenant_type, tenant_code],
                )
                tenant_row = tenant_rows[0]
                admin_rows = tx_exec(
                    cur,
                    """INSERT INTO app_user (full_name, email, role, password_hash, tenant_id,
                                              is_company_admin, created_by)
                       VALUES (%s, %s, 'admin', %s, %s, TRUE, %s) RETURNING id""",
                    [body.admin_full_name, admin_email, pwd_hash, tenant_row["id"], actor.get("sub")],
                )
                admin_id = admin_rows[0]["id"]
            break
        except psycopg2.errors.UniqueViolation:
            if attempt == 2:
                raise HTTPException(409, "Could not allocate a unique tenant code/slug — please retry")
            continue

    log_activity(
        "tenant", "create", entity_id=str(tenant_row["id"]),
        actor_id=actor.get("sub"), actor_role=actor.get("role"),
        to_value=body.name, detail={"tenant_code": tenant_row["tenant_code"], "tenant_type": body.tenant_type},
    )

    setup_email_sent = False
    if body.send_setup_email:
        try:
            from .password_api import _issue_token, _send_link_email
            raw = _issue_token(str(admin_id), "invite")
            _send_link_email(admin_email, body.admin_full_name, raw, "invite", tenant_id=str(tenant_row["id"]))
            setup_email_sent = True
        except Exception as exc:
            print(f"[create_tenant] setup email failed: {exc}")

    return {**_tenant_row_out(tenant_row), "admin_user_id": str(admin_id), "setup_email_sent": setup_email_sent}


@router.get("/tenants/{tenant_id}")
def get_tenant(tenant_id: str):
    row = query_one(f"SELECT {_TENANT_COLS} FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id])
    if not row:
        raise HTTPException(404, "Tenant not found")
    users = query(
        "SELECT id, full_name, email, role, is_active, is_company_admin, last_login_at, created_at "
        "FROM app_user WHERE tenant_id = %s ORDER BY created_at DESC",
        [tenant_id],
    )
    return {**_tenant_row_out(row), "users": users}


@router.put("/tenants/{tenant_id}")
def update_tenant(tenant_id: str, body: UpdateTenantIn, actor=Depends(require_platform_admin)):
    existing = query_one("SELECT id FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id])
    if not existing:
        raise HTTPException(404, "Tenant not found")
    sets, params = [], []
    if body.name is not None:
        sets.append("name = %s"); params.append(body.name.strip())
    if body.slug is not None:
        new_slug = _slugify(body.slug)
        if query_one("SELECT id FROM tenant WHERE slug = %s AND id <> %s", [new_slug, tenant_id]):
            raise HTTPException(400, "That slug is already in use")
        sets.append("slug = %s"); params.append(new_slug)
    if body.logo_url is not None:
        sets.append("logo_url = %s"); params.append(body.logo_url)
    if body.primary_colour is not None:
        sets.append("primary_colour = %s"); params.append(body.primary_colour)
    if body.primary_contact_email is not None:
        sets.append("primary_contact_email = %s"); params.append(body.primary_contact_email)
    if not sets:
        return _tenant_row_out(query_one(f"SELECT {_TENANT_COLS} FROM tenant WHERE id = %s", [tenant_id]))
    params.append(tenant_id)
    query(f"UPDATE tenant SET {', '.join(sets)} WHERE id = %s", params, fetch=False)
    return _tenant_row_out(query_one(f"SELECT {_TENANT_COLS} FROM tenant WHERE id = %s", [tenant_id]))


@router.patch("/tenants/{tenant_id}/status")
def set_tenant_status(tenant_id: str, body: TenantStatusIn, actor=Depends(require_platform_admin)):
    if body.status not in ("active", "trial", "suspended"):
        raise HTTPException(400, "status must be 'active', 'trial', or 'suspended'")
    existing = query_one("SELECT status FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id])
    if not existing:
        raise HTTPException(404, "Tenant not found")
    sets, params = ["status = %s"], [body.status]
    if body.plan is not None:
        sets.append("plan = %s"); params.append(body.plan)
    if body.subscription_end_date is not None:
        sets.append("subscription_end_date = %s"); params.append(body.subscription_end_date)
    params.append(tenant_id)
    query(f"UPDATE tenant SET {', '.join(sets)} WHERE id = %s", params, fetch=False)
    log_activity(
        "tenant", "status_change", entity_id=tenant_id,
        actor_id=actor.get("sub"), actor_role=actor.get("role"),
        from_value=existing["status"], to_value=body.status,
    )
    return _tenant_row_out(query_one(f"SELECT {_TENANT_COLS} FROM tenant WHERE id = %s", [tenant_id]))


@router.delete("/tenants/{tenant_id}")
def delete_tenant(tenant_id: str, actor=Depends(require_platform_admin)):
    """Soft delete only -- tenant rows are never hard-deleted. Its users
    are immediately blocked from login (auth.py::login and
    _refresh_staff_claims both check tenant.is_deleted)."""
    existing = query_one("SELECT id, name FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id])
    if not existing:
        raise HTTPException(404, "Tenant not found")
    if tenant_id == _SEED_TENANT_ID:
        raise HTTPException(400, "Cannot delete the Enternstech platform tenant")
    query("UPDATE tenant SET is_deleted = TRUE WHERE id = %s", [tenant_id], fetch=False)
    log_activity(
        "tenant", "delete", entity_id=tenant_id,
        actor_id=actor.get("sub"), actor_role=actor.get("role"), from_value=existing["name"],
    )
    return {"ok": True}


@router.get("/tenants/{tenant_id}/users")
def list_tenant_users(tenant_id: str):
    if not query_one("SELECT id FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id]):
        raise HTTPException(404, "Tenant not found")
    return query(
        "SELECT id, full_name, email, role, is_active, is_company_admin, last_login_at, created_at "
        "FROM app_user WHERE tenant_id = %s ORDER BY created_at DESC",
        [tenant_id],
    )


@router.post("/tenants/{tenant_id}/admins", status_code=201)
def add_tenant_admin(tenant_id: str, body: TenantAdminIn, actor=Depends(require_platform_admin)):
    tenant = query_one("SELECT id FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id])
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if body.password and len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    from ..services.email_validation import assert_real_email
    try:
        email = assert_real_email(body.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if query_one("SELECT id FROM app_user WHERE email = %s", [email]):
        raise HTTPException(400, "A user with that email already exists")
    pwd_hash = hash_password(body.password) if body.password else None
    new_id = query_one(
        """INSERT INTO app_user (full_name, email, role, password_hash, tenant_id, is_company_admin, created_by)
           VALUES (%s, %s, 'admin', %s, %s, TRUE, %s) RETURNING id""",
        [body.full_name, email, pwd_hash, tenant_id, actor.get("sub")],
    )["id"]
    setup_email_sent = False
    if body.send_setup_email:
        try:
            from .password_api import _issue_token, _send_link_email
            raw = _issue_token(str(new_id), "invite")
            _send_link_email(email, body.full_name, raw, "invite", tenant_id=tenant_id)
            setup_email_sent = True
        except Exception as exc:
            print(f"[add_tenant_admin] setup email failed: {exc}")
    row = query_one(
        "SELECT id, full_name, email, role, is_active, is_company_admin FROM app_user WHERE id = %s",
        [new_id],
    )
    return {**row, "setup_email_sent": setup_email_sent}


@router.post("/tenants/{tenant_id}/placement-officers", status_code=201)
def add_placement_officer(tenant_id: str, body: TenantAdminIn, actor=Depends(require_platform_admin)):
    """A College tenant's own campus-recruiting contact -- scoped to this
    one tenant like any other role, never platform- or company-admin scope."""
    tenant = query_one("SELECT id, tenant_type FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id])
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if tenant["tenant_type"] != "College":
        raise HTTPException(400, "Placement officers can only be added to College tenants")
    if body.password and len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    from ..services.email_validation import assert_real_email
    try:
        email = assert_real_email(body.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if query_one("SELECT id FROM app_user WHERE email = %s", [email]):
        raise HTTPException(400, "A user with that email already exists")
    pwd_hash = hash_password(body.password) if body.password else None
    new_id = query_one(
        """INSERT INTO app_user (full_name, email, role, password_hash, tenant_id, created_by)
           VALUES (%s, %s, 'placement_officer', %s, %s, %s) RETURNING id""",
        [body.full_name, email, pwd_hash, tenant_id, actor.get("sub")],
    )["id"]
    setup_email_sent = False
    if body.send_setup_email:
        try:
            from .password_api import _issue_token, _send_link_email
            raw = _issue_token(str(new_id), "invite")
            _send_link_email(email, body.full_name, raw, "invite", tenant_id=tenant_id)
            setup_email_sent = True
        except Exception as exc:
            print(f"[add_placement_officer] setup email failed: {exc}")
    row = query_one(
        "SELECT id, full_name, email, role, is_active FROM app_user WHERE id = %s", [new_id],
    )
    return {**row, "setup_email_sent": setup_email_sent}


# ── Module catalog + per-tenant gating (Feature D) ────────────────────────────

class ModuleCatalogIn(BaseModel):
    key: str
    label: str
    group: Optional[str] = None
    default_route: Optional[str] = None
    icon: Optional[str] = None


class ModuleCatalogUpdateIn(BaseModel):
    label: Optional[str] = None
    group: Optional[str] = None
    default_route: Optional[str] = None
    icon: Optional[str] = None
    is_active: Optional[bool] = None


class TenantModulesIn(BaseModel):
    modules: dict  # {module_key: bool}


def _plan_allowed_modules(plan_name: Optional[str]) -> Optional[List[str]]:
    """None means 'no restriction' (empty allowed_modules_json, or the plan
    isn't in subscription_plan_config at all -- e.g. a legacy/custom plan
    string). A real list means only those keys may be enabled."""
    if not plan_name:
        return None
    row = query_one("SELECT allowed_modules_json FROM subscription_plan_config WHERE plan_name = %s", [plan_name])
    if not row or not row["allowed_modules_json"]:
        return None
    return list(row["allowed_modules_json"])


@router.get("/modules")
def list_modules():
    return query("SELECT * FROM module_catalog ORDER BY \"group\", label")


@router.post("/modules", status_code=201)
def create_module(body: ModuleCatalogIn):
    if query_one("SELECT key FROM module_catalog WHERE key = %s", [body.key]):
        raise HTTPException(400, "That module key already exists")
    query(
        """INSERT INTO module_catalog (key, label, "group", default_route, icon)
           VALUES (%s, %s, %s, %s, %s)""",
        [body.key, body.label, body.group, body.default_route, body.icon],
        fetch=False,
    )
    return query_one("SELECT * FROM module_catalog WHERE key = %s", [body.key])


@router.put("/modules/{module_key}")
def update_module(module_key: str, body: ModuleCatalogUpdateIn):
    if not query_one("SELECT key FROM module_catalog WHERE key = %s", [module_key]):
        raise HTTPException(404, "Module not found")
    sets, params = [], []
    for field, col in (("label", "label"), ("group", '"group"'), ("default_route", "default_route"),
                        ("icon", "icon"), ("is_active", "is_active")):
        val = getattr(body, field)
        if val is not None:
            sets.append(f"{col} = %s"); params.append(val)
    if sets:
        params.append(module_key)
        query(f"UPDATE module_catalog SET {', '.join(sets)} WHERE key = %s", params, fetch=False)
    return query_one("SELECT * FROM module_catalog WHERE key = %s", [module_key])


@router.delete("/modules/{module_key}")
def disable_module(module_key: str):
    """Soft-disable only -- never hard-deleted (tenant_module_config rows
    reference it, and disabling here just stops it appearing as an option
    for new toggles; tenants that already have it enabled keep working)."""
    if not query_one("SELECT key FROM module_catalog WHERE key = %s", [module_key]):
        raise HTTPException(404, "Module not found")
    query("UPDATE module_catalog SET is_active = FALSE WHERE key = %s", [module_key], fetch=False)
    return {"ok": True}


@router.get("/tenants/{tenant_id}/modules")
def get_tenant_modules(tenant_id: str):
    if not query_one("SELECT id FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id]):
        raise HTTPException(404, "Tenant not found")
    rows = query(
        """SELECT m.key, m.label, m."group", m.icon,
                  COALESCE(c.is_enabled, TRUE) AS is_enabled
           FROM module_catalog m
           LEFT JOIN tenant_module_config c ON c.tenant_id = %s AND c.module_key = m.key
           WHERE m.is_active
           ORDER BY m."group", m.label""",
        [tenant_id],
    )
    return rows


@router.put("/tenants/{tenant_id}/modules")
def set_tenant_modules(tenant_id: str, body: TenantModulesIn, actor=Depends(require_platform_admin)):
    tenant = query_one("SELECT id, plan FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id])
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    allowed = _plan_allowed_modules(tenant["plan"])
    valid_keys = {r["key"] for r in query("SELECT key FROM module_catalog") or []}
    for key, enabled in body.modules.items():
        if key not in valid_keys:
            raise HTTPException(400, f"Unknown module '{key}'")
        if enabled and allowed is not None and key not in allowed:
            raise HTTPException(400, f"'{key}' is not included in this tenant's '{tenant['plan']}' plan")
    for key, enabled in body.modules.items():
        query(
            """INSERT INTO tenant_module_config (tenant_id, module_key, is_enabled, enabled_at, disabled_at)
               VALUES (%s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END, CASE WHEN %s THEN NULL ELSE now() END)
               ON CONFLICT (tenant_id, module_key) DO UPDATE
                 SET is_enabled = EXCLUDED.is_enabled,
                     enabled_at = CASE WHEN EXCLUDED.is_enabled THEN now() ELSE tenant_module_config.enabled_at END,
                     disabled_at = CASE WHEN EXCLUDED.is_enabled THEN NULL ELSE now() END,
                     updated_at = now()""",
            [tenant_id, key, enabled, enabled, enabled],
            fetch=False,
        )
        log_activity(
            "tenant_module", "toggle", entity_id=tenant_id,
            actor_id=actor.get("sub"), actor_role=actor.get("role"),
            to_value=key, detail={"enabled": enabled},
        )
    return get_tenant_modules(tenant_id)


# ── Subscriptions & plans (Feature E) ─────────────────────────────────────────

class SubscriptionPlanIn(BaseModel):
    plan_name: str
    allowed_modules_json: List[str] = []
    price_monthly: Optional[float] = None
    price_yearly: Optional[float] = None


class SubscriptionPlanUpdateIn(BaseModel):
    allowed_modules_json: Optional[List[str]] = None
    price_monthly: Optional[float] = None
    price_yearly: Optional[float] = None


class TenantSubscriptionIn(BaseModel):
    plan: str
    subscription_start_date: Optional[date] = None
    subscription_end_date: Optional[date] = None


class GraceConfigIn(BaseModel):
    grace_period_days: int


@router.get("/subscription-plans")
def list_subscription_plans():
    return query("SELECT * FROM subscription_plan_config ORDER BY plan_name")


@router.post("/subscription-plans", status_code=201)
def create_subscription_plan(body: SubscriptionPlanIn):
    if query_one("SELECT plan_name FROM subscription_plan_config WHERE plan_name = %s", [body.plan_name]):
        raise HTTPException(400, "That plan already exists")
    query(
        """INSERT INTO subscription_plan_config (plan_name, allowed_modules_json, price_monthly, price_yearly)
           VALUES (%s, %s::jsonb, %s, %s)""",
        [body.plan_name, json.dumps(body.allowed_modules_json), body.price_monthly, body.price_yearly],
        fetch=False,
    )
    return query_one("SELECT * FROM subscription_plan_config WHERE plan_name = %s", [body.plan_name])


@router.put("/subscription-plans/{plan_name}")
def update_subscription_plan(plan_name: str, body: SubscriptionPlanUpdateIn):
    if not query_one("SELECT plan_name FROM subscription_plan_config WHERE plan_name = %s", [plan_name]):
        raise HTTPException(404, "Plan not found")
    sets, params = [], []
    if body.allowed_modules_json is not None:
        sets.append("allowed_modules_json = %s::jsonb"); params.append(json.dumps(body.allowed_modules_json))
    if body.price_monthly is not None:
        sets.append("price_monthly = %s"); params.append(body.price_monthly)
    if body.price_yearly is not None:
        sets.append("price_yearly = %s"); params.append(body.price_yearly)
    if sets:
        sets.append("updated_at = now()")
        params.append(plan_name)
        query(f"UPDATE subscription_plan_config SET {', '.join(sets)} WHERE plan_name = %s", params, fetch=False)
    return query_one("SELECT * FROM subscription_plan_config WHERE plan_name = %s", [plan_name])


@router.delete("/subscription-plans/{plan_name}")
def delete_subscription_plan(plan_name: str):
    if plan_name == "standard":
        raise HTTPException(400, "Cannot delete the default 'standard' plan")
    if query_one("SELECT id FROM tenant WHERE plan = %s AND NOT is_deleted", [plan_name]):
        raise HTTPException(409, "This plan is still assigned to at least one company")
    query("DELETE FROM subscription_plan_config WHERE plan_name = %s", [plan_name], fetch=False)
    return {"ok": True}


@router.get("/subscriptions")
def list_subscriptions():
    rows = query(
        f"""SELECT {_TENANT_COLS},
                   (SELECT u.full_name || ' <' || u.email || '>' FROM app_user u
                    WHERE u.tenant_id = t.id AND u.is_company_admin AND u.is_active
                    ORDER BY u.created_at LIMIT 1) AS admin_contact,
                   (t.subscription_end_date - CURRENT_DATE) AS days_remaining
            FROM tenant t WHERE NOT t.is_deleted ORDER BY t.subscription_end_date NULLS LAST"""
    )
    return [_tenant_row_out(r) for r in rows]


@router.put("/tenants/{tenant_id}/subscription")
def set_tenant_subscription(tenant_id: str, body: TenantSubscriptionIn, actor=Depends(require_platform_admin)):
    tenant = query_one("SELECT plan FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id])
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    query(
        "UPDATE tenant SET plan = %s, subscription_start_date = %s, subscription_end_date = %s WHERE id = %s",
        [body.plan, body.subscription_start_date, body.subscription_end_date, tenant_id],
        fetch=False,
    )
    log_activity(
        "tenant", "subscription_change", entity_id=tenant_id,
        actor_id=actor.get("sub"), actor_role=actor.get("role"),
        from_value=tenant["plan"], to_value=body.plan,
    )
    # Live constraint: auto-disable any currently-enabled module the new
    # plan doesn't cover, rather than leaving the tenant in a state its own
    # plan no longer allows.
    allowed = _plan_allowed_modules(body.plan)
    if allowed is not None:
        enabled_rows = query(
            "SELECT module_key FROM tenant_module_config WHERE tenant_id = %s AND is_enabled",
            [tenant_id],
        ) or []
        for r in enabled_rows:
            if r["module_key"] not in allowed:
                query(
                    "UPDATE tenant_module_config SET is_enabled = FALSE, disabled_at = now(), updated_at = now() "
                    "WHERE tenant_id = %s AND module_key = %s",
                    [tenant_id, r["module_key"]], fetch=False,
                )
                log_activity(
                    "tenant_module", "auto_disabled_by_plan_change", entity_id=tenant_id,
                    actor_id=actor.get("sub"), actor_role=actor.get("role"),
                    to_value=r["module_key"], detail={"new_plan": body.plan},
                )
    return _tenant_row_out(query_one(f"SELECT {_TENANT_COLS} FROM tenant WHERE id = %s", [tenant_id]))


@router.post("/tenants/{tenant_id}/send-renewal-reminder")
def send_renewal_reminder(tenant_id: str, actor=Depends(require_platform_admin)):
    tenant = query_one(
        "SELECT id, name, plan, subscription_end_date FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id],
    )
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    admin = query_one(
        "SELECT full_name, email FROM app_user WHERE tenant_id = %s AND is_company_admin AND is_active "
        "ORDER BY created_at LIMIT 1",
        [tenant_id],
    )
    if not admin:
        raise HTTPException(404, "This company has no active company admin to notify")
    from ..services.email_layout import build_branded_email
    from ..services.connectors import send_email
    end_date_str = tenant["subscription_end_date"].strftime("%d %b %Y") if tenant["subscription_end_date"] else "soon"
    html = build_branded_email(
        eyebrow="Enternly — Subscription",
        hero_title_html=f"Your subscription renews {end_date_str}",
        hero_subtitle=f"Hi {admin['full_name']}, this is a reminder that {tenant['name']}'s Enternly subscription "
                       f"({tenant['plan']} plan) is due for renewal on {end_date_str}.",
        footer_note="Questions about your plan or billing? Simply reply to this email.",
    )
    send_email(
        admin["email"], f"Enternly subscription renewal — {tenant['name']}",
        f"Hi {admin['full_name']}, your Enternly subscription ({tenant['plan']} plan) renews on {end_date_str}.",
        html=html, tenant_id=tenant_id,
    )
    log_activity(
        "tenant", "renewal_reminder_sent", entity_id=tenant_id,
        actor_id=actor.get("sub"), actor_role=actor.get("role"), to_value=admin["email"],
    )
    return {"ok": True, "sent_to": admin["email"]}


@router.get("/tenants/{tenant_id}/grace-config")
def get_grace_config(tenant_id: str):
    row = query_one("SELECT grace_period_days FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id])
    if not row:
        raise HTTPException(404, "Tenant not found")
    return row


@router.put("/tenants/{tenant_id}/grace-config")
def set_grace_config(tenant_id: str, body: GraceConfigIn):
    if body.grace_period_days < 0:
        raise HTTPException(400, "grace_period_days cannot be negative")
    if not query_one("SELECT id FROM tenant WHERE id = %s AND NOT is_deleted", [tenant_id]):
        raise HTTPException(404, "Tenant not found")
    query("UPDATE tenant SET grace_period_days = %s WHERE id = %s", [body.grace_period_days, tenant_id], fetch=False)
    return {"grace_period_days": body.grace_period_days}
