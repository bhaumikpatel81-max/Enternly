"""
Background Verification (BGV) API (ATS spec §10.1).

Staff manage the tenant's BGV check-type list, initiate a BGV case for a
candidate (real vendor push or manual fallback -- see
services/bgv_connectors.py), and review/manually update results. The
vendor's own webhook lands at POST /api/bgv/webhooks/{provider} -- it
carries no JWT (an external system calls it), so it's listed in main.py's
_PUBLIC_PREFIXES and authenticates itself via an HMAC signature checked
against a per-tenant secret in system_settings, with the tenant resolved
from the case row (looked up by external_ref), never from the request.

Gated tenant-wide via require_tenant_module -- no per-recruiter delegation
concept, mirroring document_api.py and the other GATED_NAV_MODULES routers.
require_tenant_module is a documented no-op for any request without a
decodable staff Bearer token, so mixing the vendor webhook into this same
router (which carries that dependency) is safe -- the webhook has no
Authorization header at all.
"""
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth_utils import get_current_user, is_company_tier
from ..db import query, query_one
from ..module_access import require_tenant_module
from ..services import bgv_connectors
from ..services.activity_log import log_activity

router = APIRouter(prefix="/api/bgv", tags=["bgv"],
                    dependencies=[Depends(require_tenant_module("bgv"))])

_CHECK_STATUSES = ("pending", "in_progress", "flagged", "approved", "rejected")


def _require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not is_company_tier(user):
        raise HTTPException(403, "Company Admin access required")
    return user


def _recompute_case_status(case_id) -> str:
    """Derives bgv_case.overall_status from its bgv_check rows -- any
    rejected wins, else any flagged wins, else all-approved wins, else
    in_progress/pending -- and writes it back. overall_status is never
    written directly by a check-level update; it's always derived here so
    the two can't drift out of sync."""
    checks = query("SELECT status FROM bgv_check WHERE bgv_case_id=%s", [case_id]) or []
    statuses = {c["status"] for c in checks}
    if not statuses:
        new_status = "pending"
    elif "rejected" in statuses:
        new_status = "rejected"
    elif "flagged" in statuses:
        new_status = "flagged"
    elif statuses == {"approved"}:
        new_status = "approved"
    elif statuses == {"pending"}:
        new_status = "pending"
    else:
        new_status = "in_progress"

    if new_status in ("approved", "rejected"):
        query("UPDATE bgv_case SET overall_status=%s, completed_at=now() WHERE id=%s", [new_status, case_id], fetch=False)
    else:
        query("UPDATE bgv_case SET overall_status=%s WHERE id=%s", [new_status, case_id], fetch=False)
    return new_status


# ── Check type config (admin-tier) ──────────────────────────────────────

class CheckTypeIn(BaseModel):
    key: str
    label: str
    is_active: bool = True


class CheckTypePatchIn(BaseModel):
    label: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("/check-types")
def list_check_types(user: dict = Depends(_require_admin)):
    rows = query(
        """SELECT id, key, label, is_active FROM bgv_check_type_config
           WHERE tenant_id=%s ORDER BY label""",
        [user.get("tenant_id")],
    ) or []
    return {"types": rows}


