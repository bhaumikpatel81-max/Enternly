"""
Band management API (TA admin).

Bands are now editable: create, rename, reorder, delete, and mapped to
the Group Companies they're available for on the requisition form.
Scoring uses the requisition.criticality FLAG (not band codes) so
renaming or removing a band never corrupts gamification points.

GET    /api/bands/all       all bands incl. inactive, with company_ids (admin view)
GET    /api/bands           active bands only (used by req form dropdowns — existing endpoint)
POST   /api/bands           create a new band, optionally mapped to companies
PATCH  /api/bands/{id}      rename / reorder / reactivate / remap companies
DELETE /api/bands/{id}      hard-delete if unused; auto-deactivates if referenced
                            by any requisition or approval chain rule
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user
from ..db import query, query_one

router = APIRouter(prefix="/api/bands", tags=["bands"])

_ADMIN_ROLES = {"admin", "ta_manager"}


def _require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in _ADMIN_ROLES:
        raise HTTPException(403, "ta_manager / admin only")
    return user


def _set_band_companies(band_id: str, company_ids: List[str]) -> None:
    """Replace a band's mapped-company set wholesale (see /api/bands company
    scoping — a Group Company with nothing mapped shows no bands at all)."""
    query("DELETE FROM band_company WHERE band_id = %s", [band_id], fetch=False)
    for cid in dict.fromkeys(company_ids):  # de-dupe, keep order
        if cid:
            query(
                "INSERT INTO band_company (band_id, company_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                [band_id, cid],
                fetch=False,
            )


_BAND_COLS = """b.id, b.code, b.rank, b.description, b.is_active,
    COALESCE(
        (SELECT array_agg(bc.company_id) FROM band_company bc WHERE bc.band_id = b.id),
        ARRAY[]::uuid[]
    ) AS company_ids"""


# ── List all bands (admin — includes inactive) ────────────────────────────────

@router.get("/all")
def list_all_bands(user: dict = Depends(_require_admin)):
    return query(f"SELECT {_BAND_COLS} FROM band b ORDER BY b.rank")


# ── Create a new band ─────────────────────────────────────────────────────────

class CreateBandIn(BaseModel):
    code:        str
    rank:        Optional[int] = None
    description: Optional[str] = None
    company_ids: List[str]     = []


@router.post("/")
def create_band(body: CreateBandIn, user: dict = Depends(_require_admin)):
    existing = query_one("SELECT id FROM band WHERE UPPER(code) = UPPER(%s)", [body.code])
    if existing:
        raise HTTPException(409, f"Band code '{body.code}' already exists")
    rank = body.rank
    if rank is None:
        rank = query_one("SELECT COALESCE(MAX(rank), 0) + 1 AS next FROM band")["next"]
    new_id = query_one(
        """INSERT INTO band (code, rank, description)
           VALUES (%s, %s, %s) RETURNING id""",
        [body.code.upper(), rank, body.description],
    )["id"]
    if body.company_ids:
        _set_band_companies(new_id, body.company_ids)
    return query_one(f"SELECT {_BAND_COLS} FROM band b WHERE b.id=%s", [new_id])


# ── Patch band (rename / reorder / reactivate / remap companies) ─────────────

class PatchBandIn(BaseModel):
    code:        Optional[str]       = None
    rank:        Optional[int]       = None
    description: Optional[str]       = None
    is_active:   Optional[bool]      = None
    company_ids: Optional[List[str]] = None


@router.patch("/{band_id}")
def patch_band(band_id: str, body: PatchBandIn, user: dict = Depends(_require_admin)):
    band = query_one("SELECT id, code FROM band WHERE id=%s", [band_id])
    if not band:
        raise HTTPException(404, "Band not found")

    sets, vals = [], []
    if body.code is not None:
        dup = query_one(
            "SELECT id FROM band WHERE UPPER(code)=UPPER(%s) AND id<>%s",
            [body.code, band_id],
        )
        if dup:
            raise HTTPException(409, f"Band code '{body.code}' already exists")
        sets.append("code=%s"); vals.append(body.code.upper())
    if body.rank is not None:
        sets.append("rank=%s"); vals.append(body.rank)
    if body.description is not None:
        sets.append("description=%s"); vals.append(body.description)
    if body.is_active is not None:
        sets.append("is_active=%s"); vals.append(body.is_active)

    if sets:
        vals.append(band_id)
        query(f"UPDATE band SET {', '.join(sets)} WHERE id=%s", vals, fetch=False)
    if body.company_ids is not None:
        _set_band_companies(band_id, body.company_ids)

    return query_one(f"SELECT {_BAND_COLS} FROM band b WHERE b.id=%s", [band_id])


# ── Delete a band — hard-delete if unused, else deactivate ──────────────────

@router.delete("/{band_id}")
def delete_band(band_id: str, user: dict = Depends(_require_admin)):
    band = query_one("SELECT id, code FROM band WHERE id=%s", [band_id])
    if not band:
        raise HTTPException(404, "Band not found")

    # A band referenced by any requisition or approval-chain rule (past or
    # present) can't be hard-deleted without corrupting that history — fall
    # back to deactivating it instead of erroring outright.
    req_count = query_one("SELECT COUNT(*) AS n FROM requisition WHERE band_id=%s", [band_id])
    chain_count = query_one("SELECT COUNT(*) AS n FROM approval_chain WHERE band_id=%s", [band_id])
    refs = (req_count["n"] if req_count else 0) + (chain_count["n"] if chain_count else 0)
    if refs > 0:
        query("UPDATE band SET is_active=FALSE WHERE id=%s", [band_id], fetch=False)
        return {
            "ok": True,
            "action": "deactivated",
            "note": f"Band '{band['code']}' is referenced by {refs} existing requisition(s)/approval "
                    "rule(s) — deactivated instead of deleted so that history stays intact.",
        }

    query("DELETE FROM band_company WHERE band_id=%s", [band_id], fetch=False)
    query("DELETE FROM band WHERE id=%s", [band_id], fetch=False)
    return {"ok": True, "action": "deleted"}
