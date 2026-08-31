"""
PART B — Proctoring endpoints (consent-gated).

HARD LEGAL GATE: No real external candidate may be recorded until
legal sign-off is obtained. Testing with internal volunteers only.
All proctoring data must stay on company infrastructure — media bytes are
stored in Postgres (services/proctoring_storage.py), not local disk or a
third-party cloud. AI flags are assistive only — reviewed by a human
recruiter, never auto-reject.

Buildable in this router (B1-B7):
  B1  Consent + recording badge
  B2  Identity snapshot (webcam still stored per session)
  B3  Webcam video chunks
  B4  Screen recording chunks (flagged if declined)
  B5  Audio monitoring (part of webcam stream)
  B6  AI behaviour flags submitted from browser TF.js analysis
  B7  Flag review tool for human recruiter

Out of scope — specialist vendor or native app needed (NOT attempted here):
  - Lockdown browser (tab/copy/paste/print blocking) → needs installed native app
  - Secondary-device / hidden-phone detection via audio/Wi-Fi → needs native + hardware
  - Virtual machine blocking → needs native system access
  - Government-ID biometric face matching → specialist paid identity service
  - Keystroke-dynamics identity → low reliability; skip
"""
import io
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

import re as _re
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services import proctoring_storage as storage
from ..services import proctoring_scorer as scorer
from .enteri_ai_api import _recruiter_owns_req

from ..module_access import require_tenant_module

router = APIRouter(prefix="/api/proctoring", tags=["proctoring"],
                    dependencies=[Depends(require_tenant_module("proctoring_review"))])


def _application_req_id(application_id):
    """Return the requisition_id for an application, or None."""
    row = query_one("SELECT requisition_id FROM application WHERE id=%s", [application_id])
    return row["requisition_id"] if row else None


def _proctoring_session_req_id(session_id):
    """Return the requisition_id behind a proctoring_session, via its application, or None."""
    row = query_one(
        """SELECT a.requisition_id
           FROM proctoring_session ps
           JOIN application a ON a.id = ps.application_id
           WHERE ps.id = %s""",
        [session_id],
    )
    return row["requisition_id"] if row else None


def _review_req_id_for_id(id_):
    """
    Like _proctoring_session_req_id, but also resolves a bare enteri_ai_session.id
    -- GET /review now lists 'did_not_run' rows keyed by enteri_ai_session.id
    (there's no proctoring_session row to key them by), so the detail
    endpoint must accept either id type without opening up scope beyond
    what /review already returned.
    """
    req_id = _proctoring_session_req_id(id_)
    if req_id is not None:
        return req_id
    row = query_one(
        """SELECT a.requisition_id
           FROM enteri_ai_session ns
           JOIN application a ON a.id = ns.application_id
           WHERE ns.id = %s""",
        [id_],
    )
    return row["requisition_id"] if row else None


# ── B1: Create session + record consent ───────────────────────────────────────

class CreateSessionIn(BaseModel):
    application_id: str
    enteri_ai_session_id: Optional[str] = None


@router.post("/sessions", status_code=201)
def create_session(body: CreateSessionIn, user: dict = Depends(get_current_user)):
    if user.get("role") not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(404, "Not found")
    req_id = _application_req_id(body.application_id)
    if not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Not found")
    existing = query_one(
        "SELECT id FROM proctoring_session WHERE application_id = %s",
        [body.application_id],
    )
    if existing:
        # Update enteri_ai_session_id if supplied (called after Enteri AI session is created)
        if body.enteri_ai_session_id:
            query(
                "UPDATE proctoring_session SET enteri_ai_session_id=%s WHERE id=%s",
                [body.enteri_ai_session_id, existing["id"]],
                fetch=False,
            )
        return query_one("SELECT id, consent_granted, created_at FROM proctoring_session WHERE id=%s", [existing["id"]])
    row = query_one(
        """INSERT INTO proctoring_session (application_id, enteri_ai_session_id)
           VALUES (%s, %s) RETURNING id, consent_granted, created_at""",
        [body.application_id, body.enteri_ai_session_id],
    )
    return row


class ConsentIn(BaseModel):
    granted: bool
    consent_text: str = (
        "This Enteri AI interview session will be video recorded (webcam), screen recorded, "
        "and audio recorded. A photo will be taken at the start for identity purposes. "
        "AI behaviour analysis will run on the recording. All data is stored on EnternsTech GCP. "
        "AI flags are reviewed by a human recruiter and are never used to auto-reject."
    )
    retention_days: int = 90


@router.post("/sessions/{session_id}/consent")
def record_consent(
    session_id: str, body: ConsentIn, user: dict = Depends(get_current_user)
):
    if user.get("role") not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(404, "Not found")
    req_id = _proctoring_session_req_id(session_id)
    if not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Not found")
    retention_until = datetime.utcnow() + timedelta(days=body.retention_days)
    row = query_one(
        """UPDATE proctoring_session
           SET consent_granted = %s,
               proctoring_declined = %s,
               consent_text = %s,
               consented_at = now(),
               retention_until = %s
           WHERE id = %s
           RETURNING id, consent_granted, proctoring_declined""",
        [body.granted, not body.granted, body.consent_text, retention_until, session_id],
    )
    if not row:
        raise HTTPException(404, "proctoring session not found")
    return row


