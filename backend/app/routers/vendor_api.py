"""
Vendor Management API — sourcing partner companies and their portal.

Internal endpoints (recruiter / ta_manager / admin):
  register vendors, add users, open reqs, suspend access.

Portal endpoints (vendor JWT):
  see reqs opened to them, submit CVs, track submissions.

Auth split:
  - Internal routes  → get_current_user (staff JWT, role checked)
  - Portal routes    → get_current_vendor (vendor JWT with account_type='vendor')
  - Vendor login     → public (no JWT required — listed in main._PUBLIC)

Token flow reuses password_api._issue_token with account_type='vendor'
so the /set-password page works for vendor users without any new page.
"""
import os
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from ..auth_utils import (
    SECRET_KEY, ALGORITHM, AUD_VENDOR, get_current_user,
    hash_password, verify_password, is_company_tier,
)
from ..db import query, query_one
from ..module_access import recruiter_has_module
from ..routers.password_api import issue_invite_for_external_user
from ..services import excel_export
from ..services.activity_log import log_activity
from ..services.period import period_start as _period_start
from . import reports_api as _rp

router = APIRouter(prefix="/api/vendors", tags=["vendors"])

_ALLOWED_RESUME = {".pdf", ".docx", ".doc"}
_bearer = HTTPBearer(auto_error=False)
_VENDOR_TOKEN_HOURS = 8


# ── Vendor JWT helpers ────────────────────────────────────────────────────────

