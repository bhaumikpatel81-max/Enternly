"""
Documentation stage API — document collection and negotiation log.

Used when a candidate is in the 'documentation' stage (HR collects
offer documents and records salary negotiation updates).
"""
import os
import shutil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user
from .nexai_api import _recruiter_owns_req, _application_req_id

router = APIRouter(prefix="/api/applications", tags=["documentation"])

_DOCS_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "uploads", "documents"
    )
)
os.makedirs(_DOCS_DIR, exist_ok=True)


def _assert_can_access(app_id: str, user: dict) -> None:
    """Role + requisition-ownership gate for the documentation stage.
    Offer docs and negotiation notes are recruiter/ta_manager/admin only;
    recruiters are scoped to requisitions they own. 404 (not 403) on a
    non-owned or missing application, to prevent ID enumeration."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(404, "Application not found")
    if user["role"] == "recruiter":
        req_id = _application_req_id(app_id)
        if not req_id or not _recruiter_owns_req(user, req_id):
            raise HTTPException(404, "Application not found")


# ── Documents ─────────────────────────────────────────────────────────────────

@router.get("/{app_id}/documents")
def list_documents(app_id: str, user: dict = Depends(get_current_user)):
    _assert_can_access(app_id, user)
    return query(
        """SELECT d.id, d.file_name, d.doc_type, d.uploaded_at, d.notes,
                  u.full_name AS uploaded_by_name
           FROM application_document d
           LEFT JOIN app_user u ON u.id = d.uploaded_by
           WHERE d.application_id = %s
           ORDER BY d.uploaded_at DESC""",
        [app_id],
    ) or []


@router.post("/{app_id}/documents", status_code=201)
async def upload_document(
    app_id: str,
    file: UploadFile = File(...),
    doc_type: str = Form("general"),
    notes: str = Form(""),
    user: dict = Depends(get_current_user),
):
    _assert_can_access(app_id, user)

    safe_name = "".join(c for c in (file.filename or "file") if c.isalnum() or c in "._- ")
    dest = os.path.join(_DOCS_DIR, f"{app_id}_{safe_name}")
    with open(dest, "wb") as fh:
        shutil.copyfileobj(file.file, fh)

    row = query_one(
        """INSERT INTO application_document
               (application_id, file_name, file_path, doc_type, uploaded_by, notes)
           VALUES (%s, %s, %s, %s, %s, %s)
           RETURNING id""",
        [app_id, file.filename, dest, doc_type or "general", user["sub"], notes or None],
    )
    return {"id": str(row["id"]), "ok": True}


@router.delete("/{app_id}/documents/{doc_id}")
def delete_document(app_id: str, doc_id: str, user: dict = Depends(get_current_user)):
    _assert_can_access(app_id, user)
    doc = query_one(
        "SELECT file_path FROM application_document WHERE id=%s AND application_id=%s",
        [doc_id, app_id],
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    try:
        if os.path.exists(doc["file_path"]):
            os.remove(doc["file_path"])
    except Exception:
        pass
    query("DELETE FROM application_document WHERE id=%s", [doc_id], fetch=False)
    return {"ok": True}


# ── Negotiation log ───────────────────────────────────────────────────────────

class NegotiationEntryIn(BaseModel):
    note: str
    stage_detail: Optional[str] = None


@router.get("/{app_id}/negotiation")
def get_negotiation(app_id: str, user: dict = Depends(get_current_user)):
    _assert_can_access(app_id, user)
    return query(
        """SELECT n.id, n.note, n.stage_detail, n.logged_at,
                  u.full_name AS logged_by_name
           FROM negotiation_log n
           LEFT JOIN app_user u ON u.id = n.logged_by
           WHERE n.application_id = %s
           ORDER BY n.logged_at DESC""",
        [app_id],
    ) or []


@router.post("/{app_id}/negotiation", status_code=201)
def add_negotiation(
    app_id: str,
    body: NegotiationEntryIn,
    user: dict = Depends(get_current_user),
):
    _assert_can_access(app_id, user)
    if not body.note.strip():
        raise HTTPException(400, "note must not be empty")
    row = query_one(
        """INSERT INTO negotiation_log (application_id, note, stage_detail, logged_by)
           VALUES (%s, %s, %s, %s)
           RETURNING id, logged_at""",
        [app_id, body.note.strip(), body.stage_detail, user["sub"]],
    )
    return {"id": str(row["id"]), "ok": True}