# ── B2: Identity snapshot ─────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/identity")
async def save_identity_snapshot(
    session_id: str,
    snapshot: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """
    Store a webcam still captured at interview start.
    NOTE: Automated biometric matching against a government ID is a specialist
    paid service. The identity_match_status column is scaffolded but
    matching is NOT implemented here. A human reviews the photo.
    """
    if user.get("role") not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(404, "Not found")
    req_id = _proctoring_session_req_id(session_id)
    if not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Not found")
    _assert_consented(session_id)
    ext = os.path.splitext(snapshot.filename or "")[1] or ".jpg"
    storage.save_identity(
        session_id, await snapshot.read(), ext,
        snapshot.content_type or "image/jpeg",
    )
    return {"saved": True, "path": f"{session_id}_identity{ext}"}


# ── B3/B4/B5: Media chunk upload ─────────────────────────────────────────────

@router.post("/sessions/{session_id}/media-chunk")
async def upload_media_chunk(
    session_id: str,
    media_type: str = Form(...),   # "webcam" | "screen"
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """
    Receive a video chunk (webcam or screen). Audio is included in the webcam stream.
    Chunks are stored in Postgres, keyed by (session_id, media_type, chunk_index).
    """
    if user.get("role") not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(404, "Not found")
    req_id = _proctoring_session_req_id(session_id)
    if not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Not found")
    _assert_consented(session_id)
    if media_type not in ("webcam", "screen"):
        raise HTTPException(400, "media_type must be 'webcam' or 'screen'")
    ext = os.path.splitext(chunk.filename or "")[1] or ".webm"
    storage.save_chunk(
        session_id, media_type, chunk_index, await chunk.read(), ext,
        chunk.content_type or "video/webm",
    )
    # Marker column on first chunk — actual bytes live in proctoring_media.
    col = "webcam_video_path" if media_type == "webcam" else "screen_video_path"
    query_one(
        f"UPDATE proctoring_session SET {col} = %s WHERE id = %s RETURNING id",
        [f"{session_id}/{media_type}", session_id],
    )
    return {"saved": True, "chunk": chunk_index}


@router.post("/sessions/{session_id}/screen-declined")
def screen_declined(session_id: str, user: dict = Depends(get_current_user)):
    """Flag that the candidate declined screen recording (session continues, just noted)."""
    if user.get("role") not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(404, "Not found")
    req_id = _proctoring_session_req_id(session_id)
    if not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Not found")
    row = query_one(
        "UPDATE proctoring_session SET screen_recording_declined = true WHERE id = %s RETURNING id",
        [session_id],
    )
    if not row:
        raise HTTPException(404, "session not found")
    return {"noted": True}


# ── B6: AI behaviour flags (from browser TF.js analysis) ─────────────────────

class FlagsIn(BaseModel):
    flags: list  # [{ts_ms: int, type: str, confidence: float, detail: str}]


@router.post("/sessions/{session_id}/flags")
def submit_flags(session_id: str, body: FlagsIn, user: dict = Depends(get_current_user)):
    """
    Accept AI behaviour flags generated by TF.js (BlazeFace + COCO-SSD) in the browser.
    Flag types: multi_face | no_face | face_away | phone_detected | unknown_object
    These are assistive only — surfaced to a human reviewer, never auto-reject.
    """
    if user.get("role") not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(404, "Not found")
    req_id = _proctoring_session_req_id(session_id)
    if not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Not found")
    # Atomic append at the DB level (flags || new) instead of read-modify-write
    # in Python — two concurrent submissions no longer race and silently drop
    # one side's flags.
    updated = query_one(
        """UPDATE proctoring_session
           SET flags = flags || %s::jsonb, flag_count = flag_count + %s
           WHERE id = %s
           RETURNING flag_count""",
        [json.dumps(body.flags), len(body.flags), session_id],
    )
    if not updated:
        raise HTTPException(404, "session not found")
    return {"flag_count": updated["flag_count"]}


@router.post("/sessions/{session_id}/complete")
def complete_session(session_id: str, user: dict = Depends(get_current_user)):
    if user.get("role") not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(404, "Not found")
    req_id = _proctoring_session_req_id(session_id)
    if not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Not found")
    row = query_one(
        """UPDATE proctoring_session
           SET proctoring_complete = TRUE
           WHERE id = %s RETURNING id, flag_count""",
        [session_id],
    )
    if not row:
        raise HTTPException(404, "session not found")
    return row


# ── B7: Human flag review tool ────────────────────────────────────────────────

@router.get("/review")
def list_for_review(
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """
    List proctored sessions requiring human review.

    Enumerates from enteri_ai_session (every conversational interview actually
    conducted), LEFT JOINed to proctoring_session -- not the reverse. A
    completed interview with no proctoring_session row at all previously had
    nowhere to show up on this screen (proctoring_session-only FROM clause),
    making a "didn't-run" case (candidate's browser silently failed to init
    proctoring -- ad-blocker, denied permission, script error) indistinguishable
    from "no proctored sessions yet" -- a recruiter had no way to notice the
    gap where they actually look. proctoring_status now makes that explicit:
    'ran' | 'declined' | 'did_not_run'.

    Phase 5, Fix 2 -- limit/offset mirror GET /appeals' exact pattern
    (Query(200, ge=1, le=500) / Query(0, ge=0), default 200 so existing
    frontend callers that don't pass either param see identical behaviour
    to before this fix -- sessions past 200 are now reachable via offset
    instead of silently invisible.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    scope_join = ""
    params: list = []
    if user["role"] == "recruiter":
        scope_join = (
            "JOIN requisition_recruiter rr_scope "
            "  ON rr_scope.requisition_id = r.id AND rr_scope.recruiter_id = %s"
        )
        params.append(user["sub"])

    params.append(user.get("tenant_id"))
    params += [limit, offset]

    return query(
        f"""
        SELECT COALESCE(ps.id, ns.id) AS id, ns.application_id,
               ps.consent_granted, COALESCE(ps.flag_count, 0) AS flag_count,
               (SELECT count(*) FROM jsonb_array_elements(COALESCE(ps.flags, '[]'::jsonb)) f
                WHERE f->>'type' = 'strike') AS strike_count,
               COALESCE((SELECT count(*) FROM proctoring_integrity_flag pif
                         WHERE pif.session_id = ps.id AND pif.reviewed = false), 0) AS unreviewed_integrity_count,
               ps.screen_recording_declined, ps.human_decision,
               COALESCE(ps.created_at, ns.completed_at, ns.created_at) AS created_at,
               ps.reviewed_at,
               CASE
                 WHEN ps.id IS NULL THEN 'did_not_run'
                 WHEN ps.proctoring_declined THEN 'declined'
                 ELSE 'ran'
               END AS proctoring_status,
               c.full_name AS candidate_name, r.title AS req_title
        FROM enteri_ai_session ns
        JOIN application a ON a.id = ns.application_id
        JOIN candidate  c ON c.id = a.candidate_id
        JOIN requisition r ON r.id = a.requisition_id
        LEFT JOIN proctoring_session ps ON ps.enteri_ai_session_id = ns.id
        {scope_join}
        WHERE ns.status IN ('completed', 'terminated_proctoring')
          AND r.tenant_id = %s
        ORDER BY COALESCE(ps.created_at, ns.completed_at, ns.created_at) DESC
        LIMIT %s OFFSET %s
        """,
        params,
    )


@router.get("/review/{session_id}")
def get_review(session_id: str, user: dict = Depends(get_current_user)):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req_id = _review_req_id_for_id(session_id)
    if req_id is None or not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "session not found")
    row = query_one(
        """
        SELECT ps.*, c.full_name AS candidate_name, r.title AS req_title,
               'ran' AS proctoring_status,
               ns.termination_reason, ns.score_detail
        FROM proctoring_session ps
        JOIN application a ON a.id = ps.application_id
        JOIN candidate  c ON c.id = a.candidate_id
        JOIN requisition r ON r.id = a.requisition_id
        LEFT JOIN enteri_ai_session ns ON ns.id = ps.enteri_ai_session_id
        WHERE ps.id = %s
        """,
        [session_id],
    )
    if row:
        row["proctoring_status"] = "declined" if row.get("proctoring_declined") else "ran"
        return row

    # No proctoring_session row at all -- this is the 'did_not_run' case
    # /review lists by enteri_ai_session.id when there's nothing else to key on.
    ns_row = query_one(
        """
        SELECT ns.id, ns.application_id, c.full_name AS candidate_name, r.title AS req_title
        FROM enteri_ai_session ns
        JOIN application a ON a.id = ns.application_id
        JOIN candidate  c ON c.id = a.candidate_id
        JOIN requisition r ON r.id = a.requisition_id
        WHERE ns.id = %s
        """,
        [session_id],
    )
    if not ns_row:
        raise HTTPException(404, "session not found")
    ns_row["proctoring_status"] = "did_not_run"
    return ns_row


class ReviewIn(BaseModel):
    reviewer_notes: Optional[str] = None
    human_decision: Optional[str] = None


@router.patch("/review/{session_id}")
def update_review(
    session_id: str, body: ReviewIn, user: dict = Depends(get_current_user)
):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req_id = _proctoring_session_req_id(session_id)
    if req_id is None or not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "session not found")
    sets, params = [], []
    if body.reviewer_notes is not None:
        sets.append("reviewer_notes = %s"); params.append(body.reviewer_notes)
    if body.human_decision is not None:
        allowed = {"cleared", "flagged_minor", "flagged_major", "voided"}
        if body.human_decision not in allowed:
            raise HTTPException(400, f"human_decision must be one of {sorted(allowed)}")
        sets.append("human_decision = %s"); params.append(body.human_decision)
        sets.append("reviewed_by = %s");    params.append(user["sub"])
        sets.append("reviewed_at = now()")
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(session_id)
    row = query_one(
        f"UPDATE proctoring_session SET {', '.join(sets)} WHERE id = %s RETURNING id, human_decision",
        params,
    )
    if not row:
        raise HTTPException(404, "session not found")
    return row


@router.get("/review/{session_id}/integrity-flags")
def list_integrity_flags(session_id: str, user: dict = Depends(get_current_user)):
    """Phase 4, Part D — every integrity flag for this session, newest first."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req_id = _proctoring_session_req_id(session_id)
    if req_id is None or not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "session not found")
    return query(
        """SELECT id, flag_kind, detail, created_at, reviewed, reviewed_by, reviewed_at
           FROM proctoring_integrity_flag
           WHERE session_id = %s
           ORDER BY created_at DESC""",
        [session_id],
    )


class IntegrityFlagReviewIn(BaseModel):
    reviewed: bool = True


@router.patch("/review/{session_id}/integrity-flags/{flag_id}")
def review_integrity_flag(
    session_id: str, flag_id: str, body: IntegrityFlagReviewIn,
    user: dict = Depends(get_current_user),
):
    """Phase 4, Part D — mark (or unmark) one integrity flag reviewed.
    Same reviewed_by/reviewed_at stamping style as PATCH /review/{id}."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req_id = _proctoring_session_req_id(session_id)
    if req_id is None or not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "session not found")
    row = query_one(
        """UPDATE proctoring_integrity_flag
               SET reviewed = %s,
                   reviewed_by = CASE WHEN %s THEN %s::uuid ELSE NULL END,
                   reviewed_at = CASE WHEN %s THEN now() ELSE NULL END
           WHERE id = %s AND session_id = %s
           RETURNING id, reviewed, reviewed_by, reviewed_at""",
        [body.reviewed, body.reviewed, user["sub"], body.reviewed, flag_id, session_id],
    )
    if not row:
        raise HTTPException(404, "integrity flag not found")
    return row


@router.get("/review/{session_id}/summary")
def download_summary(session_id: str, user: dict = Depends(get_current_user)):
    """Download a CSV incident summary for the session (B7)."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req_id = _proctoring_session_req_id(session_id)
    if req_id is None or not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "session not found")
    row = query_one(
        """SELECT ps.*, c.full_name, r.title
           FROM proctoring_session ps
           JOIN application a ON a.id = ps.application_id
           JOIN candidate c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE ps.id = %s""",
        [session_id],
    )
    if not row:
        raise HTTPException(404, "session not found")
    flags = row["flags"] if isinstance(row["flags"], list) else []
    lines = [
        "Enternly Enteri AI Proctoring — Incident Summary",
        f"Candidate: {row['full_name']}",
        f"Requisition: {row['title']}",
        f"Session ID: {row['id']}",
        f"Date: {row['created_at']}",
        f"Consent granted: {row['consent_granted']}",
        f"Screen recording declined: {row['screen_recording_declined']}",
        f"Total AI flags: {row['flag_count']}",
        f"Human decision: {row['human_decision'] or 'pending'}",
        f"Reviewer notes: {row['reviewer_notes'] or '—'}",
        "",
        "AI FLAG TIMELINE",
        "ts_ms,type,confidence,detail",
    ]
    for fl in flags:
        lines.append(f"{fl.get('ts_ms','')},{fl.get('type','')},{fl.get('confidence','')},{fl.get('detail','')}")
    lines += [
        "",
        "OUT OF SCOPE (requires specialist vendor / native app):",
        "  - Lockdown browser: needs installed native application",
        "  - Secondary-device detection: needs native + hardware signals",
        "  - VM blocking: needs native system access",
        "  - Government-ID face matching: specialist paid identity service",
        "  - Keystroke-dynamics identity: low reliability, skipped",
    ]
    content = "\n".join(lines)
    return StreamingResponse(
        io.BytesIO(content.encode()),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=proctor_summary_{session_id[:8]}.csv"},
    )


# ── B8: Media listing + streaming (recruiter-only, JWT-protected) ─────────────

@router.get("/{session_id}/media")
def list_media(session_id: str, user: dict = Depends(get_current_user)):
    """Return sorted lists of available webcam and screen chunk filenames."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req_id = _proctoring_session_req_id(session_id)
    if req_id is None or not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Session not found")
    result: dict = {"webcam": [], "screen": []}
    for kind in ("webcam", "screen"):
        result[kind] = [
            f"chunk_{c['chunk_index']:05d}{c['ext']}"
            for c in storage.list_chunks(session_id, kind)
        ]
    return result


_CHUNK_FNAME_RE = _re.compile(r"^chunk_(\d+)")


@router.get("/{session_id}/media/{kind}/{filename}")
def stream_media(
    session_id: str,
    kind: str,
    filename: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Stream a single video chunk with HTTP range support for in-browser playback."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req_id = _proctoring_session_req_id(session_id)
    if req_id is None or not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Chunk not found")
    if kind not in ("webcam", "screen"):
        raise HTTPException(400, "kind must be webcam or screen")
    safe = os.path.basename(filename)  # path-traversal guard
    m_idx = _CHUNK_FNAME_RE.match(safe)
    if not m_idx:
        raise HTTPException(404, "Chunk not found")
    chunk_index = int(m_idx.group(1))
    meta = storage.chunk_meta(session_id, kind, chunk_index)
    if not meta:
        raise HTTPException(404, "Chunk not found")

    file_size = meta["byte_size"]
    mime = meta["content_type"] or "video/webm"

    range_hdr = request.headers.get("range", "")
    m = _re.match(r"bytes=(\d+)-(\d*)", range_hdr)
    if m:
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else file_size - 1
        end = min(end, file_size - 1)
        chunk_len = end - start + 1
        data = storage.read_chunk_range(session_id, kind, chunk_index, start, chunk_len)
        return StreamingResponse(
            io.BytesIO(data),
            status_code=206,
            media_type=mime,
            headers={
                "Content-Range":  f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges":  "bytes",
                "Content-Length": str(chunk_len),
            },
        )

    data = storage.read_chunk_range(session_id, kind, chunk_index)
    return StreamingResponse(
        io.BytesIO(data),
        media_type=mime,
        headers={
            "Accept-Ranges":  "bytes",
            "Content-Length": str(file_size),
        },
    )


@router.get("/{session_id}/identity")
def stream_identity(session_id: str, user: dict = Depends(get_current_user)):
    """Stream the candidate identity snapshot image."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req_id = _proctoring_session_req_id(session_id)
    if req_id is None or not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "No identity snapshot for this session")
    identity = storage.read_identity(session_id)
    if not identity:
        raise HTTPException(404, "No identity snapshot for this session")
    mime = identity["content_type"] or (
        "image/png" if identity["ext"].lower() == ".png" else "image/jpeg"
    )
    return StreamingResponse(io.BytesIO(identity["data"]), media_type=mime)


