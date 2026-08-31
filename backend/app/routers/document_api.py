"""
Document Collection & Verification API (ATS spec §10).

Staff manage the tenant's required-document list, request documents from a
candidate, and verify/reject what comes back (with an optional compliance
review step). Candidates upload against their own requested documents via
the candidate portal JWT. Gated tenant-wide via require_tenant_module --
no per-recruiter delegation concept, mirroring the other GATED_NAV_MODULES
routers (cv_api.py, hiring_plan_api.py, etc).
"""
import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ..auth_utils import get_current_user, is_company_tier
from ..db import query, query_one
from ..module_access import require_tenant_module
from ..routers.candidate_portal_api import get_current_candidate
from ..services.activity_log import log_activity

router = APIRouter(prefix="/api/documents", tags=["documents"],
                    dependencies=[Depends(require_tenant_module("documents"))])

_ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".docx"}
_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_TERMINAL_UPLOAD_STATUSES = ("uploaded", "verified", "compliance_review", "rejected")


def _require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not is_company_tier(user):
        raise HTTPException(403, "Company Admin access required")
    return user


def _refresh_request_status(candidate_id: str, tenant_id: str) -> None:
    """Recompute the candidate's latest document_request.status from
    whatever's actually been uploaded so far -- the request row is a
    snapshot of intent, not a queue, so only the most recent one matters."""
    latest = query_one(
        """SELECT id, requested_doc_types FROM document_request
           WHERE candidate_id=%s AND tenant_id=%s ORDER BY created_at DESC LIMIT 1""",
        [candidate_id, tenant_id],
    )
    if not latest:
        return
    docs = query(
        "SELECT doc_type, status FROM candidate_document WHERE candidate_id=%s AND tenant_id=%s",
        [candidate_id, tenant_id],
    ) or []
    doc_status = {d["doc_type"]: d["status"] for d in docs}
    types = latest["requested_doc_types"] or []
    done = sum(1 for t in types if doc_status.get(t) in _TERMINAL_UPLOAD_STATUSES)
    new_status = "complete" if types and done == len(types) else ("partial" if done > 0 else "sent")
    query("UPDATE document_request SET status=%s WHERE id=%s", [new_status, latest["id"]], fetch=False)


# ── Document type config (admin-tier) ──────────────────────────────────

class DocTypeIn(BaseModel):
    key: str
    label: str
    is_required: bool = True
    is_active: bool = True


class DocTypePatchIn(BaseModel):
    label: Optional[str] = None
    is_required: Optional[bool] = None
    is_active: Optional[bool] = None


@router.get("/types")
def list_document_types(user: dict = Depends(_require_admin)):
    rows = query(
        """SELECT id, key, label, is_required, is_active FROM document_type_config
           WHERE tenant_id=%s ORDER BY label""",
        [user.get("tenant_id")],
    ) or []
    return {"types": rows}


