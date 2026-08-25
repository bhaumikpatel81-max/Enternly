"""
Organisation management API (Company Admin).

Group Companies and Business Units are created/managed here rather than
being hard-coded in seed data, so a Company Admin can set them up for any org.

Group Companies
  GET    /api/org/group-companies          all (includes inactive) — admin view
  POST   /api/org/group-companies          create
  PATCH  /api/org/group-companies/{id}     rename / toggle active
  DELETE /api/org/group-companies/{id}     soft-delete (refuses if active BUs exist)

Business Units
  GET    /api/org/business-units           all (includes inactive) — admin view
  POST   /api/org/business-units           create
  PATCH  /api/org/business-units/{id}      rename / move company / toggle active
  DELETE /api/org/business-units/{id}      soft-delete (refuses if open reqs exist)
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user
from ..db import query, query_one
from ..module_access import recruiter_has_module

router = APIRouter(prefix="/api/org", tags=["organisation"])

_ADMIN_ROLES = {"admin", "platform_admin", "company_admin"}


def _require_admin(user: dict = Depends(get_current_user)) -> dict:
    role = user.get("role")
    if role in _ADMIN_ROLES:
        return user
    if role == "recruiter" and recruiter_has_module(user.get("sub"), "organisation"):
        return user
    raise HTTPException(403, "Company Admin only")


def _set_company_hrbps(company_id: str, user_ids: List[str], tenant_id: str) -> None:
    """Replace a group company's assigned-HRBP set wholesale (visibility
    fallback -- see scope_requisitions_for_hrbp in hrbp_api.py). Silently
    skips any user_id that doesn't belong to the caller's own tenant rather
    than erroring, since this is a multi-select populated from a
    same-tenant dropdown -- a mismatched id here would only come from a
    tampered request."""
    query("DELETE FROM app_user_company WHERE company_id = %s", [company_id], fetch=False)
    for uid in dict.fromkeys(user_ids):  # de-dupe, keep order
        if uid:
            query(
                """INSERT INTO app_user_company (user_id, company_id)
                   SELECT %s, %s WHERE EXISTS (
                       SELECT 1 FROM app_user WHERE id = %s AND tenant_id = %s
                   )
                   ON CONFLICT DO NOTHING""",
                [uid, company_id, uid, tenant_id],
                fetch=False,
            )


@router.get("/hrbp-users")
def list_hrbp_users(user: dict = Depends(_require_admin)):
    """Active app_user accounts with the hrbp role -- for the HRBP
    multi-select on the Organisation screen's group-company rows."""
    return query(
        "SELECT id, full_name, email FROM app_user WHERE role = 'hrbp' AND is_active = TRUE AND tenant_id = %s ORDER BY full_name",
        [user.get("tenant_id")],
    )


# ═══════════════════════════════════════════════════════════════════
#  GROUP COMPANIES
# ═══════════════════════════════════════════════════════════════════

_GC_COLS = """gc.id, gc.name, gc.domain, gc.is_active,
    COALESCE(
        (SELECT array_agg(u.id) FROM app_user_company auc JOIN app_user u ON u.id = auc.user_id WHERE auc.company_id = gc.id),
        ARRAY[]::uuid[]
    ) AS hrbp_ids,
    COALESCE(
        (SELECT array_agg(u.full_name ORDER BY u.full_name) FROM app_user_company auc JOIN app_user u ON u.id = auc.user_id WHERE auc.company_id = gc.id),
        ARRAY[]::text[]
    ) AS hrbp_names"""


@router.get("/group-companies")
def list_group_companies(user: dict = Depends(_require_admin)):
    return query(
        f"SELECT {_GC_COLS} FROM group_company gc WHERE gc.tenant_id = %s ORDER BY gc.name",
        [user.get("tenant_id")],
    )


class GCIn(BaseModel):
    name:          str
    domain:        Optional[str] = None
    hrbp_user_ids: List[str]     = []