# ── Helper ────────────────────────────────────────────────────────────────────

def _assert_consented(session_id: str):
    row = query_one(
        "SELECT consent_granted FROM proctoring_session WHERE id = %s", [session_id]
    )
    if not row:
        raise HTTPException(404, "proctoring session not found")
    if not row["consent_granted"]:
        raise HTTPException(403, "Consent not granted — cannot store proctoring data")


# ── Candidate-facing endpoints (token-auth, no JWT required) ─────────────────
# These mirror the recruiter JWT endpoints above but authenticate via the
# candidate's invite token. Existing recruiter endpoints are unchanged.

def _get_invite_for_token(token: str) -> dict:
    invite = query_one(
        "SELECT id, application_id, expires_at FROM enteri_ai_invite WHERE token = %s",
        [token],
    )
    if not invite:
        raise HTTPException(400, "Invalid interview token")
    exp = invite["expires_at"]
    if exp and exp.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(400, "Interview link has expired")
    return invite


def _candidate_owns_session(token: str, session_id: str) -> dict:
    """Verify token owner's application matches the proctoring session. Returns session row."""
    invite = _get_invite_for_token(token)
    row = query_one(
        "SELECT id, application_id, consent_granted FROM proctoring_session WHERE id = %s",
        [session_id],
    )
    if not row or str(row["application_id"]) != str(invite["application_id"]):
        raise HTTPException(403, "Not authorised")
    return row


