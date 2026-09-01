"""
Preboarding & Asset Allocation API (ATS spec §12).

A preboarding_case is created once a candidate's offer is accepted (staff
calls POST /candidates/{id}/initiate, which guards on that acceptance --
this module never fires automatically off the offer flow, so offers_api.py
stays untouched). From there: tenant-configurable welcome/policy content is
shown in the candidate portal, the candidate acknowledges each policy, and
staff route asset-allocation requests to IT/Admin/HR/Security. Readiness is
computed on every read from policy-acks + asset-task completion + required
document verification (Document Collection module) -- never stored, so it
can't drift from the rows it's derived from.

Gated tenant-wide via require_tenant_module -- no per-recruiter delegation
concept, mirroring document_api.py/bgv_api.py and the other
GATED_NAV_MODULES routers.
"""
import secrets
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user, is_company_tier
from ..db import query, query_one
from ..module_access import require_tenant_module
from ..routers.candidate_portal_api import get_current_candidate
from ..services.activity_log import log_activity

router = APIRouter(prefix="/api/preboarding", tags=["preboarding"],
                    dependencies=[Depends(require_tenant_module("preboarding"))])

# offer.status's schema-legal 'accepted' value is never actually written by
# any code path in this codebase today -- there is no candidate-facing
# accept/decline endpoint yet, so the offer flow's real terminal state is
# 'sent_to_darwinbox' (all internal approvals cleared, offer released
# outward). Despite the literal enum name, that release is NOT
# Darwinbox-specific -- it's the generic "offer finalized, on its way to
# the candidate" signal regardless of which HRMS (or none) a tenant
# eventually syncs to via the hrms_* integration layer. Treat both here so
# this upgrades automatically the day a real candidate-response endpoint
# starts writing 'accepted'/'released' instead.
_ACCEPTED_OFFER_STATUSES = ("accepted", "released", "sent_to_darwinbox")

_CONTENT_TYPES = ("company_info", "welcome_video", "org_structure", "policy")
_ASSET_TEAMS = ("IT", "Admin", "HR", "Security")
_ASSET_STATUSES = ("requested", "in_progress", "assigned", "completed", "cancelled")
_DEFAULT_POLICIES = [
    ("hr_policy", "HR Policy"),
    ("leave_policy", "Leave Policy"),
    ("insurance", "Insurance"),
    ("code_of_conduct", "Code of Conduct"),
    ("it_security", "IT Security Policy"),
]


def _require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not is_company_tier(user):
        raise HTTPException(403, "Company Admin access required")
    return user


def _seed_policy_acks(tenant_id, case_id) -> None:
    """Seeds the standard policy-ack rows for a case, unless it already has
    some -- confirm() and initiate() both call this, and a case can only
    reach this point once (initiate/confirm both 409 on an existing case
    before getting here), but the guard keeps it safely idempotent either way."""
    if query_one("SELECT id FROM preboarding_policy_ack WHERE preboarding_case_id=%s LIMIT 1", [case_id]):
        return
    for policy_key, policy_label in _DEFAULT_POLICIES:
        query(
            """INSERT INTO preboarding_policy_ack (tenant_id, preboarding_case_id, policy_key, policy_label)
               VALUES (%s,%s,%s,%s)""",
            [tenant_id, case_id, policy_key, policy_label], fetch=False,
        )


def _maybe_mark_ready(case_id) -> Optional[str]:
    """Recomputes and (if changed) persists preboarding_case.status --
    'ready' once every policy is acknowledged and no asset task is still
    blocking (requested/in_progress); never overrides a terminal 'joined'
    status. Called after any policy ack or asset-task update, so the case
    status always reflects current child-row state without a separate
    "recompute" endpoint."""
    case = query_one("SELECT status FROM preboarding_case WHERE id=%s", [case_id])
    if not case or case["status"] == "joined":
        return case["status"] if case else None
    acks = query("SELECT acknowledged FROM preboarding_policy_ack WHERE preboarding_case_id=%s", [case_id]) or []
    all_acked = all(a["acknowledged"] for a in acks) if acks else True
    blocking = query_one(
        "SELECT 1 FROM asset_task WHERE preboarding_case_id=%s AND status IN ('requested','in_progress')",
        [case_id],
    )
    new_status = "ready" if (all_acked and not blocking) else "in_progress"
    if new_status != case["status"]:
        query("UPDATE preboarding_case SET status=%s WHERE id=%s", [new_status, case_id], fetch=False)
    return new_status