@router.post("/group-companies")
def create_group_company(body: GCIn, user: dict = Depends(_require_admin)):
    tenant_id = user.get("tenant_id")
    dup = query_one(
        "SELECT id FROM group_company WHERE tenant_id = %s AND LOWER(name)=LOWER(%s)",
        [tenant_id, body.name],
    )
    if dup:
        raise HTTPException(409, f"Group company '{body.name}' already exists")
    new_id = query_one(
        """INSERT INTO group_company (name, domain, tenant_id)
           VALUES (%s, %s, %s) RETURNING id""",
        [body.name.strip(), (body.domain or "").strip() or None, tenant_id],
    )["id"]
    if body.hrbp_user_ids:
        _set_company_hrbps(new_id, body.hrbp_user_ids, tenant_id)
    return query_one(f"SELECT {_GC_COLS} FROM group_company gc WHERE gc.id=%s", [new_id])


class GCPatch(BaseModel):
    name:          Optional[str]       = None
    domain:        Optional[str]       = None
    is_active:     Optional[bool]      = None
    hrbp_user_ids: Optional[List[str]] = None


@router.patch("/group-companies/{gc_id}")
def patch_group_company(gc_id: str, body: GCPatch, user: dict = Depends(_require_admin)):
    tenant_id = user.get("tenant_id")
    gc = query_one("SELECT id FROM group_company WHERE id=%s AND tenant_id=%s", [gc_id, tenant_id])
    if not gc:
        raise HTTPException(404, "Group company not found")

    sets, vals = [], []
    if body.name is not None:
        dup = query_one(
            "SELECT id FROM group_company WHERE tenant_id=%s AND LOWER(name)=LOWER(%s) AND id<>%s",
            [tenant_id, body.name, gc_id],
        )
        if dup:
            raise HTTPException(409, f"Name '{body.name}' already in use")
        sets.append("name=%s"); vals.append(body.name.strip())
    if body.domain is not None:
        sets.append("domain=%s"); vals.append(body.domain.strip() or None)
    if body.is_active is not None:
        sets.append("is_active=%s"); vals.append(body.is_active)

    if sets:
        vals.append(gc_id)
        query(f"UPDATE group_company SET {', '.join(sets)} WHERE id=%s", vals, fetch=False)
    if body.hrbp_user_ids is not None:
        _set_company_hrbps(gc_id, body.hrbp_user_ids, tenant_id)

    return query_one(f"SELECT {_GC_COLS} FROM group_company gc WHERE gc.id=%s", [gc_id])


@router.delete("/group-companies/{gc_id}")
def delete_group_company(gc_id: str, user: dict = Depends(_require_admin)):
    gc = query_one("SELECT id, name FROM group_company WHERE id=%s AND tenant_id=%s", [gc_id, user.get("tenant_id")])
    if not gc:
        raise HTTPException(404, "Group company not found")

    active_bu_count = query_one(
        "SELECT COUNT(*) AS n FROM business_unit WHERE company_id=%s AND is_active=true",
        [gc_id],
    )
    if active_bu_count and active_bu_count["n"] > 0:
        raise HTTPException(
            409,
            f"Cannot deactivate '{gc['name']}' — it has {active_bu_count['n']} active business unit(s). "
            "Deactivate all its BUs first.",
        )

    query("UPDATE group_company SET is_active=FALSE WHERE id=%s", [gc_id], fetch=False)
    return {"ok": True, "action": "deactivated"}


# ═══════════════════════════════════════════════════════════════════
#  BUSINESS UNITS
# ═══════════════════════════════════════════════════════════════════

@router.get("/business-units")
def list_business_units(user: dict = Depends(_require_admin)):
    return query(
        """SELECT bu.id, bu.name, bu.is_active,
                  gc.id AS company_id, gc.name AS company
           FROM business_unit bu
           JOIN group_company gc ON gc.id = bu.company_id
           WHERE gc.tenant_id = %s
           ORDER BY gc.name, bu.name""",
        [user.get("tenant_id")],
    )