def _get_or_create_session_secret(session_id: str) -> str:
    """
    Return the live secret for this proctoring session, minting one on first
    call. UNIQUE(session_id) on proctoring_session_key is what actually
    prevents duplicates under a race (two concurrent /candidate/init calls) —
    the INSERT ... ON CONFLICT DO NOTHING + re-select handles that safely
    without needing a transaction/lock here.
    """
    row = query_one(
        "SELECT session_secret FROM proctoring_session_key WHERE session_id = %s",
        [session_id],
    )
    if row:
        return row["session_secret"]
    query(
        """INSERT INTO proctoring_session_key (session_id, session_secret)
           VALUES (%s, %s)
           ON CONFLICT (session_id) DO NOTHING""",
        [session_id, secrets.token_urlsafe(32)],
        fetch=False,
    )
    row = query_one(
        "SELECT session_secret FROM proctoring_session_key WHERE session_id = %s",
        [session_id],
    )
    return row["session_secret"]


@router.post("/candidate/init")
def candidate_init_session(token: str):
    """Public — create or retrieve the proctoring session for this invite token."""
    invite = _get_invite_for_token(token)
    existing = query_one(
        "SELECT id, consent_granted FROM proctoring_session WHERE application_id = %s",
        [str(invite["application_id"])],
    )
    if existing:
        session_id = str(existing["id"])
        return {
            "proctoring_session_id": session_id,
            "consent_granted": bool(existing["consent_granted"]),
            "session_secret": _get_or_create_session_secret(session_id),
        }
    row = query_one(
        "INSERT INTO proctoring_session (application_id) VALUES (%s) RETURNING id, consent_granted",
        [str(invite["application_id"])],
    )
    session_id = str(row["id"])
    return {
        "proctoring_session_id": session_id,
        "consent_granted": False,
        "session_secret": _get_or_create_session_secret(session_id),
    }