def _create_vendor_token(vu: dict) -> str:
    expire = datetime.utcnow() + timedelta(hours=_VENDOR_TOKEN_HOURS)
    return jwt.encode(
        {
            "sub":          str(vu["id"]),
            "email":        vu["email"],
            "vendor_id":    str(vu["vendor_id"]),
            "name":         vu["full_name"],
            "account_type": "vendor",
            "aud":          AUD_VENDOR,
            "exp":          expire,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def get_current_vendor(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Dependency: resolve a vendor JWT → payload dict.
    Mirrors get_current_user but checks account_type='vendor'."""
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM], audience=AUD_VENDOR)
    except JWTError:
        # grace: legacy vendor token has no aud but account_type='vendor'
        try:
            legacy = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_aud": False})
        except JWTError:
            raise HTTPException(401, "Invalid or expired vendor token")
        if legacy.get("aud"):          # has a non-vendor aud → reject
            raise HTTPException(403, "Vendor access only")
        if legacy.get("account_type") != "vendor":
            raise HTTPException(403, "Vendor access only")
        payload = legacy
    if payload.get("account_type") != "vendor":
        raise HTTPException(403, "Vendor access only")
    # Re-check suspension on every request — otherwise a suspended vendor's
    # still-valid token (up to the 8h TTL) keeps working until it expires.
    if not query_one(
        "SELECT id FROM vendor WHERE id=%s AND status='active'",
        [payload.get("vendor_id")],
    ):
        raise HTTPException(403, "Vendor account is suspended or no longer exists")
    # Same re-check, but for the individual vendor user — otherwise a user
    # suspended mid-session keeps their token working until it expires.
    if not query_one(
        "SELECT id FROM vendor_user WHERE id=%s AND is_active=TRUE",
        [payload.get("sub")],
    ):
        raise HTTPException(403, "Vendor user access has been suspended")
    return payload


def _require_internal(user: dict = Depends(get_current_user)) -> dict:
    if is_company_tier(user):
        return user
    if user.get("role") == "recruiter" and recruiter_has_module(user.get("sub"), "vendors"):
        return user
    raise HTTPException(403, "Company Admin or delegated Recruiter access required")


def _ensure_candidate_portal_invite(cand_id: str, email: str, full_name: str) -> None:
    """
    Give a vendor-sourced candidate access to the candidate portal.
    Mirrors main._maybe_issue_candidate_invite but defined locally to avoid a
    circular import (main.py imports this router at load time). Idempotent:
    if a candidate_user already exists, does nothing.
    """
    try:
        if query_one("SELECT id FROM candidate_user WHERE candidate_id=%s", [cand_id]):
            return
        cand_row = query_one("SELECT tenant_id FROM candidate WHERE id=%s", [cand_id])
        tenant_id = (cand_row or {}).get("tenant_id")
        cu = query_one(
            """INSERT INTO candidate_user (candidate_id, email, tenant_id)
               VALUES (%s, %s, %s) ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id""",
            [cand_id, email.lower().strip(), tenant_id],
        )
        if cu:
            issue_invite_for_external_user(str(cu["id"]), email, full_name, "candidate", tenant_id=tenant_id)
    except Exception as exc:
        print(f"[vendor-submit] Candidate portal invite failed for {email}: {exc}")


def _notify_duplicate_submission(req_id: str, vendor_id: str, candidate_name: str) -> None:
    """
    Bell notification to the owning recruiter + every TA manager when a vendor
    submits a candidate who's already in the pipeline for this requisition.
    Best-effort: notify() itself swallows failures, and any error resolving
    recipients here must not block the 409 response to the vendor.
    """
    try:
        from ..services.notifications import notify
        from .scheduling_api import _owning_recruiter_id
        req = query_one("SELECT title FROM requisition WHERE id=%s", [req_id])
        req_title = (req or {}).get("title") or "a requisition"
        v = query_one("SELECT name FROM vendor WHERE id=%s", [vendor_id])
        vendor_name = (v or {}).get("name") or "A vendor"
        title = f"Duplicate CV from vendor — {candidate_name}"
        body = (
            f"{vendor_name} submitted '{candidate_name}' for '{req_title}', but this "
            f"candidate has already applied to this requisition."
        )
        recipients = set()
        recruiter_id = _owning_recruiter_id(req_id)
        if recruiter_id:
            recipients.add(recruiter_id)
        for row in (query("SELECT id FROM app_user WHERE role='ta_manager' AND is_active=TRUE", []) or []):
            recipients.add(str(row["id"]))
        for recipient_id in recipients:
            notify(
                recipient_id, "vendor_duplicate_submission", title,
                body=body, requisition_id=req_id,
                action_url=f"/?openReqDetail={req_id}",
            )
    except Exception as exc:
        print(f"[vendor-submit] Duplicate-CV notification failed for req {req_id}: {exc}")


# ── Vendor login (public endpoint) ───────────────────────────────────────────

class VendorLoginIn(BaseModel):
    email: str
    password: str


@router.post("/portal/login")
def vendor_login(body: VendorLoginIn):
    """Public. Vendor user logs in; receives a short-lived JWT."""
    vu = query_one(
        """SELECT vu.id, vu.vendor_id, vu.email, vu.full_name,
                  vu.password_hash, vu.is_active
           FROM vendor_user vu
           JOIN vendor v ON v.id = vu.vendor_id
           WHERE LOWER(vu.email) = %s
             AND vu.is_active = TRUE
             AND v.status = 'active'""",
        [body.email.lower().strip()],
    )
    if not vu or not verify_password(body.password, vu.get("password_hash") or ""):
        raise HTTPException(401, "Invalid credentials")
    return {
        "token": _create_vendor_token(vu),
        "name":  vu["full_name"],
        "email": vu["email"],
    }


# ── Internal: register a new vendor + first user ─────────────────────────────

class RegisterVendorIn(BaseModel):
    name: str
    contact_email: str
    contact_phone: Optional[str] = None
    first_user_name: str
    first_user_email: str


@router.post("/")
def register_vendor(body: RegisterVendorIn, user: dict = Depends(_require_internal)):
    """Create a vendor company + its first login account; email the invite link."""
    from ..services.email_validation import assert_real_email
    try:
        first_user_email = assert_real_email(body.first_user_email)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    tenant_id = user.get("tenant_id")
    existing = query_one(
        "SELECT id FROM vendor_user WHERE tenant_id = %s AND LOWER(email) = %s",
        [tenant_id, first_user_email],
    )
    if existing:
        raise HTTPException(409, "A vendor user with that email already exists")

    vendor = query_one(
        """INSERT INTO vendor (name, contact_email, contact_phone, created_by, tenant_id)
           VALUES (%s, %s, %s, %s, %s) RETURNING id, name""",
        [body.name, body.contact_email, body.contact_phone, user["sub"], tenant_id],
    )

    vu = query_one(
        """INSERT INTO vendor_user (vendor_id, full_name, email, tenant_id)
           VALUES (%s, %s, %s, %s) RETURNING id, email, full_name""",
        [str(vendor["id"]), body.first_user_name, first_user_email, tenant_id],
    )

    invite_link = None
    try:
        raw = issue_invite_for_external_user(
            str(vu["id"]), vu["email"], vu["full_name"], "vendor", tenant_id=tenant_id
        )
        from ..routers.password_api import _base_url
        invite_link = f"{_base_url(tenant_id)}/set-password?token={raw}"
    except Exception as exc:
        print(f"[vendor] Invite email failed for {vu['email']}: {exc}")

    log_activity(
        "vendor", "vendor_registered",
        entity_id=vendor["id"], actor_id=user["sub"], actor_role=user.get("role"),
        detail={"vendor_name": vendor["name"], "first_user_email": vu["email"]},
    )

    return {
        "vendor_id":   str(vendor["id"]),
        "vendor_name": vendor["name"],
        "user_id":     str(vu["id"]),
        "invite_link": invite_link,
    }


# ── Internal: add another login under an existing vendor ─────────────────────

class AddVendorUserIn(BaseModel):
    full_name: str
    email: str


@router.post("/{vendor_id}/users")
def add_vendor_user(
    vendor_id: str,
    body: AddVendorUserIn,
    user: dict = Depends(_require_internal),
):
    tenant_id = user.get("tenant_id")
    vendor = query_one(
        "SELECT id FROM vendor WHERE id=%s AND tenant_id=%s AND status='active'", [vendor_id, tenant_id]
    )
    if not vendor:
        raise HTTPException(404, "Vendor not found or suspended")

    from ..services.email_validation import assert_real_email
    try:
        email = assert_real_email(body.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    existing = query_one(
        "SELECT id FROM vendor_user WHERE tenant_id = %s AND LOWER(email) = %s",
        [tenant_id, email],
    )
    if existing:
        raise HTTPException(409, "A vendor user with that email already exists")

    vu = query_one(
        """INSERT INTO vendor_user (vendor_id, full_name, email, tenant_id)
           VALUES (%s, %s, %s, %s) RETURNING id, email, full_name""",
        [vendor_id, body.full_name, email, tenant_id],
    )

    invite_link = None
    try:
        raw = issue_invite_for_external_user(
            str(vu["id"]), vu["email"], vu["full_name"], "vendor", tenant_id=tenant_id
        )
        from ..routers.password_api import _base_url
        invite_link = f"{_base_url(tenant_id)}/set-password?token={raw}"
    except Exception as exc:
        print(f"[vendor] Invite email failed for {vu['email']}: {exc}")

    log_activity(
        "vendor", "vendor_user_added",
        entity_id=vendor_id, actor_id=user["sub"], actor_role=user.get("role"),
        detail={"email": email, "full_name": body.full_name},
    )

    return {"user_id": str(vu["id"]), "invite_link": invite_link}


# ── Internal: list all vendors ────────────────────────────────────────────────

@router.get("/")
def list_vendors(user: dict = Depends(_require_internal)):
    return query(
        """SELECT v.id, v.name, v.contact_email, v.contact_phone,
                  v.status, v.created_at,
                  COUNT(vu.id) FILTER (WHERE vu.is_active) AS user_count
           FROM vendor v
           LEFT JOIN vendor_user vu ON vu.vendor_id = v.id
           WHERE v.tenant_id = %s
           GROUP BY v.id
           ORDER BY v.created_at DESC""",
        [user.get("tenant_id")],
    )


# ── Internal: suspend or reactivate a vendor ─────────────────────────────────

class PatchVendorIn(BaseModel):
    status: str


@router.patch("/{vendor_id}")
def patch_vendor(
    vendor_id: str,
    body: PatchVendorIn,
    user: dict = Depends(_require_internal),
):
    if body.status not in ("active", "suspended"):
        raise HTTPException(400, "status must be 'active' or 'suspended'")
    if not query_one("SELECT id FROM vendor WHERE id=%s AND tenant_id=%s", [vendor_id, user.get("tenant_id")]):
        raise HTTPException(404, "Vendor not found")
    query(
        "UPDATE vendor SET status=%s WHERE id=%s",
        [body.status, vendor_id], fetch=False,
    )
    return {"ok": True, "status": body.status}


# ── Internal: suspend or reactivate ONE vendor user (not the whole vendor) ───

def _assert_ta_or_admin(user: dict) -> None:
    if not is_company_tier(user):
        raise HTTPException(403, "Company Admin access required")


@router.patch("/{vendor_id}/users/{user_id}/suspend")
def suspend_vendor_user(vendor_id: str, user_id: str, user: dict = Depends(get_current_user)):
    _assert_ta_or_admin(user)
    if not query_one(
        """SELECT vu.id FROM vendor_user vu
           JOIN vendor v ON v.id = vu.vendor_id
           WHERE vu.id=%s AND vu.vendor_id=%s AND v.tenant_id=%s""",
        [user_id, vendor_id, user.get("tenant_id")],
    ):
        raise HTTPException(404, "Vendor user not found")
    query("UPDATE vendor_user SET is_active=FALSE WHERE id=%s", [user_id], fetch=False)
    log_activity(
        "vendor", "vendor_user_suspended",
        entity_id=user_id, actor_id=user["sub"], actor_role=user.get("role"),
    )
    return {"ok": True, "is_active": False}


@router.patch("/{vendor_id}/users/{user_id}/reactivate")
def reactivate_vendor_user(vendor_id: str, user_id: str, user: dict = Depends(get_current_user)):
    _assert_ta_or_admin(user)
    if not query_one(
        """SELECT vu.id FROM vendor_user vu
           JOIN vendor v ON v.id = vu.vendor_id
           WHERE vu.id=%s AND vu.vendor_id=%s AND v.tenant_id=%s""",
        [user_id, vendor_id, user.get("tenant_id")],
    ):
        raise HTTPException(404, "Vendor user not found")
    query("UPDATE vendor_user SET is_active=TRUE WHERE id=%s", [user_id], fetch=False)
    return {"ok": True, "is_active": True}


# ── Internal: list vendors currently assigned to a requisition ───────────────

@router.get("/requisitions/{req_id}/vendors")
def list_req_vendors(req_id: str, user: dict = Depends(_require_internal)):
    if not query_one(
        "SELECT id FROM requisition WHERE id=%s AND tenant_id=%s",
        [req_id, user.get("tenant_id")],
    ):
        raise HTTPException(404, "Requisition not found")
    return query(
        """SELECT v.id, v.name, v.status, rv.opened_at,
                  (SELECT COUNT(*) FROM application a
                   WHERE a.requisition_id = %s
                     AND a.source = 'vendor:' || v.id::text) AS submissions
           FROM requisition_vendor rv
           JOIN vendor v ON v.id = rv.vendor_id
           WHERE rv.requisition_id = %s
           ORDER BY rv.opened_at DESC""",
        [req_id, req_id],
    )


# ── Internal: open a requisition to one or more vendors ──────────────────────

class OpenReqIn(BaseModel):
    vendor_ids: List[str]


@router.post("/requisitions/{req_id}/open")
def open_req_to_vendors(
    req_id: str,
    body: OpenReqIn,
    user: dict = Depends(_require_internal),
):
    if not query_one("SELECT id FROM requisition WHERE id=%s", [req_id]):
        raise HTTPException(404, "Requisition not found")
    if not body.vendor_ids:
        return {"ok": True, "opened": []}
    # One SELECT to validate every vendor id at once, then one batched INSERT,
    # instead of a per-id SELECT + INSERT round trip (was O(n) queries for n ids).
    valid_ids = {
        str(r["id"]) for r in (query(
            "SELECT id FROM vendor WHERE id = ANY(%s::uuid[]) AND status='active'",
            [body.vendor_ids],
        ) or [])
    }
    opened = [vid for vid in body.vendor_ids if vid in valid_ids]
    if opened:
        query(
            """INSERT INTO requisition_vendor (requisition_id, vendor_id, opened_by)
               SELECT %s, v, %s FROM unnest(%s::uuid[]) AS v
               ON CONFLICT (requisition_id, vendor_id) DO NOTHING""",
            [req_id, user["sub"], opened], fetch=False,
        )
    return {"ok": True, "opened": opened}


# ── Internal: remove vendor access to a req ──────────────────────────────────

@router.delete("/requisitions/{req_id}/vendors/{vendor_id}")
def close_req_vendor(
    req_id: str,
    vendor_id: str,
    user: dict = Depends(_require_internal),
):
    query(
        "DELETE FROM requisition_vendor WHERE requisition_id=%s AND vendor_id=%s",
        [req_id, vendor_id], fetch=False,
    )
    return {"ok": True}


# ── Portal: list reqs opened to this vendor ───────────────────────────────────

@router.get("/portal/requisitions")
def portal_list_reqs(vendor: dict = Depends(get_current_vendor)):
    vid = vendor["vendor_id"]
    source_tag = f"vendor:{vid}"
    return query(
        """SELECT r.id, r.req_code, r.title, r.status, r.roll_type,
                  r.hiring_location, r.project, r.min_experience, r.max_experience,
                  r.openings, r.priority, r.is_fresher_role,
                  r.job_description, r.screening_questions,
                  r.key_skills, b.code AS band, bu.name AS business_unit,
                  rv.opened_at,
                  (SELECT COUNT(*) FROM application a
                   WHERE a.requisition_id = r.id
                     AND a.source = %s) AS my_submissions
           FROM requisition_vendor rv
           JOIN requisition r  ON r.id  = rv.requisition_id
           JOIN band b         ON b.id  = r.band_id
           JOIN business_unit bu ON bu.id = r.bu_id
           WHERE rv.vendor_id = %s
             AND r.status = 'open'
           ORDER BY rv.opened_at DESC""",
        [source_tag, vid],
    )


# ── Portal: submit a CV to an opened req ─────────────────────────────────────

@router.post("/portal/requisitions/{req_id}/submit")
async def portal_submit_cv(
    req_id: str,
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    gender: str = Form("undisclosed"),
    years_experience: float = Form(None),
    current_company: str = Form(""),
    file: UploadFile = File(...),
    vendor: dict = Depends(get_current_vendor),
):
    """
    Vendor uploads a candidate CV.  Creates candidate + application with
    source = 'vendor:<id>'.  Enters the standard intake_and_screen pipeline.
    """
    vid = vendor["vendor_id"]

    # Enforce: this req must be opened to the calling vendor
    if not query_one(
        "SELECT id FROM requisition_vendor WHERE requisition_id=%s AND vendor_id=%s",
        [req_id, vid],
    ):
        raise HTTPException(403, "This requisition is not opened to your vendor")

    req = query_one(
        "SELECT id, approval_status, tenant_id FROM requisition WHERE id=%s", [req_id]
    )
    if not req:
        raise HTTPException(404, "Requisition not found")
    if (req.get("approval_status") or "approved") != "approved":
        raise HTTPException(403, "Requisition is not open for applications")

    from ..services.pipeline import _check_no_poach_block, NoPoachBlockedError
    try:
        _check_no_poach_block(current_company or None, req_id)
    except NoPoachBlockedError as exc:
        raise HTTPException(409, str(exc))

    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in _ALLOWED_RESUME:
        raise HTTPException(400, f"Unsupported file type '{suffix}'. Upload PDF or Word.")

    file_bytes = await file.read()
    try:
        from ..services.resume_parser import extract_text as _parse_resume
        resume_text, _ = _parse_resume(file_bytes, file.filename or "")
    except NotImplementedError:
        raise HTTPException(422, "Image files are not supported; upload PDF or Word.")

    source_tag = f"vendor:{vid}"

    # Dedup or create candidate — matches by email OR normalised phone, same
    # as the career-site apply flow, so a vendor can't resubmit a candidate
    # who already applied (directly or via another vendor) under a different email.
    from ..services.candidate_dedup import dedup_or_create_candidate
    try:
        cand_id = str(dedup_or_create_candidate(
            full_name=full_name,
            email=email,
            phone=phone or None,
            gender=gender,
            source=source_tag,
            resume_url=None,
            requisition_id=req_id,
        ))
    except HTTPException as exc:
        if exc.status_code == 409:
            _notify_duplicate_submission(req_id, vid, full_name)
            raise HTTPException(409, {"error_code": "duplicate_candidate", "message": exc.detail})
        raise

    # Enter the standard pipeline -- current_company feeds the no-poach check
    # inside intake_and_screen() same as the career-site apply flow.
    from ..services.pipeline import intake_and_screen
    current_company = current_company.strip() or None
    app_row = intake_and_screen(
        req_id, cand_id, resume_text, years_experience, len(file_bytes),
        current_company=current_company,
    )

    # Tag the application source (vendor:<id>) and persist current_company
    # so it's visible alongside every other application, same as career-site.
    query(
        "UPDATE application SET source=%s, current_company=%s WHERE id=%s",
        [source_tag, current_company, str(app_row["id"])], fetch=False,
    )

    # Vendor-sourced candidates get portal access too (idempotent — safe if one exists)
    _ensure_candidate_portal_invite(cand_id, email.strip().lower(), full_name)

    # Ingest into CV repository (non-blocking on failure)
    try:
        from ..routers.cv_api import ingest_and_link
        ingest_and_link(
            data=file_bytes,
            filename=file.filename or f"vendor_{cand_id}.pdf",
            source="application",
            uploaded_by=None,
            candidate_id=cand_id,
            req_id=req_id,
            tenant_id=req.get("tenant_id"),
        )
    except Exception as exc:
        print(f"[vendor-submit] CV repository ingest failed: {exc}")

    log_activity(
        "vendor", "vendor_candidate_submitted",
        entity_id=vid, requisition_id=req_id, application_id=str(app_row["id"]),
        actor_id=None, actor_role="vendor", actor_label=vendor.get("name"),
    )

    return {
        "application_id": str(app_row["id"]),
        "candidate_id":   cand_id,
        "source":         source_tag,
        "match_score":    app_row.get("match_score"),
    }


# ── Portal: vendor sees status of their own submissions ──────────────────────

@router.get("/portal/submissions")
def portal_submissions(
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    vendor: dict = Depends(get_current_vendor),
):
    vid = vendor["vendor_id"]
    source_tag = f"vendor:{vid}"
    return query(
        """SELECT a.id AS application_id,
                  c.full_name   AS candidate_name,
                  c.email       AS candidate_email,
                  r.id          AS requisition_id,
                  r.title       AS requisition_title,
                  a.status,
                  a.applied_at,
                  a.source
           FROM application a
           JOIN candidate   c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE a.source = %s
           ORDER BY a.applied_at DESC
           LIMIT %s OFFSET %s""",
        [source_tag, limit, offset],
    )


# ── Portal: Reports (open requisitions + hiring funnel, own submissions only) ─
# Reuses the same funnel (_pivot4) / joined-offered-selected (_pivot8) SQL as
# the internal TA/Recruiter/HRBP reports, scoped to this vendor's own
# `application.source = 'vendor:<id>'` rows via the xwhere/xp hook those
# pivots already support. Deliberately does NOT expose the other 6 pivots
# (diversity, internal movement, recruiter productivity, etc.) -- those are
# org-wide/internal-staff metrics a vendor has no visibility into.

def _vendor_open_requisitions(vid: str) -> list:
    """Open reqs count/openings, grouped by band -- this vendor's own scope."""
    return query(
        """SELECT b.code AS band, COUNT(*) AS n, SUM(r.openings) AS openings
           FROM requisition_vendor rv
           JOIN requisition r ON r.id = rv.requisition_id
           JOIN band b        ON b.id = r.band_id
           WHERE rv.vendor_id = %s AND r.status = 'open'
           GROUP BY b.code
           ORDER BY b.code""",
        [vid],
    ) or []


@router.get("/portal/reports/summary")
def portal_reports_summary(
    period: str = Query("yearly"),
    year: int = Query(default_factory=lambda: date.today().year),
    vendor: dict = Depends(get_current_vendor),
):
    vid = vendor["vendor_id"]
    source_tag = f"vendor:{vid}"
    ps = _period_start(period, year)
    xwhere, xp = "AND a.source = %s", [source_tag]
    return {
        "open_requisitions": _vendor_open_requisitions(vid),
        "funnel": _rp._pivot4(year, ps, "", [], xwhere=xwhere, xp=xp),
        "totals": _rp._pivot8(year, ps, "", [], xwhere=xwhere, xp=xp),
    }


@router.get("/portal/reports/excel")
def portal_reports_excel(
    period: str = Query("yearly"),
    year: int = Query(default_factory=lambda: date.today().year),
    vendor: dict = Depends(get_current_vendor),
):
    vid = vendor["vendor_id"]
    source_tag = f"vendor:{vid}"
    ps = _period_start(period, year)
    xwhere, xp = "AND a.source = %s", [source_tag]

    open_reqs = _vendor_open_requisitions(vid)
    funnel = _rp._pivot4(year, ps, "", [], xwhere=xwhere, xp=xp)
    totals = _rp._pivot8(year, ps, "", [], xwhere=xwhere, xp=xp)

    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    excel_export.sheet_from_rows(wb, "Open Requisitions", open_reqs)
    excel_export.sheet_from_rows(wb, "Hiring Funnel", funnel)
    excel_export.sheet_from_rows(wb, "Joined Offered Selected", [totals])
    excel_export.build_summary_sheet(
        wb,
        title=f"Vendor Report — {period.title()} {year}",
        generated_by=vendor.get("name") or vendor.get("email") or "",
        generated_at=datetime.now(),
        rows=funnel,
        measures_meta=[{"key": "n", "label": "Candidates"}],
    )
    return excel_export.stream_workbook(wb, f"enternly_vendor_report_{year}_{period}.xlsx")
