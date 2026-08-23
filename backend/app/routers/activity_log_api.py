"""
Activity timeline reports — read-only drill-down into everything that has
happened to one candidate/application or one requisition: pipeline stage
moves (stage_event), offer approval steps (offer_approval_step), and every
other backend-timestamped action (activity_log) that those two tables don't
already cover (requisition lifecycle, screening pass/hold, interview
scheduling, NexAI, offers, campus, vendor, module-access).

Deliberately NOT folded into custom_reports_api.py's EXPLORES catalog: this
is a fixed-shape per-entity chronological UNION, not a dimension/measure
aggregation. RBAC here is intentionally narrower than Custom Reports
(no hiring_manager) — the user's explicit ask was ta_manager/recruiter/admin
only. Timestamps are never pushed live; these endpoints are only hit when a
report screen is opened on demand.
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services import excel_export
from .nexai_api import _recruiter_owns_req, _application_req_id

router = APIRouter(prefix="/api/activity-log", tags=["activity_log"])

_ALLOWED_ROLES = ("ta_manager", "recruiter", "admin")


def _check_role(user: dict):
    if user["role"] not in _ALLOWED_ROLES:
        raise HTTPException(403, "Not authorized for Activity Timeline")


# ── Human-readable "Detail" column ───────────────────────────────────────────
# activity_log.detail is a free-form JSONB blob every call site fills in
# however's convenient (event ids, links, recipient addresses, flags) — great
# for debugging, unreadable as raw `{"event_id": "...", "meet_link": "..."}`
# in a report a recruiter/TA manager is reading. stage_event/offer_approval_step
# rows carry a plain-text note instead (not JSON), so those pass through
# untouched below.

_LABEL_OVERRIDES = {
    "id": "ID", "hm": "HM", "ta": "TA", "url": "URL", "utc": "UTC",
    "cv": "CV", "jd": "JD", "ai": "AI", "sla": "SLA",
}


def _label_from_key(key: str) -> str:
    return " ".join(_LABEL_OVERRIDES.get(w.lower(), w.capitalize()) for w in key.split("_"))


def _humanize_value(v):
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if v is None or v == "":
        return "—"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) if v else "—"
    return str(v)


def _humanize_detail(raw) -> str:
    if not raw:
        return ""
    text = raw.strip() if isinstance(raw, str) else raw
    if not isinstance(text, str) or not (text.startswith("{") or text.startswith("[")):
        return text or ""  # plain-text note (stage_event/offer_approval_step) — leave as-is
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(data, dict):
        if not data:
            return ""
        return " · ".join(f"{_label_from_key(k)}: {_humanize_value(v)}" for k, v in data.items())
    if isinstance(data, list):
        return ", ".join(str(x) for x in data) if data else ""
    return str(data)


def _humanize_rows(rows: list) -> list:
    for r in rows:
        r["detail_text"] = _humanize_detail(r.get("detail_text"))
    return rows


_APPLICATION_TIMELINE_SQL = """
SELECT se.occurred_at AS occurred_at, 'stage_event' AS source, se.to_status AS action,
       COALESCE(au.full_name, 'System') AS actor_name,
       se.note AS detail_text, se.from_status AS from_value, se.to_status AS to_value,
       se.application_id AS application_id
FROM stage_event se
LEFT JOIN app_user au ON au.id = se.actor_id
WHERE se.application_id = %(app_id)s

UNION ALL

SELECT al.occurred_at, 'activity_log', al.action,
       COALESCE(au2.full_name, al.actor_label, 'System'),
       al.detail::text, al.from_value, al.to_value,
       al.application_id
FROM activity_log al
LEFT JOIN app_user au2 ON au2.id = al.actor_id
WHERE al.application_id = %(app_id)s

UNION ALL

SELECT oas.acted_at, 'offer_approval_step', oas.status,
       COALESCE(au3.full_name, 'System'),
       oas.notes, NULL, oas.status,
       o.application_id
FROM offer_approval_step oas
JOIN offer o ON o.id = oas.offer_id
LEFT JOIN app_user au3 ON au3.id = oas.approver_id
WHERE o.application_id = %(app_id)s AND oas.acted_at IS NOT NULL

ORDER BY occurred_at
"""


def _application_timeline_rows(app_id: str) -> list:
    return _humanize_rows(query(_APPLICATION_TIMELINE_SQL, {"app_id": app_id}) or [])


_REQUISITION_TIMELINE_SQL = """
WITH app_ids AS (
    SELECT id FROM application WHERE requisition_id = %(req_id)s
)
SELECT se.occurred_at AS occurred_at, 'stage_event' AS source, se.to_status AS action,
       COALESCE(au.full_name, 'System') AS actor_name,
       se.note AS detail_text, se.from_status AS from_value, se.to_status AS to_value,
       se.application_id AS application_id
FROM stage_event se
LEFT JOIN app_user au ON au.id = se.actor_id
WHERE se.application_id IN (SELECT id FROM app_ids)

UNION ALL

SELECT al.occurred_at, 'activity_log', al.action,
       COALESCE(au2.full_name, al.actor_label, 'System'),
       al.detail::text, al.from_value, al.to_value,
       al.application_id
FROM activity_log al
LEFT JOIN app_user au2 ON au2.id = al.actor_id
WHERE al.requisition_id = %(req_id)s OR al.application_id IN (SELECT id FROM app_ids)

UNION ALL

SELECT oas.acted_at, 'offer_approval_step', oas.status,
       COALESCE(au3.full_name, 'System'),
       oas.notes, NULL, oas.status,
       o.application_id