@router.post("/check-types")
def create_check_type(body: CheckTypeIn, user: dict = Depends(_require_admin)):
    tenant_id = user.get("tenant_id")
    if query_one("SELECT id FROM bgv_check_type_config WHERE tenant_id=%s AND key=%s", [tenant_id, body.key]):
        raise HTTPException(409, "A check type with that key already exists")
    row = query_one(
        """INSERT INTO bgv_check_type_config (tenant_id, key, label, is_active)
           VALUES (%s,%s,%s,%s) RETURNING id, key, label, is_active""",
        [tenant_id, body.key, body.label, body.is_active],
    )
    log_activity("bgv_check_type_config", "bgv_check_type_created",
                 entity_id=row["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"key": body.key})
    return row


@router.patch("/check-types/{type_id}")
def patch_check_type(type_id: str, body: CheckTypePatchIn, user: dict = Depends(_require_admin)):
    tenant_id = user.get("tenant_id")
    if not query_one("SELECT id FROM bgv_check_type_config WHERE id=%s AND tenant_id=%s", [type_id, tenant_id]):
        raise HTTPException(404, "Check type not found")
    fields, params = [], []
    if body.label is not None:
        fields.append("label=%s"); params.append(body.label)
    if body.is_active is not None:
        fields.append("is_active=%s"); params.append(body.is_active)
    if not fields:
        raise HTTPException(400, "No fields to update")
    params.append(type_id)
    query(f"UPDATE bgv_check_type_config SET {', '.join(fields)} WHERE id=%s", params, fetch=False)
    log_activity("bgv_check_type_config", "bgv_check_type_updated",
                 entity_id=type_id, actor_id=user["sub"], actor_role=user.get("role"))
    return {"ok": True}


# ── Case initiation + status ────────────────────────────────────────────

class InitiateBgvIn(BaseModel):
    check_types: List[str]
    application_id: Optional[str] = None


@router.post("/candidates/{candidate_id}/initiate")
def initiate_bgv_case(candidate_id: str, body: InitiateBgvIn, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    if not body.check_types:
        raise HTTPException(400, "check_types must not be empty")
    if not query_one("SELECT id FROM candidate WHERE id=%s AND tenant_id=%s", [candidate_id, tenant_id]):
        raise HTTPException(404, "Candidate not found")
    if body.application_id and not query_one(
        "SELECT id FROM application WHERE id=%s AND candidate_id=%s", [body.application_id, candidate_id]
    ):
        raise HTTPException(404, "Application not found for this candidate")

    case = query_one(
        """INSERT INTO bgv_case (tenant_id, candidate_id, application_id, initiated_by)
           VALUES (%s,%s,%s,%s) RETURNING id, tenant_id, candidate_id""",
        [tenant_id, candidate_id, body.application_id, user["sub"]],
    )
    checks = [
        query_one(
            """INSERT INTO bgv_check (tenant_id, bgv_case_id, check_type)
               VALUES (%s,%s,%s) RETURNING id, check_type, status""",
            [tenant_id, case["id"], check_type],
        )
        for check_type in body.check_types
    ]

    try:
        result = bgv_connectors.initiate_bgv(dict(case), [dict(c) for c in checks])
    except RuntimeError as exc:
        raise HTTPException(502, f"Could not reach the BGV vendor: {exc}")

    provider = result.get("provider", "manual")
    external_ref = result.get("external_ref")
    query(
        "UPDATE bgv_case SET provider=%s, external_ref=%s, overall_status='in_progress' WHERE id=%s",
        [provider, external_ref, case["id"]], fetch=False,
    )

    log_activity("bgv_case", "bgv_initiated",
                 entity_id=case["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"candidate_id": candidate_id, "check_types": body.check_types, "provider": provider})

    return {
        "id": str(case["id"]), "provider": provider, "external_ref": external_ref, "status": "in_progress",
        "checks": [{"id": str(c["id"]), "check_type": c["check_type"], "status": c["status"]} for c in checks],
    }


@router.get("/candidates/{candidate_id}")
def get_candidate_bgv_status(candidate_id: str, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    cases = query(
        """SELECT id, provider, external_ref, overall_status, initiated_at, completed_at
           FROM bgv_case WHERE candidate_id=%s AND tenant_id=%s ORDER BY initiated_at DESC""",
        [candidate_id, tenant_id],
    ) or []
    if not cases:
        return {"cases": []}
    case_ids = [c["id"] for c in cases]
    checks = query(
        """SELECT id, bgv_case_id, check_type, status, result_summary, evidence_url, updated_at
           FROM bgv_check WHERE bgv_case_id = ANY(%s) ORDER BY check_type""",
        [case_ids],
    ) or []
    by_case = {}
    for c in checks:
        by_case.setdefault(str(c["bgv_case_id"]), []).append(c)
    return {"cases": [
        {**case, "id": str(case["id"]), "checks": by_case.get(str(case["id"]), [])}
        for case in cases
    ]}


@router.get("/cases/{case_id}")
def get_case_detail(case_id: str, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    case = query_one(
        """SELECT id, candidate_id, application_id, provider, external_ref, overall_status,
                  initiated_by, initiated_at, completed_at
           FROM bgv_case WHERE id=%s AND tenant_id=%s""",
        [case_id, tenant_id],
    )
    if not case:
        raise HTTPException(404, "BGV case not found")
    checks = query(
        """SELECT id, check_type, status, result_summary, evidence_url, updated_by, updated_at
           FROM bgv_check WHERE bgv_case_id=%s AND tenant_id=%s ORDER BY check_type""",
        [case_id, tenant_id],
    ) or []
    return {**case, "id": str(case["id"]), "checks": checks}


class CandidateBgvStatusIn(BaseModel):
    status: str


@router.patch("/candidates/{candidate_id}/status")
def set_candidate_bgv_status(candidate_id: str, body: CandidateBgvStatusIn, user: dict = Depends(get_current_user)):
    if body.status not in _CHECK_STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(_CHECK_STATUSES)}")
    tenant_id = user.get("tenant_id")
    case = query_one(
        """SELECT id FROM bgv_case WHERE candidate_id=%s AND tenant_id=%s
           ORDER BY initiated_at DESC LIMIT 1""",
        [candidate_id, tenant_id],
    )
    if not case:
        raise HTTPException(404, "No BGV case found for this candidate")

    checks = query("SELECT id FROM bgv_check WHERE bgv_case_id=%s AND tenant_id=%s", [case["id"], tenant_id]) or []
    if not checks:
        # Nothing to derive overall_status from -- the manual override IS the status.
        query("UPDATE bgv_case SET overall_status=%s WHERE id=%s", [body.status, case["id"]], fetch=False)
        new_status = body.status
    else:
        query(
            "UPDATE bgv_check SET status=%s, updated_by=%s, updated_at=now() WHERE bgv_case_id=%s",
            [body.status, user["sub"], case["id"]], fetch=False,
        )
        new_status = _recompute_case_status(case["id"])

    log_activity("bgv_case", "bgv_status_manually_set",
                 entity_id=case["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"candidate_id": candidate_id, "status": body.status})
    return {"id": str(case["id"]), "status": new_status}


class CheckPatchIn(BaseModel):
    status: str
    result_summary: Optional[str] = None
    evidence_url: Optional[str] = None


@router.patch("/checks/{check_id}")
def patch_check(check_id: str, body: CheckPatchIn, user: dict = Depends(get_current_user)):
    if body.status not in _CHECK_STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(_CHECK_STATUSES)}")
    tenant_id = user.get("tenant_id")
    check = query_one("SELECT id, bgv_case_id FROM bgv_check WHERE id=%s AND tenant_id=%s", [check_id, tenant_id])
    if not check:
        raise HTTPException(404, "Check not found")

    query(
        """UPDATE bgv_check SET status=%s, result_summary=COALESCE(%s, result_summary),
               evidence_url=COALESCE(%s, evidence_url), updated_by=%s, updated_at=now()
           WHERE id=%s""",
        [body.status, body.result_summary, body.evidence_url, user["sub"], check_id], fetch=False,
    )
    new_status = _recompute_case_status(check["bgv_case_id"])

    log_activity("bgv_check", "bgv_check_updated",
                 entity_id=check_id, actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"status": body.status})
    return {"id": check_id, "status": body.status, "case_status": new_status}


# ── Inbound vendor webhook ───────────────────────────────────────────────

@router.post("/webhooks/{provider}")
async def bgv_webhook(provider: str, request: Request):
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON payload")

    parsed = bgv_connectors.parse_bgv_webhook(provider, payload)
    external_ref = parsed.get("external_ref")
    if not external_ref:
        raise HTTPException(400, "Payload missing a case reference")

    case = query_one(
        "SELECT id, tenant_id FROM bgv_case WHERE external_ref=%s AND provider=%s",
        [external_ref, provider],
    )
    if not case:
        raise HTTPException(404, "Unknown BGV case")

    secret = bgv_connectors.get_webhook_secret(case["tenant_id"], provider)
    signature = request.headers.get("X-Signature", "")
    if not secret or not bgv_connectors.verify_webhook_signature(secret, raw_body, signature):
        raise HTTPException(401, "Invalid signature")

    for check_update in parsed.get("checks", []):
        query(
            """UPDATE bgv_check SET status=%s, result_summary=COALESCE(%s, result_summary),
                   evidence_url=COALESCE(%s, evidence_url), updated_at=now()
               WHERE bgv_case_id=%s AND check_type=%s""",
            [check_update["status"], check_update.get("result_summary"), check_update.get("evidence_url"),
             case["id"], check_update["check_type"]],
            fetch=False,
        )

    new_status = _recompute_case_status(case["id"])

    log_activity("bgv_case", "bgv_webhook_received",
                 entity_id=case["id"],
                 detail={"provider": provider, "external_ref": external_ref, "resulting_status": new_status})

    return {"ok": True, "status": new_status}