@router.post("/candidate/{session_id}/consent")
def candidate_record_consent(session_id: str, body: ConsentIn, token: str):
    """Public — record proctoring consent from the candidate page."""
    _candidate_owns_session(token, session_id)
    retention_until = datetime.utcnow() + timedelta(days=body.retention_days)
    row = query_one(
        """UPDATE proctoring_session
           SET consent_granted = %s,
               proctoring_declined = %s,
               consent_text = %s,
               consented_at = now(),
               retention_until = %s
           WHERE id = %s
           RETURNING id, consent_granted""",
        [body.granted, not body.granted, body.consent_text, retention_until, session_id],
    )
    if not row:
        raise HTTPException(404, "Session not found")
    return row


@router.post("/candidate/{session_id}/identity")
async def candidate_identity_snapshot(
    session_id: str,
    snapshot: UploadFile = File(...),
    token: str = Query(...),
):
    """Public — upload identity snapshot from the candidate page."""
    row = _candidate_owns_session(token, session_id)
    if not row["consent_granted"]:
        raise HTTPException(403, "Consent not granted")
    ext = os.path.splitext(snapshot.filename or "")[1] or ".jpg"
    storage.save_identity(
        session_id, await snapshot.read(), ext,
        snapshot.content_type or "image/jpeg",
    )
    return {"saved": True}


