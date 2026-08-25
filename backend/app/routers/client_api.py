"""
Clients — for a customer company that hires on behalf of external clients
(a staffing / RPO agency use case), not only for its own internal roles.

A requisition's client_id is optional: NULL means an internal hire; set means
the requisition is being worked on behalf of that client. Every client is
scoped to the tenant that created it -- one customer never sees another's
client roster.

GET    /api/clients          — list (any staff role; used to populate the
                                "Hiring for" picker on the requisition form)
POST   /api/clients          — create (Company Admin)
PATCH  /api/clients/{id}     — rename / toggle active (Company Admin)
DELETE /api/clients/{id}     — soft-deactivate; refuses if open requisitions
                                still reference it
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user, require_company_admin
from ..db import query, query_one

router = APIRouter(prefix="/api/clients", tags=["clients"])


@router.get("")
def list_clients(user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    if not tenant_id:
        return []
    return query(
        "SELECT id, name, is_active FROM client WHERE tenant_id = %s ORDER BY name",
        [tenant_id],
    )


class ClientIn(BaseModel):
    name: str


@router.post("", status_code=201)
def create_client(body: ClientIn, user: dict = Depends(require_company_admin)):
    tenant_id = user.get("tenant_id")
    if not tenant_id:
        raise HTTPException(400, "No tenant on this account")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Client name is required")
    dup = query_one(
        "SELECT id FROM client WHERE tenant_id = %s AND LOWER(name) = LOWER(%s)",
        [tenant_id, name],
    )
    if dup:
        raise HTTPException(409, f"Client '{name}' already exists")
    return query_one(
        "INSERT INTO client (tenant_id, name) VALUES (%s, %s) RETURNING id, name, is_active",
        [tenant_id, name],
    )


class ClientPatch(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


@router.patch("/{client_id}")
def patch_client(client_id: str, body: ClientPatch, user: dict = Depends(require_company_admin)):
    tenant_id = user.get("tenant_id")
    existing = query_one(
        "SELECT id FROM client WHERE id = %s AND tenant_id = %s", [client_id, tenant_id]
    )
    if not existing:
        raise HTTPException(404, "Client not found")

    sets, vals = [], []
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Client name cannot be empty")
        dup = query_one(
            "SELECT id FROM client WHERE tenant_id = %s AND LOWER(name) = LOWER(%s) AND id <> %s",
            [tenant_id, name, client_id],
        )
        if dup:
            raise HTTPException(409, f"Client '{name}' already exists")
        sets.append("name = %s"); vals.append(name)
    if body.is_active is not None:
        sets.append("is_active = %s"); vals.append(body.is_active)
    if not sets:
        raise HTTPException(400, "Nothing to update")

    vals.append(client_id)
    return query_one(
        f"UPDATE client SET {', '.join(sets)} WHERE id = %s RETURNING id, name, is_active",
        vals,
    )


@router.delete("/{client_id}")
def delete_client(client_id: str, user: dict = Depends(require_company_admin)):
    tenant_id = user.get("tenant_id")
    existing = query_one(
        "SELECT id, name FROM client WHERE id = %s AND tenant_id = %s", [client_id, tenant_id]
    )
    if not existing:
        raise HTTPException(404, "Client not found")

    open_reqs = query_one(
        "SELECT COUNT(*) AS n FROM requisition WHERE client_id = %s AND status = 'open'",
        [client_id],
    )
    if open_reqs and open_reqs["n"] > 0:
        raise HTTPException(
            409,
            f"Cannot deactivate '{existing['name']}' — it has {open_reqs['n']} open "
            "requisition(s). Close or reassign them first.",
        )
    query("UPDATE client SET is_active = FALSE WHERE id = %s", [client_id], fetch=False)
    return {"ok": True, "action": "deactivated"}