# ── Staff: initiate + case detail + readiness ───────────────────────────

@router.post("/candidates/{candidate_id}/initiate")
def initiate_preboarding(candidate_id: str, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    if not query_one("SELECT id FROM candidate WHERE id=%s AND tenant_id=%s", [candidate_id, tenant_id]):
        raise HTTPException(404, "Candidate not found")

    offer = query_one(
        """SELECT o.id, o.application_id, o.joining_date
           FROM offer o JOIN application a ON a.id = o.application_id
           WHERE a.candidate_id = %s AND o.status = ANY(%s)
           ORDER BY o.created_at DESC LIMIT 1""",
        [candidate_id, list(_ACCEPTED_OFFER_STATUSES)],
    )
    if not offer:
        raise HTTPException(409, "This candidate has no accepted offer — preboarding cannot be initiated yet")

    existing = query_one(
        "SELECT id, status FROM preboarding_case WHERE candidate_id=%s AND tenant_id=%s", [candidate_id, tenant_id]
    )
    if existing:
        if existing["status"] == "proposed":
            raise HTTPException(409, "A preboarding case has already been proposed for this candidate — use /confirm instead")
        raise HTTPException(409, "Preboarding has already been initiated for this candidate")

    token = secrets.token_urlsafe(32)
    case = query_one(
        """INSERT INTO preboarding_case
             (tenant_id, candidate_id, application_id, offer_id, status, portal_access_token,
              initiated_by, initiated_at, joining_date)
           VALUES (%s,%s,%s,%s,'in_progress',%s,%s,now(),%s)
           RETURNING id, status, joining_date""",
        [tenant_id, candidate_id, offer["application_id"], offer["id"], token, user["sub"], offer["joining_date"]],
    )
    _seed_policy_acks(tenant_id, case["id"])

    log_activity("preboarding_case", "preboarding_initiated",
                 entity_id=case["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"candidate_id": candidate_id, "offer_id": str(offer["id"])})

    return {"id": str(case["id"]), "status": case["status"], "joining_date": case["joining_date"]}


@router.post("/candidates/{candidate_id}/confirm")
def confirm_preboarding(candidate_id: str, user: dict = Depends(get_current_user)):
    """Accepts a 'proposed' (auto-proposed by the daily scheduler) or stray
    'not_started' case into 'in_progress' -- this is the human-in-the-loop
    step the scheduler intentionally stops short of: it only proposes,
    never opens the portal or seeds policy acks itself."""
    tenant_id = user.get("tenant_id")
    case = query_one(
        "SELECT id, status FROM preboarding_case WHERE candidate_id=%s AND tenant_id=%s ORDER BY created_at DESC LIMIT 1",
        [candidate_id, tenant_id],
    )
    if not case:
        raise HTTPException(404, "No preboarding case found for this candidate")
    if case["status"] not in ("proposed", "not_started"):
        raise HTTPException(409, f"Cannot confirm a case in status '{case['status']}'")

    token = secrets.token_urlsafe(32)
    query(
        """UPDATE preboarding_case SET status='in_progress', confirmed_by=%s, confirmed_at=now(),
               portal_access_token=%s
           WHERE id=%s""",
        [user["sub"], token, case["id"]], fetch=False,
    )
    _seed_policy_acks(tenant_id, case["id"])

    log_activity("preboarding_case", "preboarding_confirmed",
                 entity_id=case["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"candidate_id": candidate_id})

    return {"id": str(case["id"]), "status": "in_progress"}


@router.get("/proposed")
def list_proposed_cases(user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    rows = query(
        """SELECT pc.id, pc.candidate_id, c.full_name AS candidate_name, pc.joining_date, pc.created_at
           FROM preboarding_case pc JOIN candidate c ON c.id = pc.candidate_id
           WHERE pc.tenant_id=%s AND pc.status='proposed' ORDER BY pc.joining_date""",
        [tenant_id],
    ) or []
    return {"cases": rows}


@router.get("/candidates/{candidate_id}")
def get_candidate_preboarding(candidate_id: str, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    case = query_one(
        """SELECT id, candidate_id, application_id, offer_id, status, initiated_by, initiated_at,
                  joining_date, created_at
           FROM preboarding_case WHERE candidate_id=%s AND tenant_id=%s ORDER BY created_at DESC LIMIT 1""",
        [candidate_id, tenant_id],
    )
    if not case:
        raise HTTPException(404, "No preboarding case found for this candidate")
    acks = query(
        """SELECT id, policy_key, policy_label, acknowledged, acknowledged_at
           FROM preboarding_policy_ack WHERE preboarding_case_id=%s ORDER BY policy_label""",
        [case["id"]],
    ) or []
    assets = query(
        """SELECT id, asset_type, assigned_team, status, notes, updated_at
           FROM asset_task WHERE preboarding_case_id=%s ORDER BY created_at""",
        [case["id"]],
    ) or []
    return {**case, "id": str(case["id"]), "policies": acks, "assets": assets}


@router.get("/candidates/{candidate_id}/readiness")
def get_candidate_readiness(candidate_id: str, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    case = query_one(
        "SELECT id FROM preboarding_case WHERE candidate_id=%s AND tenant_id=%s ORDER BY created_at DESC LIMIT 1",
        [candidate_id, tenant_id],
    )
    if not case:
        raise HTTPException(404, "No preboarding case found for this candidate")

    acks = query(
        "SELECT policy_key, policy_label, acknowledged FROM preboarding_policy_ack WHERE preboarding_case_id=%s",
        [case["id"]],
    ) or []
    policies_total = len(acks)
    policies_done = sum(1 for a in acks if a["acknowledged"])

    assets = query(
        "SELECT asset_type, status FROM asset_task WHERE preboarding_case_id=%s AND status != 'cancelled'",
        [case["id"]],
    ) or []
    assets_total = len(assets)
    assets_done = sum(1 for a in assets if a["status"] == "completed")

    required_types = query(
        "SELECT key, label FROM document_type_config WHERE tenant_id=%s AND is_required=TRUE AND is_active=TRUE",
        [tenant_id],
    ) or []
    verified_docs = query(
        "SELECT DISTINCT doc_type FROM candidate_document WHERE candidate_id=%s AND tenant_id=%s AND status='verified'",
        [candidate_id, tenant_id],
    ) or []
    verified_keys = {d["doc_type"] for d in verified_docs}
    docs_total = len(required_types)
    docs_done = sum(1 for t in required_types if t["key"] in verified_keys)

    overall_total = policies_total + assets_total + docs_total
    overall_done = policies_done + assets_done + docs_done
    percent_ready = 100 if overall_total == 0 else round(100 * overall_done / overall_total)

    return {
        "percent_ready": percent_ready,
        "policies": {"total": policies_total, "acknowledged": policies_done, "items": acks},
        "assets": {"total": assets_total, "completed": assets_done, "items": assets},
        "documents": {
            "total_required": docs_total,
            "verified": docs_done,
            "items": [{"key": t["key"], "label": t["label"], "verified": t["key"] in verified_keys} for t in required_types],
        },
    }


# ── Staff: asset allocation ─────────────────────────────────────────────

class AssetRequestItem(BaseModel):
    asset_type: str
    assigned_team: str
    notes: Optional[str] = None


class AssetRequestIn(BaseModel):
    assets: List[AssetRequestItem]


@router.post("/candidates/{candidate_id}/assets/request")
def request_assets(candidate_id: str, body: AssetRequestIn, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    if not body.assets:
        raise HTTPException(400, "assets must not be empty")
    case = query_one(
        "SELECT id FROM preboarding_case WHERE candidate_id=%s AND tenant_id=%s ORDER BY created_at DESC LIMIT 1",
        [candidate_id, tenant_id],
    )
    if not case:
        raise HTTPException(404, "No preboarding case found for this candidate")

    created = []
    for item in body.assets:
        if item.assigned_team not in _ASSET_TEAMS:
            raise HTTPException(400, f"assigned_team must be one of {', '.join(_ASSET_TEAMS)}")
        row = query_one(
            """INSERT INTO asset_task (tenant_id, preboarding_case_id, asset_type, assigned_team, notes, requested_by)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, asset_type, assigned_team, status""",
            [tenant_id, case["id"], item.asset_type, item.assigned_team, item.notes, user["sub"]],
        )
        created.append(row)

    log_activity("preboarding_case", "assets_requested",
                 entity_id=case["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"candidate_id": candidate_id, "assets": [a.model_dump() for a in body.assets]})

    return {"case_id": str(case["id"]), "assets": [{**a, "id": str(a["id"])} for a in created]}


class AssetPatchIn(BaseModel):
    status: Optional[str] = None
    assigned_team: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/assets/{asset_task_id}")
def patch_asset_task(asset_task_id: str, body: AssetPatchIn, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    task = query_one(
        "SELECT id, preboarding_case_id FROM asset_task WHERE id=%s AND tenant_id=%s",
        [asset_task_id, tenant_id],
    )
    if not task:
        raise HTTPException(404, "Asset task not found")
    if body.status is not None and body.status not in _ASSET_STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(_ASSET_STATUSES)}")
    if body.assigned_team is not None and body.assigned_team not in _ASSET_TEAMS:
        raise HTTPException(400, f"assigned_team must be one of {', '.join(_ASSET_TEAMS)}")

    fields, params = [], []
    if body.status is not None:
        fields.append("status=%s"); params.append(body.status)
    if body.assigned_team is not None:
        fields.append("assigned_team=%s"); params.append(body.assigned_team)
    if body.notes is not None:
        fields.append("notes=%s"); params.append(body.notes)
    if not fields:
        raise HTTPException(400, "No fields to update")
    fields.append("updated_by=%s"); params.append(user["sub"])
    fields.append("updated_at=now()")
    params.append(asset_task_id)
    query(f"UPDATE asset_task SET {', '.join(fields)} WHERE id=%s", params, fetch=False)

    new_case_status = _maybe_mark_ready(task["preboarding_case_id"])

    log_activity("asset_task", "asset_task_updated",
                 entity_id=asset_task_id, actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"status": body.status, "assigned_team": body.assigned_team})

    return {"id": asset_task_id, "case_status": new_case_status}


# ── Staff: content management (admin-tier) ──────────────────────────────

class ContentIn(BaseModel):
    title: str
    content_type: str
    body: Optional[str] = None
    url: Optional[str] = None
    display_order: int = 0
    is_active: bool = True


class ContentPatchIn(BaseModel):
    title: Optional[str] = None
    content_type: Optional[str] = None
    body: Optional[str] = None
    url: Optional[str] = None
    display_order: Optional[int] = None
    is_active: Optional[bool] = None


@router.get("/content")
def list_content(user: dict = Depends(_require_admin)):
    rows = query(
        """SELECT id, title, content_type, body, url, display_order, is_active
           FROM preboarding_content WHERE tenant_id=%s ORDER BY display_order, title""",
        [user.get("tenant_id")],
    ) or []
    return {"content": rows}


@router.post("/content")
def create_content(body: ContentIn, user: dict = Depends(_require_admin)):
    if body.content_type not in _CONTENT_TYPES:
        raise HTTPException(400, f"content_type must be one of {', '.join(_CONTENT_TYPES)}")
    tenant_id = user.get("tenant_id")
    row = query_one(
        """INSERT INTO preboarding_content (tenant_id, title, content_type, body, url, display_order, is_active)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           RETURNING id, title, content_type, body, url, display_order, is_active""",
        [tenant_id, body.title, body.content_type, body.body, body.url, body.display_order, body.is_active],
    )
    log_activity("preboarding_content", "preboarding_content_created",
                 entity_id=row["id"], actor_id=user["sub"], actor_role=user.get("role"))
    return row


@router.patch("/content/{content_id}")
def patch_content(content_id: str, body: ContentPatchIn, user: dict = Depends(_require_admin)):
    tenant_id = user.get("tenant_id")
    if not query_one("SELECT id FROM preboarding_content WHERE id=%s AND tenant_id=%s", [content_id, tenant_id]):
        raise HTTPException(404, "Content not found")
    if body.content_type is not None and body.content_type not in _CONTENT_TYPES:
        raise HTTPException(400, f"content_type must be one of {', '.join(_CONTENT_TYPES)}")

    fields, params = [], []
    for col in ("title", "content_type", "body", "url", "display_order", "is_active"):
        val = getattr(body, col)
        if val is not None:
            fields.append(f"{col}=%s"); params.append(val)
    if not fields:
        raise HTTPException(400, "No fields to update")
    params.append(content_id)
    query(f"UPDATE preboarding_content SET {', '.join(fields)} WHERE id=%s", params, fetch=False)

    log_activity("preboarding_content", "preboarding_content_updated",
                 entity_id=content_id, actor_id=user["sub"], actor_role=user.get("role"))
    return {"ok": True}


# ── Candidate portal ─────────────────────────────────────────────────────

@router.get("/portal/content")
def portal_content(candidate: dict = Depends(get_current_candidate)):
    candidate_id = candidate.get("candidate_id")
    case = query_one(
        "SELECT id, tenant_id, status, joining_date FROM preboarding_case WHERE candidate_id=%s ORDER BY created_at DESC LIMIT 1",
        [candidate_id],
    )
    if not case:
        raise HTTPException(404, "Preboarding has not been initiated for this candidate yet")
    content = query(
        """SELECT id, title, content_type, body, url, display_order
           FROM preboarding_content WHERE tenant_id=%s AND is_active=TRUE ORDER BY display_order, title""",
        [case["tenant_id"]],
    ) or []
    policies = query(
        """SELECT policy_key, policy_label, acknowledged, acknowledged_at
           FROM preboarding_policy_ack WHERE preboarding_case_id=%s ORDER BY policy_label""",
        [case["id"]],
    ) or []
    return {"case_status": case["status"], "joining_date": case["joining_date"], "content": content, "policies": policies}


class PolicyAckIn(BaseModel):
    policy_key: str


@router.post("/portal/policy-acknowledgement")
def portal_acknowledge_policy(body: PolicyAckIn, candidate: dict = Depends(get_current_candidate)):
    candidate_id = candidate.get("candidate_id")
    case = query_one(
        "SELECT id FROM preboarding_case WHERE candidate_id=%s ORDER BY created_at DESC LIMIT 1",
        [candidate_id],
    )
    if not case:
        raise HTTPException(404, "Preboarding has not been initiated for this candidate yet")
    ack = query_one(
        "SELECT id FROM preboarding_policy_ack WHERE preboarding_case_id=%s AND policy_key=%s",
        [case["id"], body.policy_key],
    )
    if not ack:
        raise HTTPException(404, "Unknown policy for this case")

    query(
        "UPDATE preboarding_policy_ack SET acknowledged=TRUE, acknowledged_at=now() WHERE id=%s",
        [ack["id"]], fetch=False,
    )
    new_status = _maybe_mark_ready(case["id"])

    log_activity("preboarding_case", "policy_acknowledged",
                 entity_id=case["id"], actor_id=candidate_id, actor_role="candidate",
                 detail={"policy_key": body.policy_key})

    return {"ok": True, "policy_key": body.policy_key, "case_status": new_status}