class BUIn(BaseModel):
    name:       str
    company_id: str


@router.post("/business-units")
def create_business_unit(body: BUIn, user: dict = Depends(_require_admin)):
    gc = query_one(
        "SELECT id FROM group_company WHERE id=%s AND tenant_id=%s AND is_active=true",
        [body.company_id, user.get("tenant_id")],
    )
    if not gc:
        raise HTTPException(404, "Group company not found or inactive")

    dup = query_one(
        "SELECT id FROM business_unit WHERE LOWER(name)=LOWER(%s) AND company_id=%s",
        [body.name, body.company_id],
    )
    if dup:
        raise HTTPException(409, f"BU '{body.name}' already exists in this company")

    row = query_one(
        """INSERT INTO business_unit (company_id, name)
           VALUES (%s, %s)
           RETURNING id, name, company_id, is_active""",
        [body.company_id, body.name.strip()],
    )
    return dict(row)


class BUPatch(BaseModel):
    name:       Optional[str]  = None
    company_id: Optional[str]  = None
    is_active:  Optional[bool] = None


@router.patch("/business-units/{bu_id}")
def patch_business_unit(bu_id: str, body: BUPatch, user: dict = Depends(_require_admin)):
    tenant_id = user.get("tenant_id")
    bu = query_one(
        """SELECT bu.id, bu.name, bu.company_id FROM business_unit bu
           JOIN group_company gc ON gc.id = bu.company_id
           WHERE bu.id=%s AND gc.tenant_id=%s""",
        [bu_id, tenant_id],
    )
    if not bu:
        raise HTTPException(404, "Business unit not found")

    sets, vals = [], []
    company_id = body.company_id or bu["company_id"]

    if body.name is not None:
        dup = query_one(
            "SELECT id FROM business_unit WHERE LOWER(name)=LOWER(%s) AND company_id=%s AND id<>%s",
            [body.name, company_id, bu_id],
        )
        if dup:
            raise HTTPException(409, f"BU name '{body.name}' already exists in this company")
        sets.append("name=%s"); vals.append(body.name.strip())
    if body.company_id is not None:
        gc = query_one(
            "SELECT id FROM group_company WHERE id=%s AND tenant_id=%s AND is_active=true",
            [body.company_id, tenant_id],
        )
        if not gc:
            raise HTTPException(404, "Group company not found or inactive")
        sets.append("company_id=%s"); vals.append(body.company_id)
    if body.is_active is not None:
        sets.append("is_active=%s"); vals.append(body.is_active)

    if sets:
        vals.append(bu_id)
        query(f"UPDATE business_unit SET {', '.join(sets)} WHERE id=%s", vals, fetch=False)

    return query_one(
        """SELECT bu.id, bu.name, bu.is_active, gc.id AS company_id, gc.name AS company
           FROM business_unit bu JOIN group_company gc ON gc.id=bu.company_id
           WHERE bu.id=%s""",
        [bu_id],
    )


@router.delete("/business-units/{bu_id}")
def delete_business_unit(bu_id: str, user: dict = Depends(_require_admin)):
    bu = query_one(
        """SELECT bu.id, bu.name FROM business_unit bu
           JOIN group_company gc ON gc.id = bu.company_id
           WHERE bu.id=%s AND gc.tenant_id=%s""",
        [bu_id, user.get("tenant_id")],
    )
    if not bu:
        raise HTTPException(404, "Business unit not found")

    open_reqs = query_one(
        "SELECT COUNT(*) AS n FROM requisition WHERE bu_id=%s AND status='open'",
        [bu_id],
    )
    if open_reqs and open_reqs["n"] > 0:
        raise HTTPException(
            409,
            f"Cannot deactivate '{bu['name']}' — it has {open_reqs['n']} open requisition(s).",
        )

    query("UPDATE business_unit SET is_active=FALSE WHERE id=%s", [bu_id], fetch=False)
    return {"ok": True, "action": "deactivated"}
