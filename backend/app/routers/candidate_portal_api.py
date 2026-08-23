"""
Candidate Portal API.

All portal routes are gated to candidate JWT (account_type='candidate').
The TA feedback route is gated to ta_manager / admin.

HARD INVARIANT: the portal NEVER exposes any score column.
Queries that touch the application table from portal routes must
return only status + human-readable stage label. No match_score,
ai_fit_score, bot_score, combined_score, score_breakdown,
stability_score, or ai_screen_detail — ever.

Candidate login: POST /api/candidate/portal/login (public — listed in main._PUBLIC)
"""
import os
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote as _urlquote

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from ..auth_utils import (
    SECRET_KEY, ALGORITHM, AUD_CANDIDATE, get_current_user,
    hash_password, verify_password,
)
from ..db import query, query_one
from ..routers.password_api import issue_invite_for_external_user
from ..services.sla import PIPELINE_STAGE_LABELS
from ..services.pipeline import intake_and_screen, _check_no_poach_block, NoPoachBlockedError
from ..services.screening import KEYWORD_ALIASES
from ..services.resume_parser import extract_contact_info, extract_text as extract_resume_text
from ..services.candidate_profile_parser import parse_resume_to_profile, apply_parsed_profile
from ..services.activity_log import log_activity
from ..services import connectors
from ..services import linkedin_oauth

router = APIRouter(prefix="/api/candidate", tags=["candidate-portal"])

_TA_ROLES = {"ta_manager", "admin"}
_BEARER   = HTTPBearer(auto_error=False)
_TTL_HOURS = 8

# Human-readable stage labels (mirrors sla.PIPELINE_STAGE_LABELS but safe to extend)
_STAGE_LABELS = {
    **PIPELINE_STAGE_LABELS,
    "applied":       "Applied",
    "nexai_bot":     "NexAI Interview",
    "hired":         "Offer Accepted",
    "offered":       "Offer Received",
    "rejected":      "Not Progressing",
    "on_hold":       "On Hold",
    "documentation": "Documentation Review",
}


# ── Candidate JWT helpers ─────────────────────────────────────────────────────

def _create_candidate_token(cu: dict) -> str:
    expire = datetime.utcnow() + timedelta(hours=_TTL_HOURS)
    return jwt.encode(
        {
            "sub":          str(cu["id"]),
            "email":        cu["email"],
            "candidate_id": str(cu["candidate_id"]),
            "account_type": "candidate",
            "aud":          AUD_CANDIDATE,
            "exp":          expire,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def get_current_candidate(
    creds: HTTPAuthorizationCredentials | None = Depends(_BEARER),
) -> dict:
    """Dependency: resolve a candidate JWT. Mirrors get_current_vendor."""
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM], audience=AUD_CANDIDATE)
    except JWTError:
        # grace: legacy candidate token has no aud but account_type='candidate'
        try:
            legacy = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_aud": False})
        except JWTError:
            raise HTTPException(401, "Invalid or expired token")
        if legacy.get("aud"):          # has a non-candidate aud → reject
            raise HTTPException(403, "Candidate access only")
        if legacy.get("account_type") != "candidate":
            raise HTTPException(403, "Candidate access only")
        payload = legacy
    if payload.get("account_type") != "candidate":
        raise HTTPException(403, "Candidate access only")
    return payload