FROM offer_approval_step oas
JOIN offer o ON o.id = oas.offer_id
LEFT JOIN app_user au3 ON au3.id = oas.approver_id
WHERE o.application_id IN (SELECT id FROM app_ids) AND oas.acted_at IS NOT NULL

ORDER BY occurred_at
"""


def _requisition_timeline_rows(req_id: str) -> list:
    rows = _humanize_rows(query(_REQUISITION_TIMELINE_SQL, {"req_id": req_id}) or [])
    app_ids = {str(r["application_id"]) for r in rows if r.get("application_id")}
    candidate_names = {}
    if app_ids:
        cand_rows = query(
            "SELECT a.id AS application_id, c.full_name AS candidate_name "
            "FROM application a JOIN candidate c ON c.id = a.candidate_id "
            "WHERE a.id = ANY(%s::uuid[])",
            [list(app_ids)],
        ) or []
        candidate_names = {str(r["application_id"]): r["candidate_name"] for r in cand_rows}
    for r in rows:
        r["candidate_name"] = candidate_names.get(str(r["application_id"])) if r.get("application_id") else None
    return rows


def _columns(include_candidate: bool) -> list:
    cols = [
        {"key": "occurred_at", "label": "When", "type": "date"},
        {"key": "source", "label": "Source", "type": "text"},
        {"key": "action", "label": "Action", "type": "text"},
        {"key": "actor_name", "label": "Actor", "type": "text"},
        {"key": "from_value", "label": "From", "type": "text"},
        {"key": "to_value", "label": "To", "type": "text"},
        {"key": "detail_text", "label": "Detail", "type": "text"},
    ]
    if include_candidate:
        cols.insert(1, {"key": "candidate_name", "label": "Candidate", "type": "text"})
    return cols


@router.get("/application/{app_id}")
def application_timeline(app_id: str, user: dict = Depends(get_current_user)):
    _check_role(user)
    if not query_one("SELECT id FROM application WHERE id = %s", [app_id]):
        raise HTTPException(404, "Application not found")
    if user["role"] == "recruiter":
        req_id = _application_req_id(app_id)
        if not req_id or not _recruiter_owns_req(user, req_id):
            raise HTTPException(404, "Application not found")
    rows = _application_timeline_rows(app_id)
    return {"columns": _columns(include_candidate=False), "rows": rows, "row_count": len(rows)}


@router.get("/requisition/{req_id}")
def requisition_timeline(req_id: str, user: dict = Depends(get_current_user)):
    _check_role(user)
    if not query_one("SELECT id FROM requisition WHERE id = %s", [req_id]):
        raise HTTPException(404, "Requisition not found")
    if user["role"] == "recruiter" and not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Requisition not found")
    rows = _requisition_timeline_rows(req_id)
    return {"columns": _columns(include_candidate=True), "rows": rows, "row_count": len(rows)}


def _stream_timeline_excel(title: str, filename: str, rows: list, columns: list, generated_by: str):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    excel_export.sheet_from_rows(wb, "Timeline", rows, columns=[(c["label"], c["key"]) for c in columns])
    excel_export.build_summary_sheet(
        wb, title=title, generated_by=generated_by, generated_at=datetime.now(),
        filters_applied=[], rows=rows, measures_meta=[],
    )
    # filename must stay ASCII (Content-Disposition can't hold a title's em-dash etc.)
    return excel_export.stream_workbook(wb, filename)


@router.get("/application/{app_id}/excel")
def application_timeline_excel(app_id: str, user: dict = Depends(get_current_user)):
    _check_role(user)
    if not query_one("SELECT id FROM application WHERE id = %s", [app_id]):
        raise HTTPException(404, "Application not found")
    if user["role"] == "recruiter":
        req_id = _application_req_id(app_id)
        if not req_id or not _recruiter_owns_req(user, req_id):
            raise HTTPException(404, "Application not found")
    rows = _application_timeline_rows(app_id)
    return _stream_timeline_excel(
        f"Activity Timeline - Application {app_id}", f"activity_timeline_application_{app_id}.xlsx",
        rows, _columns(include_candidate=False),
        user.get("name") or user.get("email") or "",
    )


@router.get("/requisition/{req_id}/excel")
def requisition_timeline_excel(req_id: str, user: dict = Depends(get_current_user)):
    _check_role(user)
    if not query_one("SELECT id FROM requisition WHERE id = %s", [req_id]):
        raise HTTPException(404, "Requisition not found")
    if user["role"] == "recruiter" and not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Requisition not found")
    rows = _requisition_timeline_rows(req_id)
    return _stream_timeline_excel(
        f"Activity Timeline - Requisition {req_id}", f"activity_timeline_requisition_{req_id}.xlsx",
        rows, _columns(include_candidate=True),
        user.get("name") or user.get("email") or "",
    )


@router.get("/logins")
def login_history(
    user: dict = Depends(get_current_user),
    user_id: str = Query(None, description="Filter to one app_user id"),
):
    """Admin-only — login_log has no candidate/requisition to anchor a
    timeline to, so it's a separate small report rather than folded into
    the UNION above."""
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    where = "WHERE ll.user_id = %s" if user_id else ""
    params = [user_id] if user_id else []
    rows = query(
        f"""SELECT ll.logged_at, au.full_name AS actor_name, au.email, ll.user_role, ll.ip_address
            FROM login_log ll
            LEFT JOIN app_user au ON au.id = ll.user_id
            {where}
            ORDER BY ll.logged_at DESC
            LIMIT 500""",
        params,
    ) or []
    return {"rows": rows, "row_count": len(rows)}