@router.post("/types")
def create_document_type(body: DocTypeIn, user: dict = Depends(_require_admin)):
    tenant_id = user.get("tenant_id")
    if query_one("SELECT id FROM document_type_config WHERE tenant_id=%s AND key=%s", [tenant_id, body.key]):
        raise HTTPException(409, "A document type with that key already exists")
    row = query_one(
        """INSERT INTO document_type_config (tenant_id, key, label, is_required, is_active)
           VALUES (%s,%s,%s,%s,%s) RETURNING id, key, label, is_required, is_active""",
        [tenant_id, body.key, body.label, body.is_required, body.is_active],
    )
    log_activity("document_type_config", "document_type_created",
                 entity_id=row["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"key": body.key})
    return row


@router.patch("/types/{type_id}")
def patch_document_type(type_id: str, body: DocTypePatchIn, user: dict = Depends(_require_admin)):
    tenant_id = user.get("tenant_id")
    if not query_one("SELECT id FROM document_type_config WHERE id=%s AND tenant_id=%s", [type_id, tenant_id]):
        raise HTTPException(404, "Document type not found")
    fields, params = [], []
    if body.label is not None:
        fields.append("label=%s"); params.append(body.label)
    if body.is_required is not None:
        fields.append("is_required=%s"); params.append(body.is_required)
    if body.is_active is not None:
        fields.append("is_active=%s"); params.append(body.is_active)
    if not fields:
        raise HTTPException(400, "No fields to update")
    params.append(type_id)
    query(f"UPDATE document_type_config SET {', '.join(fields)} WHERE id=%s", params, fetch=False)
    log_activity("document_type_config", "document_type_updated",
                 entity_id=type_id, actor_id=user["sub"], actor_role=user.get("role"))
    return {"ok": True}


# ── Staff: request + review ─────────────────────────────────────────────

class RequestDocsIn(BaseModel):
    requested_doc_types: List[str]
    message: Optional[str] = None


@router.post("/candidates/{candidate_id}/request")
def request_documents(candidate_id: str, body: RequestDocsIn, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    if not body.requested_doc_types:
        raise HTTPException(400, "requested_doc_types must not be empty")
    if not query_one("SELECT id FROM candidate WHERE id=%s AND tenant_id=%s", [candidate_id, tenant_id]):
        raise HTTPException(404, "Candidate not found")

    req = query_one(
        """INSERT INTO document_request (tenant_id, candidate_id, requested_doc_types, requested_by, message)
           VALUES (%s,%s,%s,%s,%s) RETURNING id, status, created_at""",
        [tenant_id, candidate_id, body.requested_doc_types, user["sub"], body.message],
    )
    for doc_type in body.requested_doc_types:
        if query_one(
            """SELECT id FROM candidate_document
               WHERE tenant_id=%s AND candidate_id=%s AND doc_type=%s AND status='requested'""",
            [tenant_id, candidate_id, doc_type],
        ):
            continue
        query(
            """INSERT INTO candidate_document (tenant_id, candidate_id, doc_type, status)
               VALUES (%s,%s,%s,'requested')""",
            [tenant_id, candidate_id, doc_type], fetch=False,
        )

    log_activity("document_request", "document_requested",
                 entity_id=req["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"candidate_id": candidate_id, "requested_doc_types": body.requested_doc_types})

    # TODO: notify the candidate once an email template is wired for this
    # module (see services/notifications.py / email_template_api.py).

    return {"id": str(req["id"]), "status": req["status"], "requested_doc_types": body.requested_doc_types}


@router.get("/candidates/{candidate_id}")
def list_candidate_documents(candidate_id: str, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    docs = query(
        """SELECT id, doc_type, status, file_name, uploaded_at,
                  hr_verified_by, hr_verified_at, compliance_reviewed_by, compliance_reviewed_at,
                  rejection_reason, notes, created_at
           FROM candidate_document WHERE candidate_id=%s AND tenant_id=%s ORDER BY created_at""",
        [candidate_id, tenant_id],
    ) or []
    requests = query(
        """SELECT id, requested_doc_types, message, status, created_at
           FROM document_request WHERE candidate_id=%s AND tenant_id=%s ORDER BY created_at DESC""",
        [candidate_id, tenant_id],
    ) or []
    return {"documents": docs, "requests": requests}


class VerifyIn(BaseModel):
    notes: Optional[str] = None


@router.post("/{document_id}/verify")
def verify_document(document_id: str, body: VerifyIn, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    doc = query_one("SELECT id, status FROM candidate_document WHERE id=%s AND tenant_id=%s", [document_id, tenant_id])
    if not doc:
        raise HTTPException(404, "Document not found")
    if doc["status"] not in ("uploaded", "compliance_review"):
        raise HTTPException(400, f"Cannot verify a document in status '{doc['status']}'")
    query(
        """UPDATE candidate_document SET status='verified', hr_verified_by=%s, hr_verified_at=now(),
               notes=COALESCE(%s, notes)
           WHERE id=%s""",
        [user["sub"], body.notes, document_id], fetch=False,
    )
    log_activity("candidate_document", "document_verified",
                 entity_id=document_id, actor_id=user["sub"], actor_role=user.get("role"))
    return {"ok": True, "status": "verified"}


class ComplianceReviewIn(BaseModel):
    decision: str  # 'start' | 'approve' | 'reject'
    rejection_reason: Optional[str] = None


@router.post("/{document_id}/compliance-review")
def compliance_review_document(document_id: str, body: ComplianceReviewIn, user: dict = Depends(get_current_user)):
    if body.decision not in ("start", "approve", "reject"):
        raise HTTPException(400, "decision must be 'start', 'approve', or 'reject'")
    tenant_id = user.get("tenant_id")
    doc = query_one("SELECT id, status FROM candidate_document WHERE id=%s AND tenant_id=%s", [document_id, tenant_id])
    if not doc:
        raise HTTPException(404, "Document not found")

    if body.decision == "start":
        if doc["status"] not in ("uploaded", "verified"):
            raise HTTPException(400, f"Cannot start compliance review from status '{doc['status']}'")
        query("UPDATE candidate_document SET status='compliance_review' WHERE id=%s", [document_id], fetch=False)
        log_activity("candidate_document", "compliance_review_started",
                     entity_id=document_id, actor_id=user["sub"], actor_role=user.get("role"))
        return {"ok": True, "status": "compliance_review"}

    if doc["status"] != "compliance_review":
        raise HTTPException(400, "Document is not under compliance review")

    if body.decision == "approve":
        query(
            """UPDATE candidate_document SET status='verified', compliance_reviewed_by=%s, compliance_reviewed_at=now()
               WHERE id=%s""",
            [user["sub"], document_id], fetch=False,
        )
        log_activity("candidate_document", "compliance_review_approved",
                     entity_id=document_id, actor_id=user["sub"], actor_role=user.get("role"))
        return {"ok": True, "status": "verified"}

    if not body.rejection_reason:
        raise HTTPException(400, "rejection_reason is required to reject")
    query(
        """UPDATE candidate_document SET status='rejected', compliance_reviewed_by=%s, compliance_reviewed_at=now(),
               rejection_reason=%s
           WHERE id=%s""",
        [user["sub"], body.rejection_reason, document_id], fetch=False,
    )
    log_activity("candidate_document", "compliance_review_rejected",
                 entity_id=document_id, actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"reason": body.rejection_reason})
    return {"ok": True, "status": "rejected"}


class StatusPatchIn(BaseModel):
    status: str  # 'verified' | 'rejected'
    rejection_reason: Optional[str] = None


@router.patch("/{document_id}/status")
def patch_document_status(document_id: str, body: StatusPatchIn, user: dict = Depends(get_current_user)):
    if body.status not in ("verified", "rejected"):
        raise HTTPException(400, "status must be 'verified' or 'rejected'")
    if body.status == "rejected" and not body.rejection_reason:
        raise HTTPException(400, "rejection_reason is required when rejecting")
    tenant_id = user.get("tenant_id")
    if not query_one("SELECT id FROM candidate_document WHERE id=%s AND tenant_id=%s", [document_id, tenant_id]):
        raise HTTPException(404, "Document not found")

    if body.status == "verified":
        query(
            "UPDATE candidate_document SET status='verified', hr_verified_by=%s, hr_verified_at=now() WHERE id=%s",
            [user["sub"], document_id], fetch=False,
        )
    else:
        query(
            "UPDATE candidate_document SET status='rejected', rejection_reason=%s WHERE id=%s",
            [body.rejection_reason, document_id], fetch=False,
        )
    log_activity("candidate_document", f"document_{body.status}",
                 entity_id=document_id, actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"rejection_reason": body.rejection_reason} if body.status == "rejected" else None)
    return {"ok": True, "status": body.status}


# ── Candidate upload ─────────────────────────────────────────────────────

@router.post("/candidates/{candidate_id}/upload")
async def candidate_upload_document(
    candidate_id: str,
    doc_type: str = Form(...),
    file: UploadFile = File(...),
    candidate: dict = Depends(get_current_candidate),
):
    if str(candidate.get("candidate_id")) != str(candidate_id):
        raise HTTPException(403, "Candidate access only")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _ALLOWED:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Upload PDF, JPG, PNG, or DOCX.")
    data = await file.read()
    if len(data) > _MAX_BYTES:
        raise HTTPException(400, "File is too large (5MB max)")

    doc = query_one(
        """SELECT id, tenant_id FROM candidate_document
           WHERE candidate_id=%s AND doc_type=%s ORDER BY created_at DESC LIMIT 1""",
        [candidate_id, doc_type],
    )
    if not doc:
        raise HTTPException(404, "This document type was not requested")

    store = os.environ.get("DOC_STORE_DIR", "/app/doc_store")
    os.makedirs(store, exist_ok=True)
    dest = os.path.join(store, f"{uuid.uuid4()}{ext}")
    with open(dest, "wb") as f:
        f.write(data)

    query(
        """UPDATE candidate_document
           SET file_path=%s, file_name=%s, uploaded_by=%s, uploaded_at=now(), status='uploaded'
           WHERE id=%s""",
        [dest, file.filename, candidate.get("sub"), doc["id"]], fetch=False,
    )
    _refresh_request_status(candidate_id, doc["tenant_id"])

    log_activity("candidate_document", "document_uploaded",
                 entity_id=doc["id"], actor_id=candidate.get("sub"), actor_role="candidate",
                 detail={"doc_type": doc_type})

    return {"ok": True, "status": "uploaded"}