def _require_ta(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in _TA_ROLES:
        raise HTTPException(403, "ta_manager / admin only")
    return user


# ── Public: candidate login ───────────────────────────────────────────────────

class CandidateLoginIn(BaseModel):
    email: str
    password: str


@router.post("/portal/login")
def candidate_login(body: CandidateLoginIn):
    """Public. Candidate logs in; receives a short-lived JWT."""
    cu = query_one(
        """SELECT cu.id, cu.candidate_id, cu.email, cu.password_hash, cu.is_active,
                  c.full_name
           FROM candidate_user cu
           JOIN candidate c ON c.id = cu.candidate_id
           WHERE LOWER(cu.email) = %s AND cu.is_active = TRUE""",
        [body.email.lower().strip()],
    )
    if not cu or not verify_password(body.password, cu.get("password_hash") or ""):
        raise HTTPException(401, "Invalid credentials")
    return {
        "token": _create_candidate_token(cu),
        "name":  cu["full_name"],
        "email": cu["email"],
    }


# ── Portal: candidate's own applications — NO SCORES EVER ────────────────────

@router.get("/portal/applications")
def portal_applications(candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    # INVARIANT: never return any score column from this endpoint.
    rows = query(
        """SELECT a.id, a.status, a.applied_at, a.source,
                  r.title AS job_title, r.hiring_location,
                  b.code  AS band
           FROM application a
           JOIN requisition r ON r.id = a.requisition_id
           JOIN band b        ON b.id = r.band_id
           WHERE a.candidate_id = %s
           ORDER BY a.applied_at DESC
           LIMIT 500""",
        [cand_id],
    )
    return [
        {
            "application_id": str(r["id"]),
            "job_title":      r["job_title"],
            "hiring_location": r["hiring_location"],
            "band":           r["band"],
            "status":         r["status"],
            "stage_label":    _STAGE_LABELS.get(r["status"], r["status"].replace("_", " ").title()),
            "applied_at":     r["applied_at"],
        }
        for r in rows
    ]


# ── Portal: application pipeline stepper (Submitted/Screening/Interview/
# Documentation/Offered, + Selected on hire, + a banner for on_hold/rejected) ─

_CANDIDATE_STAGE_DEFS = [
    {"key": "submitted", "label": "Submitted", "statuses": ["applied"],
     "description": "Your application has been received."},
    {"key": "screening", "label": "Screening", "statuses": ["screen", "nexai_bot", "shortlisted"],
     "description": "Our team is reviewing your profile and screening responses."},
    {"key": "interview", "label": "Interview", "statuses": ["interview"],
     "description": "You'll be scheduled for an interview with the hiring team."},
    {"key": "documentation", "label": "Documentation", "statuses": ["documentation"],
     "description": "Final documentation and verification before an offer."},
    {"key": "offered", "label": "Offered", "statuses": ["offered", "hired"],
     "description": "An offer has been extended to you."},
]
_PIPELINE_STATUS_ORDER = ["applied", "screen", "nexai_bot", "shortlisted", "interview", "documentation", "offered", "hired"]


@router.get("/portal/applications/{application_id}/pipeline")
def portal_application_pipeline(application_id: str, candidate: dict = Depends(get_current_candidate)):
    """
    Candidate-facing stepper for one of their own applications. Collapses
    internal stages (screen/nexai_bot/shortlisted) into a single "Screening"
    step -- candidates were never meant to see that internal granularity,
    same invariant as everywhere else in this router: no score column, and
    here also no internal-only stage names.

    on_hold/rejected don't get forced into the linear stepper (that would
    misleadingly imply they're still progressing) -- instead the steps freeze
    at their last real stage (reconstructed from stage_event history) and a
    banner explains the pause/outcome.
    """
    cand_id = candidate["candidate_id"]
    app_row = query_one(
        """SELECT a.id, a.status, a.applied_at, r.title AS job_title, r.hiring_location
           FROM application a JOIN requisition r ON r.id = a.requisition_id
           WHERE a.id=%s AND a.candidate_id=%s""",
        [application_id, cand_id],
    )
    if not app_row:
        raise HTTPException(404, "Application not found")

    events = query(
        "SELECT to_status, occurred_at FROM stage_event WHERE application_id=%s ORDER BY occurred_at ASC",
        [application_id],
    ) or []

    status = app_row["status"]
    effective_status = status
    if status in ("on_hold", "rejected"):
        effective_status = "applied"
        for ev in reversed(events):
            if ev["to_status"] in _PIPELINE_STATUS_ORDER:
                effective_status = ev["to_status"]
                break

    current_index = 0
    for i, stage in enumerate(_CANDIDATE_STAGE_DEFS):
        if effective_status in stage["statuses"]:
            current_index = i
            break

    def _reached_at(stage):
        matches = [ev["occurred_at"] for ev in events if ev["to_status"] in stage["statuses"]]
        if matches:
            return min(matches)
        return app_row["applied_at"] if stage["key"] == "submitted" else None

    steps = []
    for i, stage in enumerate(_CANDIDATE_STAGE_DEFS):
        if status == "hired":
            state = "done"
        elif status in ("on_hold", "rejected"):
            state = "done" if i <= current_index else "pending"
        else:
            state = "done" if i < current_index else ("current" if i == current_index else "pending")
        steps.append({
            "key": stage["key"], "label": stage["label"], "description": stage["description"],
            "state": state, "reached_at": _reached_at(stage),
        })

    if status == "hired":
        steps.append({
            "key": "selected", "label": "Selected",
            "description": "Congratulations — you've been selected for this role.",
            "state": "done", "reached_at": None,
        })

    banner = None
    if status == "on_hold":
        banner = {"type": "on_hold", "text": "Your application is currently on hold. We'll notify you as soon as there's an update."}
    elif status == "rejected":
        banner = {"type": "rejected", "text": "We've decided not to move forward with your application for this role at this time. We appreciate the time you took to apply."}

    return {
        "job_title": app_row["job_title"],
        "hiring_location": app_row["hiring_location"],
        "status": status,
        "steps": steps,
        "banner": banner,
    }


# ── Portal: recommended roles by skill overlap — no numeric score ─────────────

def _candidate_skills(candidate_id: str) -> list[str]:
    """Return the candidate's skills array from their latest CV in the repository."""
    row = query_one(
        """SELECT cr.skills
           FROM cv_repository cr
           JOIN candidate c ON c.cv_repository_id = cr.id
           WHERE c.id = %s""",
        [candidate_id],
    )
    if row and row.get("skills"):
        return [s.lower() for s in row["skills"] if s]
    return []


def _skill_match_reason(req_skills_text: str, candidate_skills: list[str]) -> tuple[int, str]:
    """
    Compute overlap between req key_skills and candidate's skill list.
    Returns (match_count, human-readable reason string).
    No numeric score is returned to the candidate — only the reason text.
    """
    if not req_skills_text or not candidate_skills:
        return 0, ""
    req_lower = req_skills_text.lower()
    matched = []
    for cand_skill in candidate_skills:
        # Check canonical key and all aliases
        for canonical, aliases in KEYWORD_ALIASES.items():
            if cand_skill in aliases or cand_skill == canonical:
                if any(alias in req_lower for alias in aliases) or canonical in req_lower:
                    if canonical not in matched:
                        matched.append(canonical)
                    break
        else:
            # Direct substring match
            if cand_skill in req_lower and cand_skill not in matched:
                matched.append(cand_skill)
    count = len(matched)
    if matched:
        readable = ", ".join(s.title() for s in matched[:4])
        if len(matched) > 4:
            readable += f" and {len(matched)-4} more"
        reason = f"Matches your skills: {readable}"
    else:
        reason = "Explore this opportunity"
    return count, reason


@router.get("/portal/recommended")
def portal_recommended(candidate: dict = Depends(get_current_candidate)):
    """
    Open reqs ranked by skill overlap with the candidate's profile.
    Returns role + match-reason text — NO numeric score exposed.
    """
    cand_id = candidate["candidate_id"]
    cand_skills = _candidate_skills(cand_id)

    # Already applied reqs — exclude from recommendations
    applied_ids = {
        str(r["requisition_id"])
        for r in query(
            "SELECT requisition_id FROM application WHERE candidate_id=%s", [cand_id]
        )
    }

    open_reqs = query(
        """SELECT r.id, r.title, r.hiring_location, r.min_experience, r.max_experience,
                  r.key_skills, b.code AS band, bu.name AS business_unit
           FROM requisition r
           JOIN band b ON b.id = r.band_id
           JOIN business_unit bu ON bu.id = r.bu_id
           WHERE r.status = 'open'
             AND COALESCE(r.approval_status, 'approved') = 'approved'
           ORDER BY r.created_at DESC""",
    )

    results = []
    for req in open_reqs:
        if str(req["id"]) in applied_ids:
            continue
        match_count, reason = _skill_match_reason(req.get("key_skills") or "", cand_skills)
        results.append({
            "requisition_id":  str(req["id"]),
            "title":           req["title"],
            "hiring_location": req["hiring_location"],
            "band":            req["band"],
            "business_unit":   req["business_unit"],
            "min_experience":  req["min_experience"],
            "max_experience":  req["max_experience"],
            "match_reason":    reason,
            "_sort_key":       match_count,
        })

    results.sort(key=lambda x: x.pop("_sort_key"), reverse=True)
    return results


# ── Portal: one-click apply ───────────────────────────────────────────────────

def _send_neutral_rejection_email(candidate_email: str, candidate_name: str, job_title: str, company: str) -> bool:
    """
    Auto-rejection for a no-poach match. Deliberately generic -- this fires
    with no human review, so it must never disclose the real reason (a
    confidential no-poach/employer relationship). Returns whether it sent.
    """
    subject = f"Update on your application — {job_title}"
    body = (
        f"Hi {candidate_name or 'there'},\n\n"
        f"Thank you for your interest in the {job_title} role at {company}. "
        f"After reviewing your application, we won't be moving forward at this time.\n\n"
        f"We appreciate the time you took to apply and wish you the best in your search.\n\n"
        f"— {company} Talent Acquisition"
    )
    import html as _html
    from ..services.email_layout import build_branded_email
    name = candidate_name or "there"
    html_body = build_branded_email(
        eyebrow="Application Tracking System",
        hero_title_html="Application<br>Update.",
        hero_subtitle=f"Hi {_html.escape(name)}, thank you for your interest in the {_html.escape(job_title)} role at {_html.escape(company or '')}.",
        hero_footer_label=job_title, hero_footer_value=company,
        detail_cells=[("Candidate", candidate_name or "Candidate"), ("Position", job_title)],
        about_text=(
            "After reviewing your application, we won't be moving forward at this time.\n\n"
            "We appreciate the time you took to apply and wish you the best in your search."
        ),
        about_heading=None,
        cta_label=None, cta_link=None,
    )
    try:
        connectors.send_email(candidate_email, subject, body, html=html_body)
        return True
    except Exception as exc:
        print(f"[candidate-portal] no-poach auto-reject email failed for {candidate_email}: {exc}")
        return False


@router.post("/portal/apply/{req_id}")
def portal_apply(req_id: str, candidate: dict = Depends(get_current_candidate)):
    """
    Idempotent one-click apply.  No duplicate application allowed for the
    same candidate + req (application table has UNIQUE on those two columns).
    """
    cand_id = candidate["candidate_id"]

    req = query_one(
        """SELECT r.id, r.approval_status, r.title, gc.name AS company
           FROM requisition r
           JOIN business_unit bu ON bu.id = r.bu_id
           JOIN group_company gc ON gc.id = bu.company_id
           WHERE r.id=%s""",
        [req_id],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")
    if (req.get("approval_status") or "approved") != "approved":
        raise HTTPException(403, "This requisition is not open for applications yet")

    # Idempotency: if already applied, return existing application
    existing = query_one(
        "SELECT id, status FROM application WHERE requisition_id=%s AND candidate_id=%s",
        [req_id, cand_id],
    )
    if existing:
        return {
            "application_id": str(existing["id"]),
            "status":         existing["status"],
            "already_applied": True,
        }

    # Get candidate's latest resume text for screening
    cv_row = query_one(
        """SELECT cr.raw_text, cr.experience_years
           FROM cv_repository cr
           JOIN candidate c ON c.cv_repository_id = cr.id
           WHERE c.id = %s""",
        [cand_id],
    )
    resume_text = (cv_row or {}).get("raw_text") or ""
    years_exp   = (cv_row or {}).get("experience_years")

    # This entry point never collected current_company at all -- the portal's
    # self-service applicants were the one intake path that bypassed the
    # no-poach control entirely. There's no form field for it here, so parse
    # it the same way the resume-preview endpoint already does (main.py's
    # /api/parse-resume), off the same resume_text already fetched above.
    parsed_contact = extract_contact_info(resume_text) if resume_text else {}
    current_company = parsed_contact.get("current_company")

    no_poach_match = None
    try:
        _check_no_poach_block(current_company, req_id, entity_id=cand_id)
    except NoPoachBlockedError as exc:
        no_poach_match = exc.company_name

    if no_poach_match:
        # Per design: never silently drop the candidate -- store the
        # application (CV/audit trail intact), auto-reject it, and log the
        # real reason internally. The candidate-facing email must stay
        # neutral (no mention of no-poach/employer) since it fires with no
        # human review and the real reason discloses a confidential
        # relationship.
        cand_row = query_one("SELECT full_name, email FROM candidate WHERE id=%s", [cand_id])
        app_row = query_one(
            """INSERT INTO application
                 (requisition_id, candidate_id, status, source, current_company)
               VALUES (%s, %s, 'rejected', 'career_site', %s)
               RETURNING *""",
            [req_id, cand_id, current_company],
        )
        app_id = str(app_row["id"])
        query(
            """INSERT INTO stage_event (application_id, from_status, to_status, note)
               VALUES (%s, 'applied', 'rejected', %s)""",
            [app_id, "Auto-rejected at intake (see activity_log for internal reason)"],
            fetch=False,
        )
        log_activity(
            "application", "no_poach_auto_rejected",
            entity_id=app_id, application_id=app_id, requisition_id=req_id,
            actor_id=None, actor_role="system",
            detail={"current_company": current_company, "matched_company": no_poach_match},
        )
        if cand_row and cand_row.get("email"):
            _send_neutral_rejection_email(
                cand_row["email"], cand_row.get("full_name"), req["title"], req.get("company"),
            )
        return {
            "application_id": app_id,
            "status":         "rejected",
            "already_applied": False,
        }

    app_row = intake_and_screen(req_id, cand_id, resume_text, years_exp, current_company=current_company)

    # Tag source + persist current_company (matches the other 3 intake paths --
    # also needed for no_poach_api.py's live-match report, which reads
    # application.current_company directly).
    query(
        "UPDATE application SET source='career_site', current_company=%s WHERE id=%s",
        [current_company, str(app_row["id"])], fetch=False,
    )
    return {
        "application_id": str(app_row["id"]),
        "status":         app_row.get("status"),
        "already_applied": False,
    }


# ── Portal: submit feedback ───────────────────────────────────────────────────

class FeedbackIn(BaseModel):
    company_rating:   int
    interview_rating: int
    comments:         Optional[str] = None
    application_id:   Optional[str] = None


@router.post("/portal/feedback")
def portal_submit_feedback(body: FeedbackIn, candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    if not (1 <= body.company_rating <= 5):
        raise HTTPException(400, "company_rating must be 1–5")
    if not (1 <= body.interview_rating <= 5):
        raise HTTPException(400, "interview_rating must be 1–5")
    if body.application_id:
        app = query_one(
            "SELECT id FROM application WHERE id=%s AND candidate_id=%s",
            [body.application_id, cand_id],
        )
        if not app:
            raise HTTPException(403, "Application not found or does not belong to you")
    row = query_one(
        """INSERT INTO candidate_feedback
               (candidate_id, application_id, company_rating, interview_rating, comments)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        [cand_id, body.application_id, body.company_rating, body.interview_rating, body.comments],
    )
    return {"ok": True, "feedback_id": str(row["id"])}


# ── Portal: candidate sees their own feedback ─────────────────────────────────

@router.get("/portal/feedback")
def portal_my_feedback(candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    return query(
        """SELECT cf.id, cf.company_rating, cf.interview_rating, cf.comments,
                  cf.submitted_at, r.title AS job_title
           FROM candidate_feedback cf
           LEFT JOIN application a  ON a.id  = cf.application_id
           LEFT JOIN requisition r  ON r.id  = a.requisition_id
           WHERE cf.candidate_id = %s
           ORDER BY cf.submitted_at DESC""",
        [cand_id],
    )


# ── TA route: all candidate feedback (experience dashboard) ──────────────────

@router.get("/feedback")  # mounted at /api/candidate/feedback
def ta_all_feedback(
    company_rating: Optional[int]  = None,
    req_id:         Optional[str]  = None,
    date_from:      Optional[str]  = None,
    date_to:        Optional[str]  = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(_require_ta),
):
    """TA-only: all candidate experience feedback, filterable."""
    conditions = ["1=1"]
    params: list = []
    if company_rating:
        conditions.append("cf.company_rating = %s"); params.append(company_rating)
    if req_id:
        conditions.append("a.requisition_id = %s"); params.append(req_id)
    if date_from:
        conditions.append("cf.submitted_at >= %s"); params.append(date_from)
    if date_to:
        conditions.append("cf.submitted_at <= %s"); params.append(date_to)

    where = " AND ".join(conditions)
    return query(
        f"""SELECT cf.id, c.full_name AS candidate_name, c.email AS candidate_email,
                   cf.company_rating, cf.interview_rating, cf.comments, cf.submitted_at,
                   r.title AS job_title
            FROM candidate_feedback cf
            JOIN candidate c ON c.id = cf.candidate_id
            LEFT JOIN application a ON a.id = cf.application_id
            LEFT JOIN requisition r ON r.id = a.requisition_id
            WHERE {where}
            ORDER BY cf.submitted_at DESC
            LIMIT %s OFFSET %s""",
        params + [limit, offset],
    )


# ── Portal: My Profile — basic info, skills, work experience, education ──────

_MAX_EXPERIENCE_ENTRIES = 5
_MAX_EDUCATION_ENTRIES  = 8


@router.get("/portal/profile")
def portal_get_profile(candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    cand = query_one(
        """SELECT full_name, given_name, email, phone, resume_url, skills,
                  linkedin_url, linkedin_connected_at, linkedin_last_synced_at,
                  linkedin_profile_name, linkedin_profile_photo_url,
                  linkedin_reminders_opt_out
           FROM candidate WHERE id=%s""",
        [cand_id],
    )
    if not cand:
        raise HTTPException(404, "Candidate not found")
    experience = query(
        """SELECT id, company, title, start_month, start_year, end_month, end_year,
                  is_current, description, source
           FROM candidate_work_experience WHERE candidate_id=%s
           ORDER BY sort_order ASC, start_year DESC NULLS LAST""",
        [cand_id],
    )
    education = query(
        """SELECT id, institution, degree, field_of_study, start_year, end_year, source
           FROM candidate_education WHERE candidate_id=%s
           ORDER BY sort_order ASC, start_year DESC NULLS LAST""",
        [cand_id],
    )
    return {
        "full_name":  cand["full_name"],
        "given_name": cand["given_name"],
        "email":      cand["email"],
        "phone":      cand["phone"],
        "resume_url": cand["resume_url"],
        "skills":     cand["skills"] or [],
        "linkedin": {
            "url":               cand["linkedin_url"],
            "connected_at":      cand["linkedin_connected_at"],
            "last_synced_at":    cand["linkedin_last_synced_at"],
            "profile_name":      cand["linkedin_profile_name"],
            "profile_photo_url": cand["linkedin_profile_photo_url"],
            "reminders_opt_out": cand["linkedin_reminders_opt_out"],
        },
        "experience": [{**e, "id": str(e["id"])} for e in (experience or [])],
        "education":  [{**e, "id": str(e["id"])} for e in (education or [])],
    }


class ProfileUpdateIn(BaseModel):
    full_name:  Optional[str] = None
    given_name: Optional[str] = None
    phone:      Optional[str] = None


@router.put("/portal/profile")
def portal_update_profile(body: ProfileUpdateIn, candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    if body.full_name is not None and not body.full_name.strip():
        raise HTTPException(400, "Full name cannot be blank")
    query(
        """UPDATE candidate SET
             full_name  = COALESCE(NULLIF(%s, ''), full_name),
             given_name = %s,
             phone      = %s
           WHERE id=%s""",
        [
            (body.full_name or "").strip(),
            (body.given_name or "").strip() or None,
            (body.phone or "").strip() or None,
            cand_id,
        ],
        fetch=False,
    )
    return {"ok": True}


class SkillsIn(BaseModel):
    skills: list[str]


@router.put("/portal/profile/skills")
def portal_update_skills(body: SkillsIn, candidate: dict = Depends(get_current_candidate)):
    cleaned = sorted({s.strip().lower() for s in body.skills if s and s.strip()})[:50]
    query(
        "UPDATE candidate SET skills=%s WHERE id=%s",
        [cleaned, candidate["candidate_id"]], fetch=False,
    )
    return {"ok": True, "skills": cleaned}


class ExperienceIn(BaseModel):
    company:     str
    title:       str
    start_month: Optional[int] = None
    start_year:  Optional[int] = None
    end_month:   Optional[int] = None
    end_year:    Optional[int] = None
    is_current:  bool = False
    description: Optional[str] = None


@router.post("/portal/profile/experience")
def portal_add_experience(body: ExperienceIn, candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    if not body.company.strip() or not body.title.strip():
        raise HTTPException(400, "Company and title are required")
    count = query_one(
        "SELECT COUNT(*) AS n FROM candidate_work_experience WHERE candidate_id=%s", [cand_id]
    )
    if (count["n"] if count else 0) >= _MAX_EXPERIENCE_ENTRIES:
        raise HTTPException(400, f"You can add up to {_MAX_EXPERIENCE_ENTRIES} work experience entries")
    row = query_one(
        """INSERT INTO candidate_work_experience
             (candidate_id, company, title, start_month, start_year,
              end_month, end_year, is_current, description, source, sort_order)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'manual',
                   (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM candidate_work_experience WHERE candidate_id=%s))
           RETURNING id""",
        [cand_id, body.company.strip(), body.title.strip(), body.start_month, body.start_year,
         body.end_month, body.end_year, body.is_current, body.description, cand_id],
    )
    return {"ok": True, "id": str(row["id"])}


@router.put("/portal/profile/experience/{exp_id}")
def portal_update_experience(exp_id: str, body: ExperienceIn, candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    if not body.company.strip() or not body.title.strip():
        raise HTTPException(400, "Company and title are required")
    row = query_one(
        """UPDATE candidate_work_experience SET
             company=%s, title=%s, start_month=%s, start_year=%s,
             end_month=%s, end_year=%s, is_current=%s, description=%s,
             source='manual', updated_at=now()
           WHERE id=%s AND candidate_id=%s RETURNING id""",
        [body.company.strip(), body.title.strip(), body.start_month, body.start_year,
         body.end_month, body.end_year, body.is_current, body.description, exp_id, cand_id],
    )
    if not row:
        raise HTTPException(404, "Experience entry not found")
    return {"ok": True}


@router.delete("/portal/profile/experience/{exp_id}")
def portal_delete_experience(exp_id: str, candidate: dict = Depends(get_current_candidate)):
    row = query_one(
        "DELETE FROM candidate_work_experience WHERE id=%s AND candidate_id=%s RETURNING id",
        [exp_id, candidate["candidate_id"]],
    )
    if not row:
        raise HTTPException(404, "Experience entry not found")
    return {"ok": True}


class EducationIn(BaseModel):
    institution:    str
    degree:         Optional[str] = None
    field_of_study: Optional[str] = None
    start_year:     Optional[int] = None
    end_year:       Optional[int] = None


@router.post("/portal/profile/education")
def portal_add_education(body: EducationIn, candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    if not body.institution.strip():
        raise HTTPException(400, "Institution is required")
    count = query_one(
        "SELECT COUNT(*) AS n FROM candidate_education WHERE candidate_id=%s", [cand_id]
    )
    if (count["n"] if count else 0) >= _MAX_EDUCATION_ENTRIES:
        raise HTTPException(400, f"You can add up to {_MAX_EDUCATION_ENTRIES} education entries")
    row = query_one(
        """INSERT INTO candidate_education
             (candidate_id, institution, degree, field_of_study, start_year, end_year, source, sort_order)
           VALUES (%s,%s,%s,%s,%s,%s,'manual',
                   (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM candidate_education WHERE candidate_id=%s))
           RETURNING id""",
        [cand_id, body.institution.strip(), body.degree, body.field_of_study,
         body.start_year, body.end_year, cand_id],
    )
    return {"ok": True, "id": str(row["id"])}


@router.put("/portal/profile/education/{edu_id}")
def portal_update_education(edu_id: str, body: EducationIn, candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    if not body.institution.strip():
        raise HTTPException(400, "Institution is required")
    row = query_one(
        """UPDATE candidate_education SET
             institution=%s, degree=%s, field_of_study=%s, start_year=%s, end_year=%s,
             source='manual', updated_at=now()
           WHERE id=%s AND candidate_id=%s RETURNING id""",
        [body.institution.strip(), body.degree, body.field_of_study,
         body.start_year, body.end_year, edu_id, cand_id],
    )
    if not row:
        raise HTTPException(404, "Education entry not found")
    return {"ok": True}


@router.delete("/portal/profile/education/{edu_id}")
def portal_delete_education(edu_id: str, candidate: dict = Depends(get_current_candidate)):
    row = query_one(
        "DELETE FROM candidate_education WHERE id=%s AND candidate_id=%s RETURNING id",
        [edu_id, candidate["candidate_id"]],
    )
    if not row:
        raise HTTPException(404, "Education entry not found")
    return {"ok": True}


_RESUME_SUPPORTED_EXT = {".pdf", ".docx", ".doc"}
_RESUME_MAX_BYTES     = 5 * 1024 * 1024  # 5MB


@router.post("/portal/profile/resume")
async def portal_update_resume(file: UploadFile = File(...), candidate: dict = Depends(get_current_candidate)):
    """
    Candidate re-uploads their resume from the portal. Re-parses it and
    prefills any profile fields/collections that are still empty (see
    candidate_profile_parser.apply_parsed_profile) — never overwrites
    existing work experience/education/skills the candidate already has.
    """
    import pathlib
    cand_id = candidate["candidate_id"]
    ext = pathlib.Path(file.filename or "").suffix.lower()
    if ext not in _RESUME_SUPPORTED_EXT:
        raise HTTPException(400, "Only PDF, DOC, or DOCX resumes are supported")
    data = await file.read()
    if len(data) > _RESUME_MAX_BYTES:
        raise HTTPException(400, "Resume file is too large (5MB max)")

    raw_text, warning = extract_resume_text(data, file.filename or f"resume{ext}")
    if not raw_text:
        raise HTTPException(400, warning or "Could not read this resume file")

    cv_store = os.environ.get("CV_STORE_DIR", "/app/cv_store")
    os.makedirs(cv_store, exist_ok=True)
    cv_id = str(uuid.uuid4())
    dest = os.path.join(cv_store, f"{cv_id}{ext}")
    with open(dest, "wb") as f:
        f.write(data)

    cv_row = query_one(
        """INSERT INTO cv_repository
             (id, file_name, file_path, file_ext, candidate_name, candidate_id,
              map_status, raw_text, text_vector, source)
           VALUES (%s,%s,%s,%s,
                   (SELECT full_name FROM candidate WHERE id=%s), %s,
                   'mapped', %s, to_tsvector('english', %s), 'candidate_portal')
           RETURNING id""",
        [cv_id, file.filename, dest, ext.lstrip("."), cand_id, cand_id, raw_text, raw_text],
    )
    query(
        "UPDATE candidate SET resume_url=%s, cv_repository_id=%s WHERE id=%s",
        [dest, str(cv_row["id"]), cand_id], fetch=False,
    )

    parsed = parse_resume_to_profile(raw_text)
    if parsed:
        apply_parsed_profile(cand_id, parsed)

    return {"ok": True, "warning": warning or None, "prefilled": bool(parsed)}


# ── Portal: LinkedIn connect (OAuth, gated) + manual URL + unsubscribe ───────

@router.get("/portal/linkedin/status")
def portal_linkedin_status(candidate: dict = Depends(get_current_candidate)):
    """Frontend checks this before rendering the 'Update LinkedIn' button --
    when configured is false, it shows a 'Coming Soon' state instead of a
    connect flow that would fail."""
    return {"configured": linkedin_oauth.is_configured()}


@router.get("/portal/linkedin/connect")
def portal_linkedin_connect(candidate: dict = Depends(get_current_candidate)):
    if not linkedin_oauth.is_configured():
        raise HTTPException(
            400,
            "LinkedIn connection isn't available yet -- coming soon. "
            "You can still add your profile link manually below.",
        )
    cand_id = candidate["candidate_id"]
    query("DELETE FROM linkedin_oauth_state WHERE created_at < now() - interval '10 minutes'", fetch=False)
    state = secrets.token_urlsafe(32)
    query(
        "INSERT INTO linkedin_oauth_state (state, candidate_id) VALUES (%s, %s)",
        [state, cand_id], fetch=False,
    )
    auth_url = linkedin_oauth.build_auth_url(state)
    return {"auth_url": auth_url}


def _portal_base_url() -> str:
    from ..services.connectors import _load_email_cfg
    return (_load_email_cfg().get("base_url") or os.environ.get("APP_BASE_URL", "")).rstrip("/") or "http://localhost:8000"


@router.get("/linkedin/callback")  # mounted at /api/candidate/linkedin/callback — PUBLIC, see main._PUBLIC
def portal_linkedin_callback(code: str = None, state: str = None, error: str = None):
    """LinkedIn redirects the candidate's browser here directly with just a
    `?code=&state=` query string, no Authorization header -- authenticates
    the *request* via the one-time `state` nonce (created only by an
    already-authenticated /portal/linkedin/connect call, consumed exactly
    once here). Never crashes to a raw error page -- always redirects back
    to the portal's My Profile tab, carrying either success or the real
    failure reason as a query flag."""
    base_url = _portal_base_url()
    return_url = f"{base_url}/candidate-portal?next=profile&section=linkedin"
    try:
        if error:
            return RedirectResponse(f"{return_url}&linkedinError={_urlquote(error, safe='')}")
        if not code or not state:
            return RedirectResponse(f"{return_url}&linkedinError=missing_code_or_state")

        row = query_one("SELECT * FROM linkedin_oauth_state WHERE state=%s", [state])
        query("DELETE FROM linkedin_oauth_state WHERE state=%s", [state], fetch=False)
        if not row:
            return RedirectResponse(f"{return_url}&linkedinError=invalid_or_expired_state")

        info = linkedin_oauth.exchange_code(code)
        cand_id = str(row["candidate_id"])
        query(
            """UPDATE candidate SET
                 linkedin_oauth_sub          = %s,
                 linkedin_profile_name       = %s,
                 linkedin_profile_photo_url  = %s,
                 linkedin_connected_at       = COALESCE(linkedin_connected_at, now()),
                 linkedin_last_synced_at     = now(),
                 linkedin_reminder_sent_at   = now(),
                 linkedin_reminder_next_attempt_at = NULL
               WHERE id=%s""",
            [info.get("sub"), info.get("name"), info.get("picture"), cand_id],
            fetch=False,
        )
        return RedirectResponse(f"{return_url}&linkedinConnected=1")
    except Exception as exc:
        print(f"[linkedin-oauth] connect failed: {exc}")
        detail = str(exc)[:300]
        return RedirectResponse(f"{return_url}&linkedinError={_urlquote(detail, safe='')}")


class LinkedInManualIn(BaseModel):
    linkedin_url: str


@router.post("/portal/linkedin/manual")
def portal_linkedin_manual(body: LinkedInManualIn, candidate: dict = Depends(get_current_candidate)):
    url = body.linkedin_url.strip()
    if url and "linkedin.com" not in url.lower():
        raise HTTPException(400, "That doesn't look like a LinkedIn URL")
    cand_id = candidate["candidate_id"]
    # A manual save is treated as "just refreshed" -- resets the 6-month
    # reminder clock, same as an OAuth reconnect would.
    query(
        """UPDATE candidate SET
             linkedin_url = %s,
             linkedin_source = 'manual',
             linkedin_reminder_sent_at = now(),
             linkedin_reminder_next_attempt_at = NULL
           WHERE id=%s""",
        [url or None, cand_id], fetch=False,
    )
    return {"ok": True}


@router.get("/linkedin/unsubscribe", response_class=HTMLResponse)  # mounted at /api/candidate/linkedin/unsubscribe — PUBLIC
def portal_linkedin_unsubscribe(token: str):
    """One-click unsubscribe from the email footer -- deliberately no login
    required, matching how unsubscribe links work everywhere else. Renders
    a plain confirmation page since a human clicks this from their inbox,
    not a frontend calling an API."""
    row = query_one("SELECT id FROM candidate WHERE linkedin_unsub_token=%s", [token])
    if not row:
        message = "This unsubscribe link is invalid or has already been used."
    else:
        query(
            "UPDATE candidate SET linkedin_reminders_opt_out=TRUE WHERE id=%s",
            [str(row["id"])], fetch=False,
        )
        message = "You've been unsubscribed from LinkedIn profile refresh reminders."
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
    <title>Enternly</title>
    <style>body{{font-family:Arial,Helvetica,sans-serif;background:#eef2ff;display:flex;
    align-items:center;justify-content:center;min-height:100vh;margin:0}}
    .card{{background:#fff;border-radius:12px;padding:36px 44px;max-width:420px;text-align:center;
    box-shadow:0 8px 24px -12px rgba(20,25,40,.2)}}
    h1{{font-size:18px;color:#020F50;margin:0 0 10px}}
    p{{font-size:14px;color:#4b5563;margin:0}}</style></head>
    <body><div class="card"><h1>Enternly</h1><p>{message}</p></div></body></html>"""
