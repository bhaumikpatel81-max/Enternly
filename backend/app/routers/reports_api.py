"""
Reports API — 8 management pivots for TA Manager, scoped variants for Recruiter
and Hiring Manager, plus openpyxl-based Excel download.
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services.period import period_start as _period_start
from ..services import excel_export

router = APIRouter(prefix="/api/reports2", tags=["reports2"])


def _recruiter_join(role: str, uid: str) -> tuple:
    """Returns (sql_join_fragment, extra_params_list)."""
    if role == "recruiter":
        return (
            "JOIN requisition_recruiter rr ON rr.requisition_id = r.id AND rr.recruiter_id = %s",
            [uid],
        )
    return ("", [])


# ── Pivot 1: Net Open Positions vs Total Demand ───────────────────────────────

def _pivot1(year, ps: date, rjoin: str, rp: list, xwhere: str = "", xp: list | None = None):
    sql = f"""
        SELECT gc.name AS company, r.roll_type,
               COUNT(r.id) AS total_reqs,
               SUM(r.openings) AS total_openings,
               COUNT(r.id) FILTER (WHERE r.status = 'open') AS open_count
        FROM requisition r
        JOIN business_unit bu ON bu.id = r.bu_id
        JOIN group_company gc ON gc.id = bu.company_id
        {rjoin}
        WHERE COALESCE(r.opened_at, r.created_at) >= %s {xwhere}
        GROUP BY gc.name, r.roll_type
        ORDER BY gc.name, r.roll_type
    """
    return query(sql, rp + [ps] + (xp or []))


# ── Pivot 2: Diversity Hiring YTD ─────────────────────────────────────────────

def _pivot2(year, ps: date, rjoin: str, rp: list, xwhere: str = "", xp: list | None = None):
    sql = f"""
        SELECT gc.name AS company, c.gender, COUNT(*) AS n
        FROM application a
        JOIN candidate   c  ON c.id  = a.candidate_id
        JOIN requisition r  ON r.id  = a.requisition_id
        JOIN business_unit bu ON bu.id = r.bu_id
        JOIN group_company gc ON gc.id = bu.company_id
        {rjoin}
        WHERE a.applied_at >= %s
          AND a.status IN ('hired','documentation','offered')
          {xwhere}
        GROUP BY gc.name, c.gender
        ORDER BY gc.name, c.gender
    """
    return query(sql, rp + [ps] + (xp or []))


# ── Pivot 3: Status of Open Positions by Entity & Band ────────────────────────

def _pivot3(year, ps: date, rjoin: str, rp: list, xwhere: str = "", xp: list | None = None):
    sql = f"""
        SELECT gc.name AS company, b.code AS band, r.status, COUNT(r.id) AS n
        FROM requisition r
        JOIN business_unit bu ON bu.id = r.bu_id
        JOIN group_company gc ON gc.id = bu.company_id
        JOIN band b ON b.id = r.band_id
        {rjoin}
        WHERE COALESCE(r.opened_at, r.created_at) >= %s {xwhere}
        GROUP BY gc.name, b.code, r.status
        ORDER BY gc.name, b.code
    """
    return query(sql, rp + [ps] + (xp or []))


# ── Pivot 4: Status by Hiring Stage (funnel) ─────────────────────────────────

def _pivot4(year, ps: date, rjoin: str, rp: list, xwhere: str = "", xp: list | None = None):
    sql = f"""
        SELECT
          CASE
            WHEN a.status = 'applied'  THEN 'Sourcing'
            WHEN a.status IN ('screen','nexai_bot','shortlisted') THEN 'Screening'
            WHEN a.status = 'interview' THEN 'Interview'
            WHEN a.status IN ('documentation','offered','hired') THEN 'Selected'
            ELSE 'Other'
          END AS stage,
          COUNT(*) AS n
        FROM application a
        JOIN requisition r ON r.id = a.requisition_id
        {rjoin}
        WHERE a.applied_at >= %s {xwhere}
        GROUP BY stage
        ORDER BY n DESC
    """
    return query(sql, rp + [ps] + (xp or []))


# ── Pivot 5: Internal Movement ────────────────────────────────────────────────

def _pivot5(year, ps: date, rjoin: str, rp: list, xwhere: str = "", xp: list | None = None):
    sql = f"""
        SELECT gc.name AS company,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE a.is_internal_movement) AS internal_count
        FROM application a
        JOIN requisition r ON r.id = a.requisition_id
        JOIN business_unit bu ON bu.id = r.bu_id
        JOIN group_company gc ON gc.id = bu.company_id
        {rjoin}
        WHERE a.applied_at >= %s {xwhere}
        GROUP BY gc.name
        ORDER BY gc.name
    """
    return query(sql, rp + [ps] + (xp or []))


# ── Pivot 6: Aging of Open Positions ─────────────────────────────────────────

def _pivot6(year, ps: date, role: str, uid: str, xwhere: str = "", xp: list | None = None):
    if role == "recruiter":
        extra = "AND va.id IN (SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s)"
        params = [uid]
    else:
        extra = ""
        params = []
    sql = f"""
        SELECT va.aging_bracket AS aging_bracket, COUNT(*) AS n,
               ROUND(AVG(va.aging_days)::numeric, 0) AS avg_days
        FROM v_requisition_aging va
        JOIN requisition r ON r.id = va.id
        WHERE 1=1 {extra} {xwhere}
        GROUP BY va.aging_bracket
        ORDER BY MIN(va.aging_days)
    """
    return query(sql, params + (xp or []))


# ── Pivot 7: Recruiter Productivity ──────────────────────────────────────────

def _pivot7(year, ps: date, rjoin2: str, rp2: list, xwhere: str = "", xp: list | None = None):
    """rjoin2/rp2 are for recruiter-scope on requisition_recruiter table."""
    # SQL param order: ps (for applied_at) comes first, then rp2 (recruiter_id if scoped), then xp
    where_extra = "AND rr.recruiter_id = %s" if rjoin2 else ""
    sql = f"""
        SELECT u.full_name AS recruiter,
               COUNT(DISTINCT a.id) AS total_handled,
               COUNT(DISTINCT a.id) FILTER (WHERE a.status IN ('documentation','offered','hired')) AS converted
        FROM app_user u
        JOIN requisition_recruiter rr ON rr.recruiter_id = u.id
        JOIN requisition r ON r.id = rr.requisition_id
        LEFT JOIN application a ON a.requisition_id = r.id AND a.applied_at >= %s
        WHERE u.role IN ('recruiter','ta_manager')
          {where_extra} {xwhere}
        GROUP BY u.full_name
        ORDER BY converted DESC
    """
    return query(sql, [ps] + rp2 + (xp or []))


# ── Pivot 8: Total Joined/Offered/Selected ────────────────────────────────────

def _pivot8(year, ps: date, rjoin: str, rp: list, xwhere: str = "", xp: list | None = None):
    sql = f"""
        SELECT
          COUNT(*) FILTER (WHERE a.status = 'documentation') AS selected,
          COUNT(*) FILTER (WHERE a.status = 'offered') AS offered,
          COUNT(*) FILTER (WHERE a.status = 'hired') AS joined
        FROM application a
        JOIN requisition r ON r.id = a.requisition_id
        {rjoin}
        WHERE a.applied_at >= %s {xwhere}
    """
    rows = query(sql, rp + [ps] + (xp or []))
    return rows[0] if rows else {"selected": 0, "offered": 0, "joined": 0}


# ── TA pivot endpoint ─────────────────────────────────────────────────────────

PIVOT_MAP = {
    "1": ("Net Open Positions", _pivot1),
    "2": ("Diversity Hiring YTD", _pivot2),
    "3": ("Status by Entity & Band", _pivot3),
    "4": ("Status by Hiring Stage", _pivot4),
    "5": ("Internal Movement", _pivot5),
    "6": ("Aging of Open Positions", _pivot6),
    "7": ("Recruiter Productivity", _pivot7),
    "8": ("Total Joined / Offered / Selected", _pivot8),
}


@router.get("/ta/pivot/{pivot_id}")
def ta_pivot(
    pivot_id: str,
    period: str = Query("yearly"),
    year: int = Query(default_factory=lambda: date.today().year),
    user: dict = Depends(get_current_user),
):
    if user["role"] not in ("ta_manager", "admin"):
        raise HTTPException(403, "TA Manager or Admin only")
    if pivot_id not in PIVOT_MAP:
        raise HTTPException(404, f"pivot_id must be 1-8, got {pivot_id!r}")
    ps = _period_start(period, year)
    rjoin, rp = "", []
    xwhere, xp = "AND r.tenant_id = %s", [user.get("tenant_id")]
    if pivot_id == "6":
        return _pivot6(year, ps, "ta_manager", "", xwhere, xp)
    if pivot_id == "7":
        return _pivot7(year, ps, "", [], xwhere, xp)
    if pivot_id == "8":
        return _pivot8(year, ps, rjoin, rp, xwhere, xp)
    return PIVOT_MAP[pivot_id][1](year, ps, rjoin, rp, xwhere, xp)


# ── Recruiter pivot endpoint ──────────────────────────────────────────────────

@router.get("/recruiter/pivot/{pivot_id}")
def recruiter_pivot(
    pivot_id: str,
    period: str = Query("yearly"),
    year: int = Query(default_factory=lambda: date.today().year),
    user: dict = Depends(get_current_user),
):
    # A ta_manager can also personally own/work requisitions as an individual
    # contributor -- "My Reports" scopes to whoever is asking (uid below), so
    # letting them in here just gives them their own numbers, same as a recruiter.
    if user["role"] not in ("recruiter", "ta_manager"):
        raise HTTPException(403, "Recruiter or TA Manager only")
    if pivot_id not in PIVOT_MAP:
        raise HTTPException(404, f"pivot_id must be 1-8")
    uid = user["sub"]
    ps = _period_start(period, year)
    rjoin, rp = _recruiter_join("recruiter", uid)
    xwhere, xp = "AND r.tenant_id = %s", [user.get("tenant_id")]
    if pivot_id == "6":
        return _pivot6(year, ps, "recruiter", uid, xwhere, xp)
    if pivot_id == "7":
        return _pivot7(year, ps, "recruiter", [uid], xwhere, xp)
    if pivot_id == "8":
        return _pivot8(year, ps, rjoin, rp, xwhere, xp)
    return PIVOT_MAP[pivot_id][1](year, ps, rjoin, rp, xwhere, xp)


# ── HM summary ───────────────────────────────────────────────────────────────

@router.get("/hm/summary")
def hm_summary(user: dict = Depends(get_current_user)):
    uid = user["sub"]
    taken = query_one(
        """SELECT COUNT(*) AS n FROM interview i
           JOIN interview_panel ip ON ip.interview_id = i.id
           WHERE ip.interviewer_id = %s AND i.status = 'completed'""",
        [uid],
    )
    outcomes = query(
        """SELECT COALESCE(sc.verdict,'no_verdict') AS verdict, COUNT(*) AS n
           FROM scorecard sc
           JOIN interview i ON i.id = sc.interview_id
           JOIN interview_panel ip ON ip.interview_id = i.id
           WHERE ip.interviewer_id = %s
           GROUP BY sc.verdict""",
        [uid],
    )
    turnaround = query_one(
        """SELECT ROUND(AVG(
               EXTRACT(EPOCH FROM (sc.submitted_at - i.scheduled_at)) / 3600.0
           )::numeric, 1) AS avg_hours
           FROM scorecard sc
           JOIN interview i ON i.id = sc.interview_id
           JOIN interview_panel ip ON ip.interview_id = i.id
           WHERE ip.interviewer_id = %s
             AND i.status = 'completed'
             AND sc.submitted_at IS NOT NULL""",
        [uid],
    )
    pending = query_one(
        """SELECT COUNT(*) AS n FROM application a
           JOIN requisition r ON r.id = a.requisition_id
           WHERE r.hiring_manager_id = %s
             AND a.status IN ('documentation','interview')
             AND (a.hm_feedback IS NULL OR a.hm_feedback = '')""",
        [uid],
    )
    return {
        "interviews_taken": int(taken["n"]) if taken else 0,
        "outcomes": {o["verdict"]: int(o["n"]) for o in outcomes},
        "avg_feedback_turnaround_hours": (
            float(turnaround["avg_hours"]) if turnaround and turnaround["avg_hours"] else None
        ),
        "pending_reviews": int(pending["n"]) if pending else 0,
    }


@router.get("/hm/excel")
def hm_excel(user: dict = Depends(get_current_user)):
    if user["role"] != "hiring_manager":
        raise HTTPException(403, "Hiring Manager only")
    data = hm_summary(user=user)
    rows = [{"Verdict": k, "Count": v} for k, v in data["outcomes"].items()]

    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    excel_export.sheet_from_rows(
        wb, "Interview Metrics",
        [
            {"Metric": "Interviews Taken", "Value": data["interviews_taken"]},
            {"Metric": "Avg Feedback Turnaround (hrs)", "Value": data["avg_feedback_turnaround_hours"] or "N/A"},
            {"Metric": "Pending Reviews", "Value": data["pending_reviews"]},
        ],
    )
    excel_export.sheet_from_rows(wb, "Interview Outcomes", rows)
    excel_export.build_summary_sheet(
        wb,
        title="My Interview Reports",
        generated_by=user.get("name") or user.get("email") or "",
        generated_at=datetime.now(),
        filters_applied=[],
        rows=rows,
        measures_meta=[{"key": "Count", "label": "Count"}],
    )
    return excel_export.stream_workbook(wb, "enternly_hm_report.xlsx")


# ── Excel export helpers ──────────────────────────────────────────────────────

_PIVOT_LABELS = [
    "Net Open Positions", "Diversity YTD", "Status Entity Band",
    "Status by Stage", "Internal Movement", "Aging",
    "Recruiter Productivity", "Joined Offered Selected",
]


def _build_workbook(all_pivots: list, *, title: str, generated_by: str):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # remove default sheet
    for label, rows in zip(_PIVOT_LABELS, all_pivots):
        data = rows if isinstance(rows, list) else [rows]
        excel_export.sheet_from_rows(wb, label, data)
    # "Status by Stage" (pivot 4) is the most natural funnel-shaped sheet to summarize
    summary_rows = all_pivots[3] if isinstance(all_pivots[3], list) else [all_pivots[3]]
    excel_export.build_summary_sheet(
        wb,
        title=title,
        generated_by=generated_by,
        generated_at=datetime.now(),
        filters_applied=[],
        rows=summary_rows,
        measures_meta=[{"key": "n", "label": "Applications"}],
    )
    return wb


# ── TA Excel download ─────────────────────────────────────────────────────────

@router.get("/ta/excel")
def ta_excel(
    period: str = Query("yearly"),
    year: int = Query(default_factory=lambda: date.today().year),
    user: dict = Depends(get_current_user),
):
    if user["role"] not in ("ta_manager", "admin"):
        raise HTTPException(403, "TA Manager or Admin only")
    ps = _period_start(period, year)
    xwhere, xp = "AND r.tenant_id = %s", [user.get("tenant_id")]
    pivots = [
        _pivot1(year, ps, "", [], xwhere, xp),
        _pivot2(year, ps, "", [], xwhere, xp),
        _pivot3(year, ps, "", [], xwhere, xp),
        _pivot4(year, ps, "", [], xwhere, xp),
        _pivot5(year, ps, "", [], xwhere, xp),
        _pivot6(year, ps, "ta_manager", "", xwhere, xp),
        _pivot7(year, ps, "", [], xwhere, xp),
        [_pivot8(year, ps, "", [], xwhere, xp)],
    ]
    wb = _build_workbook(pivots, title=f"TA Reports — {period.title()} {year}", generated_by=user.get("name") or user.get("email") or "")
    return excel_export.stream_workbook(wb, f"enternly_ta_report_{year}_{period}.xlsx")


# ── Recruiter Excel download ──────────────────────────────────────────────────

@router.get("/recruiter/excel")
def recruiter_excel(
    period: str = Query("yearly"),
    year: int = Query(default_factory=lambda: date.today().year),
    user: dict = Depends(get_current_user),
):
    if user["role"] not in ("recruiter", "ta_manager"):
        raise HTTPException(403, "Recruiter or TA Manager only")
    uid = user["sub"]
    ps = _period_start(period, year)
    rjoin, rp = _recruiter_join("recruiter", uid)
    xwhere, xp = "AND r.tenant_id = %s", [user.get("tenant_id")]
    pivots = [
        _pivot1(year, ps, rjoin, rp, xwhere, xp),
        _pivot2(year, ps, rjoin, rp, xwhere, xp),
        _pivot3(year, ps, rjoin, rp, xwhere, xp),
        _pivot4(year, ps, rjoin, rp, xwhere, xp),
        _pivot5(year, ps, rjoin, rp, xwhere, xp),
        _pivot6(year, ps, "recruiter", uid, xwhere, xp),
        _pivot7(year, ps, "recruiter", [uid], xwhere, xp),
        [_pivot8(year, ps, rjoin, rp, xwhere, xp)],
    ]
    wb = _build_workbook(pivots, title=f"My Reports — {period.title()} {year}", generated_by=user.get("name") or user.get("email") or "")
    return excel_export.stream_workbook(wb, f"enternly_my_report_{year}_{period}.xlsx")