@router.post("/candidate/{session_id}/media-chunk")
async def candidate_media_chunk(
    session_id: str,
    media_type: str = Form(...),
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...),
    token: str = Query(...),
):
    """Public — receive a webcam or screen chunk from the candidate page."""
    row = _candidate_owns_session(token, session_id)
    if not row["consent_granted"]:
        raise HTTPException(403, "Consent not granted")
    if media_type not in ("webcam", "screen"):
        raise HTTPException(400, "media_type must be 'webcam' or 'screen'")
    ext = os.path.splitext(chunk.filename or "")[1] or ".webm"
    storage.save_chunk(
        session_id, media_type, chunk_index, await chunk.read(), ext,
        chunk.content_type or "video/webm",
    )
    col = "webcam_video_path" if media_type == "webcam" else "screen_video_path"
    query_one(
        f"UPDATE proctoring_session SET {col} = %s WHERE id = %s RETURNING id",
        [f"{session_id}/{media_type}", session_id],
    )
    return {"saved": True, "chunk": chunk_index}


@router.post("/candidate/{session_id}/flags")
def candidate_submit_flags(session_id: str, body: FlagsIn, token: str):
    """Public — submit AI behaviour flags from the candidate page."""
    row = _candidate_owns_session(token, session_id)
    if not row["consent_granted"]:
        raise HTTPException(403, "Consent not granted")
    # Atomic append at the DB level — see submit_flags() for why.
    updated = query_one(
        """UPDATE proctoring_session
           SET flags = flags || %s::jsonb, flag_count = flag_count + %s
           WHERE id = %s
           RETURNING flag_count""",
        [json.dumps(body.flags), len(body.flags), session_id],
    )
    return {"flag_count": updated["flag_count"] if updated else len(body.flags)}


class _LinkSessionIn(BaseModel):
    enteri_ai_session_id: str


@router.post("/candidate/{session_id}/link")
def candidate_link_session(session_id: str, body: _LinkSessionIn, token: str):
    """Public — link proctoring session to enteri_ai_session_id after /invite/begin."""
    _candidate_owns_session(token, session_id)
    query(
        "UPDATE proctoring_session SET enteri_ai_session_id = %s WHERE id = %s",
        [body.enteri_ai_session_id, session_id],
        fetch=False,
    )
    return {"linked": True}


@router.post("/candidate/{session_id}/recording-started")
def candidate_recording_started(session_id: str, token: str):
    """
    Public — Phase 5, Fix 4. Reported by interview.html right after
    _startProcRecorders() actually starts the webcam/screen MediaRecorders
    (i.e. camera/screen permissions were genuinely granted), distinct from
    consent_granted (recorded the moment the candidate clicks through,
    before permissions are even requested). Not strike/vision logic, so it
    runs regardless of PROCTORING_AI_ENABLED, same as device/heartbeat
    reporting. Idempotent: only ever set once (first call wins), so a
    reconnect/resume doesn't overwrite the original start time.
    """
    _candidate_owns_session(token, session_id)
    row = query_one(
        """UPDATE proctoring_session
               SET recording_started_at = now()
           WHERE id = %s AND recording_started_at IS NULL
           RETURNING id, recording_started_at""",
        [session_id],
    )
    if not row:
        # Either already recorded (idempotent no-op) or session not found --
        # re-check which, to keep 404 meaningful for a genuinely bad session_id.
        existing = query_one("SELECT recording_started_at FROM proctoring_session WHERE id = %s", [session_id])
        if not existing:
            raise HTTPException(404, "Session not found")
        return {"recording_started_at": existing["recording_started_at"].isoformat()}
    return {"recording_started_at": row["recording_started_at"].isoformat()}


