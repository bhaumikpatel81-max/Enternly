"""
HM Experience API — Hiring Manager home dashboard and TA-approval workflow.

Read endpoints: scoped server-side to hiring_manager_id = current uid.
Allowed roles for dashboard: hiring_manager + admin (for debugging).
ta-approve / ta-reject: ta_manager / admin only — 403 for any other role.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..module_access import recruiter_has_module
from ..services.sla import (
    STAGE_SLA_KEY,
    PIPELINE_STAGE_LABELS,
    load_config,
    compute_rag,
)
from ..services.connectors import send_email
from ..services.email_templates import render_template
from ..services.activity_log import log_activity
from .scheduling_api import _my_pending_query

router = APIRouter(prefix="/api", tags=["hm"])

_TERMINAL = frozenset({
    "hired", "rejected", "on_hold",
})
_TERMINAL_TUPLE = ("hired", "rejected", "on_hold")

# A requisition's `hiring_manager_id` is only the *default* HM at the time the
# req was created/last edited -- it can drift out of sync with who's actually
# running a given candidate's interview: interview_schedule_request.hm_user_id
# is snapshotted independently (scheduling_api._insert_schedule_request_tx)
# and can be repointed via PATCH /api/scheduling/request/{id}/assign-hm without
# ever touching the requisition row, and a requisition edit can reassign
# hiring_manager_id after a request/interview already went out under the old
# HM. Scoping "my requisitions" to hiring_manager_id alone silently dropped
# any requisition/candidate an HM is actually engaged with via one of those
# other two paths -- broadened to an OR of all three so an HM never loses
# visibility into a candidate they were actually asked to interview.
_HM_REQ_SCOPE_SQL = """r.id IN (
    SELECT id FROM requisition WHERE hiring_manager_id = %s
    UNION
    SELECT a.requisition_id FROM interview_schedule_request isr
      JOIN application a ON a.id = isr.application_id
     WHERE isr.hm_user_id = %s
    UNION
    SELECT a.requisition_id FROM interview_panel ip
      JOIN interview i ON i.id = ip.interview_id
      JOIN application a ON a.id = i.application_id
     WHERE ip.interviewer_id = %s
)"""


def _hm_scope_params(uid: str) -> list:
    return [uid, uid, uid]


# ── helpers ────────────────────────────────────────────────────────────────────

def _require_hm(user: dict):
    if user["role"] not in ("hiring_manager", "admin"):
        raise HTTPException(403, "Hiring Manager access required")


def _require_ta(user: dict):
    if user["role"] in ("ta_manager", "admin"):
        return
    if user["role"] == "recruiter" and recruiter_has_module(user.get("sub"), "req_approvals"):
        return
    raise HTTPException(403, "TA Manager or Admin access required")


def _user_email_name(user_id: str) -> tuple[Optional[str], str]:
    row = query_one("SELECT email, full_name FROM app_user WHERE id=%s", [user_id])
    if row:
        return row["email"], (row["full_name"] or "—")
    return None, "—"


def _ta_manager_emails() -> list[str]:
    rows = query(
        "SELECT email FROM app_user WHERE role='ta_manager' AND is_active=TRUE", []
    )
    return [r["email"] for r in (rows or []) if r.get("email")]


_HM_REQ_TEMPLATE_META = {
    "hm_req_approved": ("Requisition<br>Approved.", "Great news — your requisition has been approved and is now active in the pipeline."),
    "hm_req_rejected": ("Requisition<br>Not Approved.", "Your requisition could not be approved at this time."),
}


def _send_safe(
    template_key: str,
    values: dict,
    to_emails: list[str],
    req_id: str | None = None,
    actor: dict | None = None,
) -> bool:
    """Returns whether every recipient was actually emailed -- callers must
    surface/log a real notified flag instead of a blind success."""
    if not to_emails:
        return False
    try:
        from ..services.connectors import resolve_global_placeholders
        from ..services.email_layout import build_branded_email
        globals_ = resolve_global_placeholders(req_id=req_id, actor=actor)
        reply_to = globals_.get("recruiter_email") or None
        subject, body = render_template(template_key, values, req_id=req_id, actor=actor)

        hero_title_html, hero_subtitle = _HM_REQ_TEMPLATE_META.get(
            template_key, ("Requisition<br>Update.", "There's an update on your requisition."),
        )
        detail_cells = [
            ("Requisition", values.get("req_title") or ""),
            ("Hiring Manager", values.get("hm_name") or ""),
        ]
        if values.get("reason"):
            detail_cells.append(("Reason", values["reason"]))
        html_body = build_branded_email(
            eyebrow="Application Tracking System",
            hero_title_html=hero_title_html,
            hero_subtitle=hero_subtitle,
            detail_cells=detail_cells,
            about_text=body,
            about_heading=None,
            cta_label=None, cta_link=None,
        )

        all_sent = True
        for addr in to_emails:
            try:
                send_email(addr, subject, body, html=html_body, reply_to=reply_to)
            except Exception as exc:
                all_sent = False
                print(f"[hm_api] email to {addr} failed: {exc}")
        return all_sent
    except Exception as exc:
        print(f"[hm_api] render_template({template_key!r}) failed: {exc}")
        return False


def _rag_sort_key(item: dict) -> int:
    return {"red": 0, "amber": 1, "green": 2}.get(
        (item.get("rag") or {}).get("status", "green"), 2
    )


# ── GET /api/hm/dashboard ─────────────────────────────────────────────────────

@router.get("/hm/dashboard")
def hm_dashboard(user: dict = Depends(get_current_user)):
    _require_hm(user)
    uid = user["sub"]
    sla_cfg = load_config()

    # ── 1. Pending scorecards ─────────────────────────────────────────────────
    # Interviews on the HM's reqs where the HM is on the panel but has not
    # yet submitted a scorecard (missing or draft).
    sc_rows = query(
        """
        SELECT
            i.id        AS interview_id,
            i.scheduled_at,
            c.full_name AS candidate_name,
            r.title     AS req_title,
            r.id        AS req_id,
            rc.name     AS round_name,
            COALESCE(s.status, 'not_started') AS sc_status,
            EXTRACT(EPOCH FROM (now() - COALESCE(i.scheduled_at, now()))) / 86400.0
                        AS days_waiting
        FROM interview i
        JOIN application   a  ON a.id  = i.application_id
        JOIN candidate     c  ON c.id  = a.candidate_id
        JOIN requisition   r  ON r.id  = a.requisition_id
        JOIN round_config  rc ON rc.id = i.round_config_id
        JOIN interview_panel ip
             ON ip.interview_id = i.id AND ip.interviewer_id = %s
        LEFT JOIN scorecard s
             ON s.interview_id = i.id AND s.interviewer_id = %s
        WHERE COALESCE(i.status, 'scheduled') != 'cancelled'
          AND (s.id IS NULL OR s.status = 'draft')
        ORDER BY i.scheduled_at NULLS LAST
        LIMIT 20
        """,
        [uid, uid],
    )
    sla_feedback = sla_cfg.get("stage_interview", 5)
    pending_scorecards = []
    for r in (sc_rows or []):
        days = float(r["days_waiting"] or 0)
        rag  = compute_rag(days, sla_feedback)
        pending_scorecards.append({
            "type":           "scorecard",
            "interview_id":   str(r["interview_id"]),
            "candidate_name": r["candidate_name"],
            "req_title":      r["req_title"],
            "req_id":         str(r["req_id"]),
            "round_name":     r["round_name"],
            "sc_status":      r["sc_status"],
            "days_waiting":   round(days, 1),
            "rag":            rag,
        })
    pending_scorecards.sort(key=_rag_sort_key)

    # ── 2. Pending offer approvals (HM is current sequential approver) ────────
    appr_rows = query(
        """
        SELECT
            o.id         AS offer_id,
            o.current_step,
            o.designation,
            o.total_ctc,
            oas.sla_days,
            oas.created_at AS step_created_at,
            (SELECT COUNT(*) FROM offer_approval_step oas2
             WHERE oas2.offer_id = o.id)            AS total_steps,
            c.full_name  AS candidate_name,
            r.title      AS req_title,
            r.id         AS req_id,
            a.id         AS application_id,
            EXTRACT(EPOCH FROM (now() - oas.created_at)) / 86400.0
                         AS days_waiting
        FROM offer o
        JOIN offer_approval_step oas
             ON oas.offer_id   = o.id
            AND oas.sequence   = o.current_step
            AND oas.approver_id = %s
            AND oas.status      = 'pending'
        JOIN application  a ON a.id = o.application_id
        JOIN candidate    c ON c.id = a.candidate_id
        JOIN requisition  r ON r.id = a.requisition_id
        WHERE o.status = 'pending_approval'
        ORDER BY oas.created_at NULLS LAST
        LIMIT 20
        """,
        [uid],
    )
    pending_approvals = []
    for r in (appr_rows or []):
        days     = float(r["days_waiting"] or 0)
        sla_days = int(r["sla_days"] or 2)
        rag      = compute_rag(days, sla_days)
        pending_approvals.append({
            "type":           "approval",
            "offer_id":       str(r["offer_id"]),
            "candidate_name": r["candidate_name"],
            "req_title":      r["req_title"],
            "req_id":         str(r["req_id"]),
            "application_id": str(r["application_id"]),
            "designation":    r["designation"],
            "total_ctc":      float(r["total_ctc"]) if r["total_ctc"] else None,
            "current_step":   int(r["current_step"]),
            "total_steps":    int(r["total_steps"]),
            "days_waiting":   round(days, 1),
            "rag":            rag,
        })
    pending_approvals.sort(key=_rag_sort_key)

    # ── 3. Awaiting HM decision (interview + shortlisted stages on HM's reqs) ─
    dec_rows = query(
        f"""
        SELECT
            a.id           AS app_id,
            a.status,
            a.current_round,
            c.full_name    AS candidate_name,
            r.title        AS req_title,
            r.id           AS req_id,
            EXTRACT(EPOCH FROM (
                now() - COALESCE(
                    (SELECT se.occurred_at FROM stage_event se
                     WHERE se.application_id = a.id
                       AND se.to_status = a.status
                     ORDER BY se.occurred_at DESC LIMIT 1),
                    a.applied_at
                )
            )) / 86400.0   AS days_in_stage
        FROM application a
        JOIN candidate   c ON c.id = a.candidate_id
        JOIN requisition r ON r.id = a.requisition_id
        WHERE {_HM_REQ_SCOPE_SQL}
          AND a.status IN ('interview', 'shortlisted')
          AND a.status NOT IN ({', '.join(['%s']*len(_TERMINAL_TUPLE))})
        ORDER BY a.status, days_in_stage DESC NULLS LAST
        LIMIT 30
        """,
        _hm_scope_params(uid) + list(_TERMINAL_TUPLE),
    )
    awaiting_decision = []
    for r in (dec_rows or []):
        days    = float(r["days_in_stage"] or 0)
        sla_key = STAGE_SLA_KEY.get(r["status"], "stage_default")
        target  = sla_cfg.get(sla_key, sla_cfg.get("stage_default", 5))
        rag     = compute_rag(days, target)
        awaiting_decision.append({
            "type":           "decision",
            "app_id":         str(r["app_id"]),
            "candidate_name": r["candidate_name"],
            "req_title":      r["req_title"],
            "req_id":         str(r["req_id"]),
            "stage":          r["status"],
            "stage_label":    PIPELINE_STAGE_LABELS.get(r["status"], r["status"]),
            "current_round":  r["current_round"],
            "days_in_stage":  round(days, 1),
            "rag":            rag,
        })
    awaiting_decision.sort(key=_rag_sort_key)

    # ── 4. Pending interview-availability requests (HM hasn't submitted yet) ──
    # Same query scheduling_api.my_pending() exposes at GET
    # /api/scheduling/my-pending -- shared so the SQL lives once. This is the
    # actual fix for the "AWAITING HM but Action Queue is empty" bug: the
    # dashboard HMs land on never looked at interview_schedule_request before.
    pending_availability = [{
        "type":           "availability",
        "request_id":     str(r["id"]),
        "candidate_name": r["candidate_name"],
        "req_title":      r["job_title"],
        "req_id":         str(r["req_id"]),
        "duration_min":   r["duration_min"],
        "days_waiting":   round((datetime.now(timezone.utc) - r["created_at"]).total_seconds() / 86400.0, 1),
    } for r in _my_pending_query(uid)]

    # ── 5. My requisitions with per-stage counts + approval badge ─────────────
    req_rows = query(
        f"""
        SELECT
            r.id, r.title, r.status, r.req_code,
            COALESCE(r.approval_status, 'approved') AS approval_status,
            r.openings,
            EXTRACT(EPOCH FROM (
                now() - COALESCE(r.opened_at, r.created_at)
            )) / 86400.0                             AS open_days,
            COUNT(a.id) FILTER (WHERE a.status = 'applied')       AS cnt_applied,
            COUNT(a.id) FILTER (WHERE a.status = 'screen')        AS cnt_screen,
            COUNT(a.id) FILTER (WHERE a.status = 'nexai_bot')     AS cnt_nexai_bot,
            COUNT(a.id) FILTER (WHERE a.status = 'shortlisted')   AS cnt_shortlisted,
            COUNT(a.id) FILTER (WHERE a.status = 'interview')     AS cnt_interview,
            COUNT(a.id) FILTER (WHERE a.status = 'documentation') AS cnt_documentation,
            COUNT(a.id) FILTER (WHERE a.status = 'offered')       AS cnt_offered,
            COUNT(a.id) FILTER (WHERE a.status = 'hired')         AS cnt_hired
        FROM requisition r
        LEFT JOIN application a ON a.requisition_id = r.id
        WHERE {_HM_REQ_SCOPE_SQL}
        GROUP BY r.id, r.title, r.status, r.req_code,
                 r.approval_status, r.openings, r.opened_at, r.created_at
        ORDER BY r.created_at DESC
        """,
        _hm_scope_params(uid),
    )
    ttf_target = sla_cfg.get("req_time_to_fill", 45)
    my_reqs = []
    for r in (req_rows or []):
        open_days = float(r["open_days"] or 0)
        rag = compute_rag(open_days, ttf_target) if r["status"] == "open" else None
        my_reqs.append({
            "id":              str(r["id"]),
            "title":           r["title"],
            "status":          r["status"],
            "req_code":        r["req_code"],
            "approval_status": r["approval_status"],
            "openings":        int(r["openings"] or 1),
            "open_days":       round(open_days, 1),
            "rag":             rag,
            "stage_counts": {
                "applied":       int(r["cnt_applied"] or 0),
                "screen":        int(r["cnt_screen"] or 0),
                "nexai_bot":     int(r["cnt_nexai_bot"] or 0),
                "shortlisted":   int(r["cnt_shortlisted"] or 0),
                "interview":     int(r["cnt_interview"] or 0),
                "documentation": int(r["cnt_documentation"] or 0),
                "offered":       int(r["cnt_offered"] or 0),
                "hired":         int(r["cnt_hired"] or 0),
            },
        })

    # ── 6. KPI strip ─────────────────────────────────────────────────────────
    kpi_open = query_one(
        f"""SELECT COUNT(DISTINCT r.id) AS n
           FROM requisition r
           WHERE {_HM_REQ_SCOPE_SQL}
             AND r.status = 'open'
             AND COALESCE(r.approval_status, 'approved') = 'approved'""",
        _hm_scope_params(uid),
    )
    ph = ", ".join(["%s"] * len(_TERMINAL_TUPLE))
    kpi_pipeline = query_one(
        f"""SELECT COUNT(DISTINCT a.id) AS n
            FROM application a
            JOIN requisition r ON r.id = a.requisition_id
            WHERE {_HM_REQ_SCOPE_SQL}
              AND a.status NOT IN ({ph})
              AND COALESCE(r.approval_status, 'approved') = 'approved'""",
        _hm_scope_params(uid) + list(_TERMINAL_TUPLE),
    )
    avg_fb_row = query_one(
        """SELECT ROUND(AVG(
               EXTRACT(EPOCH FROM (now() - i.scheduled_at)) / 86400.0
           )::numeric, 1) AS avg_days
           FROM interview i
           JOIN application a  ON a.id = i.application_id
           JOIN interview_panel ip
                ON ip.interview_id = i.id AND ip.interviewer_id = %s
           LEFT JOIN scorecard s
                ON s.interview_id = i.id AND s.interviewer_id = %s
           WHERE (s.id IS NULL OR s.status = 'draft')""",
        [uid, uid],
    )
    breach_rows = query(
        f"""SELECT a.status,
                EXTRACT(EPOCH FROM (now() - COALESCE(
                    (SELECT se.occurred_at FROM stage_event se
                     WHERE se.application_id = a.id AND se.to_status = a.status
                     ORDER BY se.occurred_at DESC LIMIT 1),
                    a.applied_at
                ))) / 86400.0 AS elapsed_days
            FROM application a
            JOIN requisition r ON r.id = a.requisition_id
            WHERE {_HM_REQ_SCOPE_SQL}
              AND a.status NOT IN ({ph})""",
        _hm_scope_params(uid) + list(_TERMINAL_TUPLE),
    )
    red_count = amber_count = 0
    for row in (breach_rows or []):
        sla_key = STAGE_SLA_KEY.get(row["status"], "stage_default")
        target  = sla_cfg.get(sla_key, sla_cfg.get("stage_default", 5))
        rag     = compute_rag(row["elapsed_days"], target)
        if   rag["status"] == "red":   red_count   += 1
        elif rag["status"] == "amber": amber_count += 1

    kpi_strip = {
        "open_reqs":               int(kpi_open["n"])     if kpi_open     else 0,
        "candidates_in_pipeline":  int(kpi_pipeline["n"]) if kpi_pipeline else 0,
        "avg_days_pending_feedback": (
            float(avg_fb_row["avg_days"])
            if avg_fb_row and avg_fb_row["avg_days"] is not None else None
        ),
        "sla_breaches_red":   red_count,
        "sla_breaches_amber": amber_count,
    }

    return {
        "action_queue": {
            "pending_scorecards":    pending_scorecards,
            "pending_approvals":     pending_approvals,
            "awaiting_decision":     awaiting_decision,
            "pending_availability":  pending_availability,
        },
        "my_reqs":   my_reqs,
        "kpi_strip": kpi_strip,
    }


# ── GET /api/hm/ta-pending-count (TA manager: badge count) ───────────────────

@router.get("/hm/ta-pending-count")
def ta_pending_count(user: dict = Depends(get_current_user)):
    _require_ta(user)
    row = query_one(
        "SELECT COUNT(*) AS n FROM requisition "
        "WHERE COALESCE(approval_status, 'approved') = 'pending_ta_approval'",
        [],
    )
    return {"count": int(row["n"]) if row else 0}


# ── GET /api/hm/ta-pending-reqs (TA manager: approval list) ──────────────────

@router.get("/hm/ta-pending-reqs")
def ta_pending_reqs(user: dict = Depends(get_current_user)):
    _require_ta(user)
    rows = query(
        """
        SELECT r.id, r.title, r.req_code, r.created_at, r.openings,
               COALESCE(r.created_by_role, '') AS created_by_role,
               r.job_description,
               r.min_experience, r.max_experience,
               r.key_skills,
               b.code  AS band,
               bu.name AS business_unit,
               creator.full_name AS created_by_name,
               creator.email    AS created_by_email,
               hm.full_name     AS hm_name,
               hm.email         AS hm_email
        FROM requisition r
        JOIN band b          ON b.id  = r.band_id
        JOIN business_unit bu ON bu.id = r.bu_id
        LEFT JOIN app_user creator ON creator.id = r.created_by
        LEFT JOIN app_user hm      ON hm.id      = r.hiring_manager_id
        WHERE COALESCE(r.approval_status, 'approved') = 'pending_ta_approval'
        ORDER BY r.created_at DESC
        """,
        [],
    )
    return rows or []


# ── POST /api/requisitions/{req_id}/ta-approve ────────────────────────────────

@router.post("/requisitions/{req_id}/ta-approve")
def ta_approve_requisition(req_id: str, user: dict = Depends(get_current_user)):
    _require_ta(user)

    req = query_one(
        "SELECT id, title, approval_status, hiring_manager_id FROM requisition WHERE id=%s",
        [req_id],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    current = req.get("approval_status") or "approved"
    if current != "pending_ta_approval":
        raise HTTPException(
            400, f"Requisition is not pending TA approval (current: {current!r})"
        )

    query(
        "UPDATE requisition SET approval_status='approved', status='open' WHERE id=%s",
        [req_id], fetch=False,
    )

    hm_notified = False
    if req.get("hiring_manager_id"):
        hm_email, hm_name = _user_email_name(str(req["hiring_manager_id"]))
        if hm_email:
            hm_notified = _send_safe("hm_req_approved", {
                "hm_name":   hm_name,
                "req_title": req["title"],
            }, [hm_email], req_id=req_id, actor=user)
        log_activity(
            "requisition", "requisition_approval_notice_sent",
            entity_id=req_id, requisition_id=req_id,
            actor_id=user["sub"], actor_role=user["role"],
            detail={"decision": "approved", "hm_user_id": str(req["hiring_manager_id"]), "notified": hm_notified},
        )

    return {"ok": True, "approval_status": "approved", "hm_notified": hm_notified}


# ── POST /api/requisitions/{req_id}/ta-reject ─────────────────────────────────

class TARejectIn(BaseModel):
    reason: str


@router.post("/requisitions/{req_id}/ta-reject")
def ta_reject_requisition(
    req_id: str,
    body: TARejectIn,
    user: dict = Depends(get_current_user),
):
    _require_ta(user)

    req = query_one(
        "SELECT id, title, approval_status, hiring_manager_id FROM requisition WHERE id=%s",
        [req_id],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    current = req.get("approval_status") or "approved"
    if current != "pending_ta_approval":
        raise HTTPException(
            400, f"Requisition is not pending TA approval (current: {current!r})"
        )

    query(
        "UPDATE requisition SET approval_status='rejected', status='closed', rejection_reason=%s WHERE id=%s",
        [body.reason, req_id], fetch=False,
    )

    hm_notified = False
    if req.get("hiring_manager_id"):
        hm_email, hm_name = _user_email_name(str(req["hiring_manager_id"]))
        if hm_email:
            hm_notified = _send_safe("hm_req_rejected", {
                "hm_name":   hm_name,
                "req_title": req["title"],
                "reason":    body.reason,
            }, [hm_email], req_id=req_id, actor=user)
        log_activity(
            "requisition", "requisition_approval_notice_sent",
            entity_id=req_id, requisition_id=req_id,
            actor_id=user["sub"], actor_role=user["role"],
            detail={"decision": "rejected", "hm_user_id": str(req["hiring_manager_id"]), "notified": hm_notified},
        )

    return {"ok": True, "approval_status": "rejected", "hm_notified": hm_notified}


@router.post("/requisitions/{req_id}/resend-hm-notice")
def resend_hm_decision_notice(req_id: str, user: dict = Depends(get_current_user)):
    """Re-sends the TA approve/reject decision email to the requisition's HM --
    for when that notification silently failed the first time."""
    _require_ta(user)
    req = query_one(
        "SELECT id, title, approval_status, hiring_manager_id, rejection_reason FROM requisition WHERE id=%s",
        [req_id],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")
    if req["approval_status"] not in ("approved", "rejected"):
        raise HTTPException(400, f"Requisition has no TA decision to resend (status: {req['approval_status']!r})")
    if not req.get("hiring_manager_id"):
        raise HTTPException(400, "No hiring manager assigned to this requisition")

    hm_email, hm_name = _user_email_name(str(req["hiring_manager_id"]))
    hm_notified = False
    if hm_email:
        if req["approval_status"] == "approved":
            hm_notified = _send_safe("hm_req_approved", {
                "hm_name": hm_name, "req_title": req["title"],
            }, [hm_email], req_id=req_id, actor=user)
        else:
            # Migration 61 persists the reason now, so a resend can reproduce
            # the exact original wording instead of a generic fallback.
            hm_notified = _send_safe("hm_req_rejected", {
                "hm_name": hm_name, "req_title": req["title"],
                "reason": req.get("rejection_reason") or "See your Talent Acquisition contact for the original reason.",
            }, [hm_email], req_id=req_id, actor=user)
    log_activity(
        "requisition", "requisition_approval_reminder_sent",
        entity_id=req_id, requisition_id=req_id,
        actor_id=user["sub"], actor_role=user["role"],
        detail={"decision": req["approval_status"], "hm_user_id": str(req["hiring_manager_id"]), "notified": hm_notified},
    )
    return {"ok": True, "hm_notified": hm_notified}
