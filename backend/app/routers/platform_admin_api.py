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