class CompleteIn(BaseModel):
    final_flush_ok: Optional[bool] = None


@router.post("/candidate/{session_id}/complete")
def candidate_complete_session(session_id: str, token: str, body: CompleteIn = CompleteIn()):
    """Public — mark proctoring session complete when the interview ends."""
    _candidate_owns_session(token, session_id)
    row = query_one(
        """UPDATE proctoring_session
           SET proctoring_complete = TRUE
           WHERE id = %s RETURNING id, flag_count""",
        [session_id],
    )
    if not row:
        raise HTTPException(404, "Session not found")
    if body.final_flush_ok is False:
        # The candidate's browser reported the last flag batch never confirmed
        # -- append a marker flag so a reviewer sees the flag list may be
        # incomplete, instead of a clean-looking close that silently isn't.
        query(
            """UPDATE proctoring_session
               SET flags = flags || %s::jsonb, flag_count = flag_count + 1
               WHERE id = %s""",
            [json.dumps([{"type": "final_flush_failed", "ts_ms": None}]), session_id],
            fetch=False,
        )
    # Phase 4, Part B — scan for monitoring gaps at the natural end-of-session
    # point, so a tampering signal gets recorded even for sessions that
    # complete normally (not just ones that go through the gated judge/
    # terminate path). Best-effort: a scorer error must not block completion.
    try:
        scorer.record_monitoring_gaps(session_id)
    except Exception as exc:
        print(f"[proctoring] record_monitoring_gaps failed for {session_id}: {exc}")
    # Phase 4, Part C — send the digest (a no-op if there's nothing new to
    # report; see proctoring_alerts.send_integrity_digest_for_session).
    try:
        from ..services import proctoring_alerts as _alerts
        _alerts.send_integrity_digest_for_session(session_id)
    except Exception as exc:
        print(f"[proctoring] integrity digest failed for {session_id}: {exc}")
    return row


# ── Server-side proctoring ledger (Phase 2, session-secret auth) ────────────
# Separate and additive to /candidate/{id}/flags above (that JSONB-append
# path is untouched and keeps working exactly as before). These endpoints
# write into the Phase-1 tables (proctoring_flag_ledger / _session_key /
# _session_state / _pause_event) so a later phase can corroborate or move
# the strike/termination decision server-side instead of trusting the
# browser's self-reported strike_count. Auth is the per-session secret
# minted by /candidate/init (Part A), not the candidate's invite token —
# the browser holds the secret for the life of the proctoring loop and
# doesn't need to re-send the token on every tick.
#
# Record-only in this phase: none of these compute strikes or terminate
# anything. PROCTORING_AI_ENABLED stays false; nothing calls these live yet.

_SECRET_MISUSE_WINDOW_SECONDS = 300  # Phase 4, Part E — rate-limit bucket width


