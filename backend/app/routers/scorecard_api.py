"""
Panel interview scorecard / feedback API.

Endpoints
---------
GET  /api/interviews/{interview_id}/scorecard-form   form schema + caller's draft/submitted scorecard
POST /api/interviews/{interview_id}/scorecard        save draft or submit (panelists only)
GET  /api/interviews/{interview_id}/scorecard/pdf     PDF export of the caller's own scorecard
GET  /api/interviews/{interview_id}/panel-feedback   aggregated panel results (role-gated, bias-guarded)
GET  /api/applications/{app_id}/panel-feedback       same, across all rounds for an application
"""
import json
from typing import Optional
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services.activity_log import log_activity
from ..services.pdf_export import render_scorecard_pdf, stream_pdf

router = APIRouter(prefix="/api", tags=["scorecard"])


def _feedback_is_on_time(scheduled_at) -> bool:
    """
    True if panel feedback is submitted within the configured window
    (gamification_config 'sla.feedback_hours', default 48h) after the interview's
    scheduled time. If no scheduled time is on record, fall back to True so we
    never penalise on missing data.
    """
    if not scheduled_at:
        return True
    try:
        row = query_one(
            "SELECT value FROM gamification_config WHERE key='sla.feedback_hours'"
        )
        hours = float(row["value"]) if row and row.get("value") else 48.0
    except Exception:
        hours = 48.0
    try:
        sched = scheduled_at
        if isinstance(sched, str):
            sched = datetime.fromisoformat(sched)
        if sched.tzinfo is None:
            sched = sched.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) <= sched + timedelta(hours=hours)
    except Exception:
        return True


# ── Default feedback form ─────────────────────────────────────────────────────

_DEFAULT_FORM_NAME = "Default Panel Scorecard"

_DEFAULT_FORM_SCHEMA = [
    {"key": "tech_skills",          "label": "Technical / Role Skills",  "type": "rating_5",     "required": True},
    {"key": "tech_skills_note",     "label": "Notes",                    "type": "text",         "required": False, "parent": "tech_skills"},
    {"key": "communication",        "label": "Communication",            "type": "rating_5",     "required": True},
    {"key": "communication_note",   "label": "Notes",                    "type": "text",         "required": False, "parent": "communication"},
    {"key": "problem_solving",      "label": "Problem-Solving",          "type": "rating_5",     "required": True},
    {"key": "problem_solving_note", "label": "Notes",                    "type": "text",         "required": False, "parent": "problem_solving"},
    {"key": "domain_fit",           "label": "Domain / Experience Fit",  "type": "rating_5",     "required": True},
    {"key": "domain_fit_note",      "label": "Notes",                    "type": "text",         "required": False, "parent": "domain_fit"},
    {"key": "culture_fit",          "label": "Culture / Values Fit",     "type": "rating_5",     "required": True},
    {"key": "culture_fit_note",     "label": "Notes",                    "type": "text",         "required": False, "parent": "culture_fit"},
    {"key": "overall_rating",       "label": "Overall Rating",           "type": "rating_5",     "required": True},
    {"key": "recommendation",       "label": "Recommendation",           "type": "single_choice","required": True,
     "options": ["Strong Hire", "Hire", "No Hire", "Strong No Hire"]},
    {"key": "strengths",            "label": "Strengths",                "type": "textarea",     "required": False},
    {"key": "concerns",             "label": "Concerns",                 "type": "textarea",     "required": False},
]