def _record_secret_misuse(session_id: str, endpoint: str):
    """
    Best-effort; a logging failure must never turn into a second error on top
    of the 403 the caller is already about to raise. dedupe_key buckets by a
    5-minute window so repeated hammering against the same session creates
    ONE flag per window, not one per request.
    """
    try:
        bucket = int(datetime.utcnow().timestamp() // _SECRET_MISUSE_WINDOW_SECONDS)
        fid = scorer.record_integrity_flag(
            session_id, "secret_misuse",
            {"endpoint": endpoint, "detected_at": datetime.utcnow().isoformat()},
            dedupe_key=f"window_{bucket}",
        )
        if fid:
            from ..services import proctoring_alerts as _alerts
            _alerts.send_integrity_digest_for_session(session_id)
    except Exception as exc:
        print(f"[proctoring] record_secret_misuse failed for {session_id}: {exc}")


def _require_session_secret(session_id: str, session_secret: str, endpoint: str = ""):
    row = query_one(
        "SELECT session_secret, revoked FROM proctoring_session_key WHERE session_id = %s",
        [session_id],
    )
    if not row:
        # No key exists for this session_id at all -- nothing real to have
        # misused (either the session_id itself is bogus, or /candidate/init
        # was never called for it). Reject, but don't flag.
        raise HTTPException(403, "Invalid or revoked session secret")
    if row["revoked"] or not session_secret or row["session_secret"] != session_secret:
        # A REAL session/key exists and what was presented against it was
        # wrong or revoked -- genuine misuse, not a typo against nothing.
        _record_secret_misuse(session_id, endpoint)
        raise HTTPException(403, "Invalid or revoked session secret")


class FlagLedgerIn(BaseModel):
    flag_type: str
    tick_index: int
    client_timestamp: Optional[datetime] = None
    counts_as_strike: bool = False
    session_secret: str


@router.post("/session/{session_id}/flag", status_code=201)
def submit_ledger_flag(session_id: str, body: FlagLedgerIn):
    """Public (session-secret auth) — server-side flag ledger. Record-only."""
    _require_session_secret(session_id, body.session_secret, endpoint="/session/{id}/flag")
    query(
        """INSERT INTO proctoring_flag_ledger
               (session_id, flag_type, tick_index, client_timestamp, counts_as_strike, raw_payload)
           VALUES (%s, %s, %s, %s, %s, %s::jsonb)
           ON CONFLICT (session_id, flag_type, tick_index) DO NOTHING""",
        [session_id, body.flag_type, body.tick_index, body.client_timestamp,
         body.counts_as_strike, body.json()],
        fetch=False,
    )
    return {"recorded": True}


_VALID_DEVICE_TYPES = ("laptop", "phone", "unknown")


class DeviceIn(BaseModel):
    device_type: str
    session_secret: str


@router.post("/session/{session_id}/device")
def submit_device_type(session_id: str, body: DeviceIn):
    """Public (session-secret auth) — record/update the candidate's detected device type."""
    _require_session_secret(session_id, body.session_secret, endpoint="/session/{id}/device")
    if body.device_type not in _VALID_DEVICE_TYPES:
        raise HTTPException(400, f"device_type must be one of {_VALID_DEVICE_TYPES}")
    query(
        """INSERT INTO proctoring_session_state (session_id, device_type)
           VALUES (%s, %s)
           ON CONFLICT (session_id) DO UPDATE SET device_type = EXCLUDED.device_type""",
        [session_id, body.device_type],
        fetch=False,
    )
    return {"device_type": body.device_type}


class PauseIn(BaseModel):
    pause_reason: str
    session_secret: str


@router.post("/session/{session_id}/pause", status_code=201)
def start_pause_event(session_id: str, body: PauseIn):
    """Public (session-secret auth) — record the start of a legitimate interview pause."""
    _require_session_secret(session_id, body.session_secret, endpoint="/session/{id}/pause")
    row = query_one(
        """INSERT INTO proctoring_pause_event (session_id, pause_reason, raw_payload)
           VALUES (%s, %s, %s::jsonb)
           RETURNING id""",
        [session_id, body.pause_reason, body.json()],
    )
    pause_id = str(row["id"])
    resp = {"pause_id": pause_id}
    if body.pause_reason == "low_light":
        # Phase 3, Part C — tell the browser its 5-minute deadline up front so
        # it can show a countdown without polling immediately.
        timeout = scorer.check_low_light_pause_timeout(session_id)
        resp["deadline_at"] = timeout["deadline_at"]
        resp["remaining_seconds"] = timeout["remaining_seconds"]
    return resp


@router.get("/session/{session_id}/pause-status")
def poll_low_light_pause_status(session_id: str, session_secret: str):
    """
    Public (session-secret auth) — Phase 3, Part C polling endpoint. Reports
    the remaining time on the session's current open low_light pause (if
    any), computed server-side so the browser doesn't rely on its own clock.
    Read-only; does not close or time out the pause itself.
    """
    _require_session_secret(session_id, session_secret, endpoint="/session/{id}/pause-status")
    return scorer.check_low_light_pause_timeout(session_id)


class ResumeIn(BaseModel):
    pause_id: str
    session_secret: str


@router.post("/session/{session_id}/resume")
def resume_pause_event(session_id: str, body: ResumeIn):
    """Public (session-secret auth) — mark a pause event as resumed."""
    _require_session_secret(session_id, body.session_secret, endpoint="/session/{id}/resume")
    row = query_one(
        """UPDATE proctoring_pause_event
               SET resumed_at = now()
           WHERE id = %s AND session_id = %s AND resumed_at IS NULL
           RETURNING id, resumed_at""",
        [body.pause_id, session_id],
    )
    if not row:
        raise HTTPException(404, "Pause event not found, already resumed, or does not belong to this session")
    return {"pause_id": str(row["id"]), "resumed_at": row["resumed_at"].isoformat()}


class HeartbeatIn(BaseModel):
    session_secret: str


@router.post("/session/{session_id}/heartbeat", status_code=201)
def submit_heartbeat(session_id: str, body: HeartbeatIn):
    """
    Public (session-secret auth) — Phase 3, Part D. Lightweight "still
    watching" signal, sent by the browser on a steady interval while the
    interview is active and not paused. Record-only; gap detection
    (services/proctoring_scorer.detect_monitoring_gaps) is a separate
    read-only query over this history, run on demand, not from here.
    """
    _require_session_secret(session_id, body.session_secret, endpoint="/session/{id}/heartbeat")
    query(
        "INSERT INTO proctoring_heartbeat (session_id) VALUES (%s)",
        [session_id], fetch=False,
    )
    return {"recorded": True}