_VERDICT_MAP = {
    "Strong Hire":    "strong_yes",
    "Hire":           "yes",
    "No Hire":        "no",
    "Strong No Hire": "strong_no",
    # Interview Assessment Form's 4-point scale -- same cardinality as the
    # legacy one above but swaps the "strong negative" for a real middle
    # option, so it maps onto 3 of the same tokens plus a new "neutral" one
    # (see scorecard.verdict CHECK constraint, migration 66).
    "Strongly Recommend":  "strong_yes",
    "Recommend":           "yes",
    "Neutral":             "neutral",
    "Do Not Recommend":    "no",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _j(val):
    """Safely parse JSONB — psycopg2 may return dict/list or a string."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return None


def _ensure_default_form() -> dict:
    """Return the default feedback form, inserting it into DB if absent.
    Name lookup is case-insensitive (LOWER) to match idx_feedback_form_name_ci
    (Migration 58) -- a case-sensitive `name = %s` here previously created a
    fresh duplicate on every casing mismatch (e.g. seeded "Default panel
    scorecard" vs this constant's "Default Panel Scorecard")."""
    row = query_one(
        "SELECT id, schema FROM feedback_form WHERE LOWER(name) = LOWER(%s) AND is_active = TRUE LIMIT 1",
        [_DEFAULT_FORM_NAME],
    )
    if row:
        return {"id": str(row["id"]), "schema": _j(row["schema"]) or _DEFAULT_FORM_SCHEMA}
    try:
        inserted = query_one(
            "INSERT INTO feedback_form (name, schema) VALUES (%s, %s::jsonb) RETURNING id",
            [_DEFAULT_FORM_NAME, json.dumps(_DEFAULT_FORM_SCHEMA)],
        )
    except Exception:
        inserted = None
    if inserted:
        return {"id": str(inserted["id"]), "schema": _DEFAULT_FORM_SCHEMA}
    # Race: another request inserted it simultaneously (or the unique index
    # rejected our insert for that same reason)
    row2 = query_one("SELECT id, schema FROM feedback_form WHERE LOWER(name) = LOWER(%s) LIMIT 1", [_DEFAULT_FORM_NAME])
    return {"id": str(row2["id"]), "schema": _j(row2["schema"]) or _DEFAULT_FORM_SCHEMA}


def _form_for_interview(interview_id: str) -> dict:
    """Return the feedback form for this interview (per round_config, or default)."""
    rc = query_one(
        """SELECT rc.feedback_form_id
           FROM interview i
           JOIN round_config rc ON rc.id = i.round_config_id
           WHERE i.id = %s""",
        [interview_id],
    )
    if rc and rc.get("feedback_form_id"):
        form_row = query_one(
            "SELECT id, schema FROM feedback_form WHERE id = %s AND is_active = TRUE",
            [str(rc["feedback_form_id"])],
        )
        if form_row:
            return {"id": str(form_row["id"]), "schema": _j(form_row["schema"]) or _DEFAULT_FORM_SCHEMA}
    return _ensure_default_form()


def _is_panelist(interview_id: str, user_id: str) -> bool:
    """Authorised either via interview_panel (populated at booking time from
    the round's roster) or directly against round_config.panelist_emails --
    this second path stays authoritative even if the interview_panel insert
    was skipped (e.g. the panelist's account didn't exist yet when the
    interview was booked, or the roster was edited afterwards). Without this,
    a real roster member could still be locked out of their own scorecard."""
    return bool(query_one(
        """SELECT 1 FROM interview_panel WHERE interview_id = %s AND interviewer_id = %s
           UNION ALL
           SELECT 1
           FROM interview i
           JOIN round_config rc ON rc.id = i.round_config_id
           JOIN app_user u ON u.id = %s
           WHERE i.id = %s
             AND EXISTS (SELECT 1 FROM unnest(rc.panelist_emails) AS pe WHERE LOWER(pe) = LOWER(u.email))""",
        [interview_id, user_id, user_id, interview_id],
    ))


def _check_visibility(interview_id: str, user: dict) -> dict:
    """
    Return the interview row if caller is authorised to view data for this interview.
    Raises 404/403 otherwise.

    Visibility rules
    ----------------
    admin / ta_manager  → always
    recruiter           → application must be on one of their requisitions
    hiring_manager      → must be HM for the interview's requisition
    interviewer / other → must be in interview_panel for this interview
    """
    row = query_one(
        """SELECT i.id, i.application_id, i.status, i.scheduled_at,
                  c.full_name  AS candidate_name,
                  r.title      AS requisition,
                  r.id         AS req_id,
                  r.hiring_manager_id,
                  bu.name      AS department,
                  rc.name      AS round_name,
                  rc.id        AS round_config_id
           FROM interview i
           JOIN application  a  ON a.id  = i.application_id
           JOIN candidate    c  ON c.id  = a.candidate_id
           JOIN requisition  r  ON r.id  = a.requisition_id
           LEFT JOIN business_unit bu ON bu.id = r.bu_id
           JOIN round_config rc ON rc.id = i.round_config_id
           WHERE i.id = %s""",
        [interview_id],
    )
    if not row:
        raise HTTPException(404, "Interview not found")

    role = user["role"]
    uid  = user["sub"]

    if role in ("admin", "ta_manager"):
        return row

    if role == "recruiter":
        if not query_one(
            "SELECT 1 FROM requisition_recruiter WHERE requisition_id = %s AND recruiter_id = %s",
            [str(row["req_id"]), uid],
        ):
            raise HTTPException(403, "Not authorised for this interview")
        return row

    if role == "hiring_manager":
        # Owns the requisition, OR sits on this interview's panel -- the HM
        # dashboard's pending-scorecards query (hm_api.hm_dashboard) surfaces
        # "Give Feedback" cards for ANY interview the HM is panelling on, not
        # just interviews under requisitions they own (cross-team panels are
        # normal). Checking ownership only here 403'd those clicks and the
        # modal fell back to "Could not load scorecard form."
        if str(row.get("hiring_manager_id")) != uid and not _is_panelist(interview_id, uid):
            raise HTTPException(403, "Not authorised for this interview")
        return row

    # interviewer / other role: must be on the panel
    if not _is_panelist(interview_id, uid):
        raise HTTPException(403, "You are not on the panel for this interview")
    return row


def _validate_required(schema: list, form_data: dict) -> list:
    """Return labels of required fields that are missing a value."""
    return [
        f["label"] for f in schema
        if f.get("required") and not form_data.get(f["key"])
    ]


def _overall_score(schema: list, form_data: dict) -> Optional[float]:
    rating_keys = {f["key"] for f in schema if f["type"] == "rating_5"}
    vals = [v for k, v in form_data.items()
            if k in rating_keys and isinstance(v, (int, float)) and 1 <= float(v) <= 5]
    return round(sum(vals) / len(vals), 2) if vals else None


# ── Panel consensus → combined score update (Improvement 5) ──────────────────

def _recompute_panel_combined(application_id: str) -> None:
    """
    Called after each scorecard submission.

    Consensus rule (configurable later; currently hardcoded):
      ≥ 60 % 'Strong Hire' or 'Hire'        → panel_consensus = 'advance'
      ≥ 60 % 'No Hire' or 'Strong No Hire'  → panel_consensus = 'reject'
      otherwise                              → panel_consensus = 'split'

    Updated combined score (when ≥ 1 scorecard AND bot_score exists):
      0.35 × match_score + 0.50 × bot_score + 0.15 × panel_numeric

    Both panel_consensus and panel_numeric are written into score_breakdown
    JSONB and panel_consensus is also stored as a dedicated column so list
    queries can surface the badge without parsing JSONB.
    """
    app_row = query_one(
        "SELECT match_score, bot_score, score_breakdown FROM application WHERE id = %s",
        [application_id],
    )
    if not app_row:
        return

    scs = query(
        """SELECT s.overall_score, s.verdict
           FROM scorecard s
           JOIN interview i ON i.id = s.interview_id
           WHERE i.application_id = %s AND s.status = 'submitted'""",
        [application_id],
    )
    if not scs:
        return

    scores = [float(sc["overall_score"]) for sc in scs if sc.get("overall_score") is not None]
    if not scores:
        return

    # Convert avg 1–5 → 0–100
    panel_numeric = round(sum(scores) / len(scores) / 5.0 * 100.0, 1)

    # "neutral" (Interview Assessment Form's middle option) deliberately counts
    # toward neither bucket -- it only inflates `total`, making both thresholds
    # harder to hit and pulling an all/mostly-neutral panel toward "split",
    # which is the correct read of a panel that's genuinely on the fence.
    advance_verdicts = {"strong_yes", "yes"}
    reject_verdicts  = {"strong_no",  "no"}
    total  = len(scs)
    adv_ct = sum(1 for sc in scs if sc.get("verdict") in advance_verdicts)
    rej_ct = sum(1 for sc in scs if sc.get("verdict") in reject_verdicts)

    if total > 0 and adv_ct / total >= 0.60:
        panel_consensus = "advance"
    elif total > 0 and rej_ct / total >= 0.60:
        panel_consensus = "reject"
    else:
        panel_consensus = "split"

    # Update score_breakdown
    bd = app_row.get("score_breakdown") or {}
    if isinstance(bd, str):
        bd = json.loads(bd)
    bd["panel_numeric"]        = panel_numeric
    bd["panel_consensus"]      = panel_consensus
    bd["panel_submitted_count"] = total

    # Recompute combined_score only when bot_score is available
    match = float(app_row.get("match_score") or 0)
    bot   = app_row.get("bot_score")
    if bot is not None:
        combined = round(0.35 * match + 0.50 * float(bot) + 0.15 * panel_numeric, 1)
        query(
            """UPDATE application
               SET combined_score  = %s,
                   score_breakdown = %s::jsonb,
                   panel_consensus = %s
               WHERE id = %s""",
            [combined, json.dumps(bd), panel_consensus, application_id],
            fetch=False,
        )
    else:
        # Bot interview not done yet — persist panel info but don't touch combined_score
        query(
            """UPDATE application
               SET score_breakdown = %s::jsonb,
                   panel_consensus = %s
               WHERE id = %s""",
            [json.dumps(bd), panel_consensus, application_id],
            fetch=False,
        )


# ── GET active feedback forms (for the round-config feedback-form picker) ───

@router.get("/feedback-forms")
def list_feedback_forms(user: dict = Depends(get_current_user)):
    """Minimal list for the requisition round-config picker to attach a
    per-round form -- round_config.feedback_form_id already existed and was
    read here, but nothing could ever set it because no endpoint listed the
    options. No form-authoring UI here, just selection among what exists."""
    if user["role"] not in ("recruiter", "ta_manager", "admin", "hiring_manager", "hrbp"):
        raise HTTPException(403, "Not authorised")
    return query(
        "SELECT id, name FROM feedback_form WHERE is_active = TRUE AND tenant_id = %s ORDER BY name",
        [user.get("tenant_id")],
    )


def _org_name(tenant_id: str = None) -> str:
    if tenant_id:
        row = query_one("SELECT value FROM system_settings WHERE tenant_id = %s AND key = 'company_name'", [tenant_id])
    else:
        row = query_one("SELECT value FROM system_settings WHERE key = 'company_name'")
    return (row.get("value") if row else None) or "EnternsTech Pvt. Ltd."


# ── GET scorecard form + caller's existing scorecard ─────────────────────────

@router.get("/interviews/{interview_id}/scorecard-form")
def get_scorecard_form(interview_id: str, user: dict = Depends(get_current_user)):
    uid  = user["sub"]
    role = user["role"]

    interview = _check_visibility(interview_id, user)
    form      = _form_for_interview(interview_id)

    my_sc = query_one(
        """SELECT id, form_data, overall_score, verdict, status, submitted_at, created_at
           FROM scorecard
           WHERE interview_id = %s AND interviewer_id = %s""",
        [interview_id, uid],
    )
    fd = _j(my_sc["form_data"]) if my_sc else {}

    is_panel     = _is_panelist(interview_id, uid)
    submitted_own = my_sc and my_sc.get("status") == "submitted"

    # Bias control: panelist may only see others after submitting their own.
    # Recruiters / HMs / TA / Admin who are NOT on the panel can always see all scores.
    can_see_others = (
        (is_panel and submitted_own) or
        role in ("admin", "ta_manager") or
        (role in ("recruiter", "hiring_manager") and not is_panel)
    )

    return {
        "interview": {
            "id":             str(interview["id"]),
            "application_id": str(interview["application_id"]),
            "candidate_name": interview["candidate_name"],
            "requisition":    interview["requisition"],
            "department":     interview.get("department"),
            "round_name":     interview["round_name"],
            "status":         interview["status"],
            "scheduled_at":   interview["scheduled_at"].isoformat() if interview.get("scheduled_at") else None,
        },
        "organization": _org_name(user.get("tenant_id")),
        "interviewer_name": user.get("name"),
        "form":        form,
        "my_scorecard": {
            "id":                str(my_sc["id"]) if my_sc else None,
            "form_data":         fd or {},
            "overall_score":     float(my_sc["overall_score"]) if my_sc and my_sc.get("overall_score") else None,
            "verdict":           my_sc.get("verdict") if my_sc else None,
            "status":            my_sc.get("status", "not_started") if my_sc else "not_started",
            "submitted_at":      my_sc["submitted_at"].isoformat() if my_sc and my_sc.get("submitted_at") else None,
            # Digital-signature substitute (no signature capture) -- the
            # submitting user's identity + timestamp, per the "Submitted by
            # {name} on {date}" line in the modal footer once locked.
            "submitted_by_name": user.get("name") if my_sc and my_sc.get("status") == "submitted" else None,
        },
        "is_panelist":    is_panel,
        "can_see_others": can_see_others,
    }


# ── POST save draft / submit scorecard ───────────────────────────────────────

class ScorecardIn(BaseModel):
    form_data: dict
    action: str  # "draft" or "submit"


@router.post("/interviews/{interview_id}/scorecard")
def save_scorecard(
    interview_id: str,
    body: ScorecardIn,
    user: dict = Depends(get_current_user),
):
    uid = user["sub"]

    if not _is_panelist(interview_id, uid):
        raise HTTPException(403, "Only panel members can submit scorecards")

    iv_ctx = query_one(
        """SELECT i.id, a.status AS app_status, a.current_round, rc.sequence
           FROM interview i
           JOIN application a  ON a.id = i.application_id
           JOIN round_config rc ON rc.id = i.round_config_id
           WHERE i.id = %s""",
        [interview_id],
    )
    if not iv_ctx:
        raise HTTPException(404, "Interview not found")

    # Read-only enforcement for cross-round carry-forward: the SAME hiring
    # manager is legitimately a panelist on every round of a requisition (they
    # get added to interview_panel at every round's booking), so _is_panelist
    # alone can't tell "my round" from "an earlier round I also sit on." Only
    # the round matching the application's current active round is writable --
    # every earlier round is permanently read-only from here on, even for its
    # own original submitter. This must be server-side; the frontend disabling
    # fields is not enough (the tell is a crafted API call, not the UI).
    if iv_ctx["app_status"] != "interview" or iv_ctx["sequence"] != iv_ctx["current_round"]:
        raise HTTPException(
            403,
            "This round is read-only — feedback can only be submitted or edited "
            "for the candidate's current round.",
        )

    existing = query_one(
        "SELECT id, status FROM scorecard WHERE interview_id = %s AND interviewer_id = %s",
        [interview_id, uid],
    )
    if existing and existing.get("status") == "submitted":
        raise HTTPException(409, "Scorecard already submitted and locked")

    form   = _form_for_interview(interview_id)
    action = (body.action or "draft").lower()

    if action == "submit":
        missing = _validate_required(form["schema"], body.form_data)
        if missing:
            raise HTTPException(422, f"Required fields missing: {', '.join(missing)}")

    score    = _overall_score(form["schema"], body.form_data)
    verdict  = _VERDICT_MAP.get(body.form_data.get("recommendation"))
    form_json = json.dumps(body.form_data)
    status   = "submitted" if action == "submit" else "draft"

    if existing:
        if action == "submit":
            query(
                """UPDATE scorecard
                   SET form_data = %s::jsonb, overall_score = %s, verdict = %s,
                       status = %s, feedback_form_id = %s, submitted_at = now()
                   WHERE interview_id = %s AND interviewer_id = %s""",
                [form_json, score, verdict, status, form["id"], interview_id, uid],
                fetch=False,
            )
        else:
            query(
                """UPDATE scorecard
                   SET form_data = %s::jsonb, overall_score = %s, verdict = %s,
                       status = %s, feedback_form_id = %s
                   WHERE interview_id = %s AND interviewer_id = %s""",
                [form_json, score, verdict, status, form["id"], interview_id, uid],
                fetch=False,
            )
    else:
        if action == "submit":
            query(
                """INSERT INTO scorecard
                   (interview_id, interviewer_id, feedback_form_id,
                    form_data, overall_score, verdict, status, submitted_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, now())""",
                [interview_id, uid, form["id"], form_json, score, verdict, status],
                fetch=False,
            )
        else:
            query(
                """INSERT INTO scorecard
                   (interview_id, interviewer_id, feedback_form_id,
                    form_data, overall_score, verdict, status)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)""",
                [interview_id, uid, form["id"], form_json, score, verdict, status],
                fetch=False,
            )

    updated = query_one(
        """SELECT id, overall_score, verdict, status, submitted_at
           FROM scorecard
           WHERE interview_id = %s AND interviewer_id = %s""",
        [interview_id, uid],
    )

    # Improvement 5: recompute panel consensus + combined score on every submit
    if action == "submit":
        iv_row = query_one(
            """SELECT i.application_id, a.requisition_id, i.scheduled_at
               FROM interview i
               JOIN application a ON a.id = i.application_id
               WHERE i.id = %s""",
            [interview_id],
        )
        if iv_row and iv_row.get("application_id"):
            try:
                _recompute_panel_combined(str(iv_row["application_id"]))
            except Exception as _pc_exc:
                print(f"[scorecard] panel combined recompute failed: {_pc_exc}")
                # Stale combined_score/consensus would otherwise look
                # authoritative to whoever decides advancement next -- flag
                # the application so the UI can warn instead of showing
                # silently-outdated numbers, and log a durable trace.
                try:
                    query(
                        """UPDATE application
                             SET flags = flags || jsonb_build_object('scores_stale', true)
                           WHERE id = %s""",
                        [str(iv_row["application_id"])], fetch=False,
                    )
                except Exception:
                    pass
                log_activity(
                    "application", "scorecard_recompute_failed",
                    entity_id=str(iv_row["application_id"]), application_id=str(iv_row["application_id"]),
                    requisition_id=iv_row.get("requisition_id"),
                    actor_id=None, actor_role="system",
                    detail={"interview_id": interview_id, "error": str(_pc_exc)},
                )
            # Gamification: panel_pass when positive recommendation
            try:
                from ..services.gamification import award as _gam_award
                req_id_str = str(iv_row["requisition_id"]) if iv_row.get("requisition_id") else None
                app_id_str = str(iv_row["application_id"])
                if verdict in ("strong_yes", "yes"):
                    _gam_award("recruiter", uid, "panel_pass", req_id_str, app_id_str)
                # feedback_on_time only if submitted within the configured window
                # (default 48h) after the interview's scheduled time.
                if _feedback_is_on_time(iv_row.get("scheduled_at")):
                    _gam_award("recruiter", uid, "feedback_on_time", req_id_str, app_id_str)
            except Exception as _ge_exc:
                print(f"[scorecard] gamification award failed: {_ge_exc}")

            # Every panelist submits at most once (scorecard locks after
            # submit, see the 409 check above), so "the panel just became
            # fully submitted" is a one-time transition -- ping the hiring
            # manager immediately rather than waiting on
            # hm_feedback_reminder_worker's first pass (~1h+ after the
            # interview, on a 30-min poll). This was the actual gap behind
            # candidates getting rejected with no HM feedback and no email
            # ever sent -- nothing previously notified the HM the moment
            # feedback was actually ready to review.
            try:
                panel_counts = query_one(
                    """SELECT COUNT(*) AS expected,
                              COUNT(*) FILTER (WHERE s.status = 'submitted') AS submitted
                       FROM interview_panel ip
                       LEFT JOIN scorecard s
                              ON s.interview_id = ip.interview_id AND s.interviewer_id = ip.interviewer_id
                       WHERE ip.interview_id = %s""",
                    [interview_id],
                )
                if panel_counts and panel_counts["expected"] > 0 and panel_counts["expected"] == panel_counts["submitted"]:
                    from ..services.hm_feedback_reminder_worker import send_feedback_ready_email
                    send_feedback_ready_email(interview_id)
            except Exception as _nf_exc:
                print(f"[scorecard] hm feedback-ready notify failed: {_nf_exc}")

    return {
        "ok": True,
        "scorecard": {
            "id":           str(updated["id"]),
            "status":       updated["status"],
            "overall_score": float(updated["overall_score"]) if updated.get("overall_score") else None,
            "verdict":      updated.get("verdict"),
            "submitted_at": updated["submitted_at"].isoformat() if updated.get("submitted_at") else None,
            "submitted_by_name": user.get("name") if updated["status"] == "submitted" else None,
        },
    }


# ── GET PDF export of the caller's own scorecard ─────────────────────────────

@router.get("/interviews/{interview_id}/scorecard/pdf")
def get_scorecard_pdf(interview_id: str, user: dict = Depends(get_current_user)):
    uid = user["sub"]

    interview = _check_visibility(interview_id, user)
    form = _form_for_interview(interview_id)

    my_sc = query_one(
        """SELECT form_data, overall_score, status, submitted_at
           FROM scorecard
           WHERE interview_id = %s AND interviewer_id = %s""",
        [interview_id, uid],
    )
    if not my_sc:
        raise HTTPException(404, "You have no scorecard on this interview yet")

    pdf_bytes = render_scorecard_pdf(
        organization=_org_name(user.get("tenant_id")),
        candidate_name=interview["candidate_name"],
        requisition=interview["requisition"],
        department=interview.get("department"),
        round_name=interview["round_name"],
        scheduled_at_str=interview["scheduled_at"].strftime("%d %b %Y") if interview.get("scheduled_at") else None,
        interviewer_name=user.get("name") or "",
        schema=form["schema"],
        form_data=_j(my_sc["form_data"]) or {},
        overall_score=float(my_sc["overall_score"]) if my_sc.get("overall_score") is not None else None,
        submitted_by_name=user.get("name") if my_sc.get("status") == "submitted" else None,
        submitted_at_str=my_sc["submitted_at"].strftime("%d %b %Y, %H:%M") if my_sc.get("submitted_at") else None,
    )
    safe_name = "".join(c for c in interview["candidate_name"] if c.isalnum() or c in " _-").strip() or "Candidate"
    return stream_pdf(pdf_bytes, f"Scorecard - {safe_name}.pdf")


def _derive_consensus(token_counts: dict, total: int) -> Optional[str]:
    """Return panel_consensus label from a verdict-TOKEN histogram (strong_yes/
    yes/neutral/no/strong_no -- the internal values in _VERDICT_MAP, not the
    raw `recommendation` display label), or None if no scorecards.

    Keying on tokens rather than labels is what lets this work across forms
    with different recommendation wording (e.g. "Strong Hire" vs "Strongly
    Recommend") without hardcoding every form's label set here -- this used to
    match on labels directly, which silently produced "split" for any form
    using different wording since none of its labels ever matched.
    "neutral" counts toward neither side, same as _recompute_panel_combined."""
    if total == 0:
        return None
    adv = sum(v for k, v in token_counts.items() if k in ("strong_yes", "yes"))
    rej = sum(v for k, v in token_counts.items() if k in ("strong_no", "no"))
    if adv / total >= 0.60:
        return "advance"
    if rej / total >= 0.60:
        return "reject"
    return "split"


# ── GET aggregated panel feedback for an interview ───────────────────────────

@router.get("/interviews/{interview_id}/panel-feedback")
def get_panel_feedback(interview_id: str, user: dict = Depends(get_current_user)):
    uid  = user["sub"]
    role = user["role"]

    interview = _check_visibility(interview_id, user)

    # Bias guard: a panelist who hasn't submitted cannot see others' scores
    if _is_panelist(interview_id, uid) and role not in ("admin", "ta_manager"):
        my_sc = query_one(
            "SELECT status FROM scorecard WHERE interview_id = %s AND interviewer_id = %s",
            [interview_id, uid],
        )
        if not my_sc or my_sc.get("status") != "submitted":
            raise HTTPException(403, "Submit your own scorecard before viewing others")

    # Fall back for the interview's OWN (current) form -- used for any
    # scorecard row that predates the feedback_form_id column (NULL) -- but
    # every other scorecard's ratings are read using the form THAT scorecard
    # was actually submitted under (see the per-row lookup below). A round's
    # form assignment can change after some panelists already submitted (this
    # feature adds exactly that: bulk-reassigning existing rounds onto a new
    # form) -- keying every entry off "whatever form this round points at
    # today" would silently drop an earlier submission's ratings the moment
    # its keys stop matching the round's current schema.
    current_form = _form_for_interview(interview_id)

    scs = query(
        """SELECT s.form_data, s.overall_score, s.verdict, s.submitted_at, s.feedback_form_id,
                  u.full_name AS interviewer_name, u.role AS interviewer_role
           FROM scorecard s
           JOIN app_user u ON u.id = s.interviewer_id
           WHERE s.interview_id = %s AND s.status = 'submitted'
           ORDER BY s.submitted_at""",
        [interview_id],
    )

    _form_cache: dict = {}

    def _rating_fields(feedback_form_id) -> tuple:
        if not feedback_form_id:
            schema = current_form["schema"]
        else:
            key = str(feedback_form_id)
            if key not in _form_cache:
                row = query_one("SELECT schema FROM feedback_form WHERE id = %s", [key])
                _form_cache[key] = _j(row["schema"]) if row else current_form["schema"]
            schema = _form_cache[key]
        keys = [f["key"] for f in schema if f["type"] == "rating_5"]
        labels = {f["key"]: f["label"] for f in schema if f["type"] == "rating_5"}
        return keys, labels

    entries = []
    rating_labels: dict = {}
    for sc in (scs or []):
        fd = _j(sc["form_data"]) or {}
        own_rating_keys, own_rating_labels = _rating_fields(sc.get("feedback_form_id"))
        rating_labels.update(own_rating_labels)  # union across every form actually used
        entries.append({
            "interviewer_name": sc["interviewer_name"],
            "interviewer_role": sc["interviewer_role"],
            "verdict":          sc["verdict"],
            "overall_score":    float(sc["overall_score"]) if sc.get("overall_score") else None,
            "recommendation":   fd.get("recommendation"),
            "strengths":        fd.get("strengths"),
            "concerns":         fd.get("concerns"),
            "ratings":          {k: fd[k] for k in own_rating_keys if fd.get(k)},
            "submitted_at":     sc["submitted_at"].isoformat() if sc.get("submitted_at") else None,
        })
    rating_keys = list(rating_labels.keys())

    # Roll-up. verdict_counts stays keyed by the raw recommendation label
    # (frontend renders the key verbatim as a pill) -- token_counts, keyed by
    # the internal _VERDICT_MAP value, is what panel_consensus is derived
    # from, so consensus is correct regardless of which form's labels these
    # scorecards used.
    verdict_counts: dict = {}
    token_counts: dict = {}
    for e in entries:
        r = e.get("recommendation")
        if r:
            verdict_counts[r] = verdict_counts.get(r, 0) + 1
        t = e.get("verdict")
        if t:
            token_counts[t] = token_counts.get(t, 0) + 1

    scores    = [e["overall_score"] for e in entries if e.get("overall_score")]
    avg_all   = round(sum(scores) / len(scores), 2) if scores else None

    avg_ratings: dict = {}
    for k in rating_keys:
        vals = [e["ratings"][k] for e in entries if e["ratings"].get(k)]
        if vals:
            avg_ratings[k] = round(sum(vals) / len(vals), 2)

    panel = query(
        """SELECT u.full_name, u.role,
                  (s.id IS NOT NULL)        AS has_scorecard,
                  (s.status = 'submitted')  AS submitted
           FROM interview_panel ip
           JOIN app_user u ON u.id = ip.interviewer_id
           LEFT JOIN scorecard s
             ON s.interview_id = ip.interview_id AND s.interviewer_id = ip.interviewer_id
           WHERE ip.interview_id = %s
           ORDER BY u.full_name""",
        [interview_id],
    )

    return {
        "interview": {
            "id":             str(interview["id"]),
            "candidate_name": interview["candidate_name"],
            "requisition":    interview["requisition"],
            "round_name":     interview["round_name"],
            "status":         interview["status"],
        },
        "panel_members": [
            {"full_name": m["full_name"], "role": m["role"], "submitted": bool(m["submitted"])}
            for m in (panel or [])
        ],
        "scorecards": entries,
        "rollup": {
            "total_submitted":   len(entries),
            "verdict_counts":    verdict_counts,
            "avg_overall_score": avg_all,
            "avg_ratings":       avg_ratings,
            "rating_labels":     rating_labels,
            "panel_consensus":   _derive_consensus(token_counts, len(entries)),
        },
    }


# ── GET aggregated panel feedback across all rounds for an application ────────

def _round_transcript(round_type: str, notes: Optional[dict], enteri_ai_transcript: dict) -> dict:
    """Whichever transcript source a round actually produced: the Enteri AI
    session (bot rounds, one per application) or the Meeting Notetaker's
    interview_notes row (live/panel/hr rounds, one per interview).
    `notes` is the interview_notes row for this interview_id, pre-fetched by
    the caller (batched across every round of an application in one query
    rather than one query per round) -- None if that round has no notes."""
    if round_type == "bot_interview":
        if not enteri_ai_transcript:
            return {"source": "enteri_ai", "available": False}
        return {
            "source": "enteri_ai",
            "available": bool(enteri_ai_transcript.get("transcript") or enteri_ai_transcript.get("conversation")),
            "transcript": enteri_ai_transcript.get("conversation") or enteri_ai_transcript.get("transcript"),
        }
    if not notes:
        return {"source": "meeting_notes", "available": False, "fetch_status": "none"}
    return {
        "source": "meeting_notes",
        "available": bool(notes.get("transcript_text")),
        "fetch_status": notes.get("fetch_status"),
        "transcript_text": notes.get("transcript_text"),
        "summary": _j(notes.get("summary")),
    }


@router.get("/applications/{app_id}/panel-feedback")
def get_application_panel_feedback(app_id: str, user: dict = Depends(get_current_user)):
    """Consolidated, per-round feedback + score + transcript for an entire
    application -- one shot, no polling. Two audiences share this endpoint:
      - hrbp / ta_manager / recruiter / admin: full read access (scoped).
      - hiring_manager: their own requisition's application, ALL rounds
        (this is also what powers #3's cross-round context carry-forward --
        a round-2 HM calling this sees round-1's feedback/score/transcript
        read-only; the write-side lock lives in save_scorecard(), not here)."""
    role = user["role"]
    uid  = user["sub"]

    app_row = query_one(
        """SELECT a.id, a.requisition_id, a.current_round, a.status AS app_status,
                  r.title AS requisition, r.hiring_manager_id,
                  c.full_name AS candidate_name, c.resume_url
           FROM application a
           JOIN requisition r ON r.id = a.requisition_id
           JOIN candidate   c ON c.id = a.candidate_id
           WHERE a.id = %s""",
        [app_id],
    )
    if not app_row:
        raise HTTPException(404, "Application not found")

    if role == "recruiter":
        if not query_one(
            "SELECT 1 FROM requisition_recruiter WHERE requisition_id = %s AND recruiter_id = %s",
            [str(app_row["requisition_id"]), uid],
        ):
            raise HTTPException(403, "Not authorised")
    elif role == "hiring_manager":
        # Same fix as _check_visibility: owns the requisition, OR is a
        # panelist on at least one of this application's rounds -- an HM
        # panelling cross-team on someone else's requisition needs to read
        # this consolidated view too (e.g. from "View Panel Feedback" after
        # submitting their own scorecard), not just the requisition's owner.
        is_panelist_on_app = query_one(
            """SELECT 1 FROM interview i
               JOIN interview_panel ip ON ip.interview_id = i.id
               WHERE i.application_id = %s AND ip.interviewer_id = %s""",
            [app_id, uid],
        )
        if str(app_row.get("hiring_manager_id")) != uid and not is_panelist_on_app:
            raise HTTPException(403, "Not authorised")
    elif role == "hrbp":
        from .hrbp_api import scope_requisitions_for_hrbp
        where, params = scope_requisitions_for_hrbp(user)
        if not query_one(
            f"SELECT 1 FROM requisition r WHERE r.id = %s AND {where}",
            [str(app_row["requisition_id"]), *params],
        ):
            raise HTTPException(403, "Not authorised")
    elif role not in ("admin", "ta_manager"):
        raise HTTPException(403, "Not authorised")

    interviews = query(
        """SELECT i.id, i.status, i.scheduled_at,
                  rc.name AS round_name, rc.sequence, rc.round_type, rc.feedback_form_id
           FROM interview i
           JOIN round_config rc ON rc.id = i.round_config_id
           WHERE i.application_id = %s
           ORDER BY rc.sequence, i.scheduled_at""",
        [app_id],
    )

    # enteri_ai_session is unique per application -- fetch once, reuse for any
    # bot_interview-type round rather than re-querying per round.
    enteri_ai_transcript = query_one(
        "SELECT transcript, conversation FROM enteri_ai_session WHERE application_id = %s",
        [app_id],
    )

    iv_ids = [str(iv["id"]) for iv in (interviews or [])]

    # Batch every round's scorecards into one query instead of one per round.
    scs_by_iv: dict = {}
    if iv_ids:
        for sc in (query(
            """SELECT s.interview_id, s.form_data, s.overall_score, s.verdict, s.submitted_at,
                      s.feedback_form_id,
                      u.full_name AS interviewer_name, u.role AS interviewer_role
               FROM scorecard s
               JOIN app_user u ON u.id = s.interviewer_id
               WHERE s.interview_id = ANY(%s::uuid[]) AND s.status = 'submitted'
               ORDER BY s.interview_id, s.submitted_at""",
            [iv_ids],
        ) or []):
            scs_by_iv.setdefault(str(sc["interview_id"]), []).append(sc)

    # Batch every distinct feedback form actually in play: both what each
    # ROUND currently points at (for rounds with no scorecards yet) AND what
    # each individual SCORECARD was actually submitted under (form_ids can
    # diverge from the round's current assignment once that assignment
    # changes after a submission -- see get_panel_feedback's identical fix).
    form_ids = {str(iv["feedback_form_id"]) for iv in (interviews or []) if iv.get("feedback_form_id")}
    form_ids |= {str(sc["feedback_form_id"]) for scs in scs_by_iv.values() for sc in scs if sc.get("feedback_form_id")}
    forms_by_id: dict = {}
    if form_ids:
        for fr in (query(
            "SELECT id, schema FROM feedback_form WHERE id = ANY(%s::uuid[]) AND is_active = TRUE",
            [list(form_ids)],
        ) or []):
            forms_by_id[str(fr["id"])] = {"id": str(fr["id"]), "schema": _j(fr["schema"]) or _DEFAULT_FORM_SCHEMA}
    _default_form_cache: Optional[dict] = None

    def _form_for(iv_row: dict) -> dict:
        nonlocal _default_form_cache
        fid = iv_row.get("feedback_form_id")
        if fid and str(fid) in forms_by_id:
            return forms_by_id[str(fid)]
        if _default_form_cache is None:
            _default_form_cache = _ensure_default_form()
        return _default_form_cache

    def _rating_fields_for_scorecard(sc: dict, round_form: dict) -> list:
        """The rating_5 keys for whichever form THIS scorecard was actually
        submitted under, not necessarily the round's current form (see the
        batching comment above)."""
        fid = sc.get("feedback_form_id")
        schema = forms_by_id.get(str(fid), round_form)["schema"] if fid else round_form["schema"]
        return [f["key"] for f in schema if f["type"] == "rating_5"]

    # Batch every non-bot round's Meeting Notetaker transcript into one query
    # (bot rounds reuse enteri_ai_transcript, already fetched once above).
    non_bot_ids = [str(iv["id"]) for iv in (interviews or []) if iv["round_type"] != "bot_interview"]
    notes_by_iv: dict = {}
    if non_bot_ids:
        for nr in (query(
            """SELECT interview_id, transcript_text, summary, fetch_status
               FROM interview_notes WHERE interview_id = ANY(%s::uuid[])""",
            [non_bot_ids],
        ) or []):
            notes_by_iv[str(nr["interview_id"])] = nr

    rounds = []
    for iv in (interviews or []):
        iv_id = str(iv["id"])
        scs   = scs_by_iv.get(iv_id, [])
        form  = _form_for(iv)

        entries = []
        for sc in scs:
            fd = _j(sc["form_data"]) or {}
            own_rating_keys = _rating_fields_for_scorecard(sc, form)
            entries.append({
                "interviewer_name": sc["interviewer_name"],
                "interviewer_role": sc["interviewer_role"],
                "verdict":          sc["verdict"],
                "overall_score":    float(sc["overall_score"]) if sc.get("overall_score") else None,
                "recommendation":   fd.get("recommendation"),
                "ratings":          {k: fd[k] for k in own_rating_keys if fd.get(k)},
                "strengths":        fd.get("strengths"),
                "concerns":         fd.get("concerns"),
                "submitted_at":     sc["submitted_at"].isoformat() if sc.get("submitted_at") else None,
            })

        vc: dict = {}
        for e in entries:
            r = e.get("recommendation")
            if r:
                vc[r] = vc.get(r, 0) + 1

        scores = [e["overall_score"] for e in entries if e.get("overall_score")]
        # Read-only tell for the UI: only the round matching the application's
        # current active round (while it's still in the interview stage) is
        # editable -- everything else is prior/historical context. The server
        # enforces this independently in save_scorecard(); this is display-only.
        is_current_round = (
            app_row.get("app_status") == "interview"
            and iv["sequence"] == app_row.get("current_round")
        )
        rounds.append({
            "interview_id":     iv_id,
            "round_name":       iv["round_name"],
            "round_type":       iv["round_type"],
            "sequence":         iv["sequence"],
            "status":           iv["status"],
            "scheduled_at":     iv["scheduled_at"].isoformat() if iv.get("scheduled_at") else None,
            "is_current_round": is_current_round,
            "scorecards":       entries,
            "transcript":       _round_transcript(iv["round_type"], notes_by_iv.get(iv_id), enteri_ai_transcript),
            "rollup": {
                "total_submitted":  len(entries),
                "verdict_counts":   vc,
                "avg_overall_score": round(sum(scores) / len(scores), 2) if scores else None,
            },
        })

    return {
        "candidate_name": app_row["candidate_name"],
        "requisition":    app_row["requisition"],
        "current_round":  app_row.get("current_round"),
        "app_status":      app_row.get("app_status"),
        "resume_filename": app_row.get("resume_url"),
        "rounds":          rounds,
    }
