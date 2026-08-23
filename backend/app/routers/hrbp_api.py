"""
HRBP-facing API (Enternly Batch 1).

Two purposes:
  1. Lookup endpoints used by the requisition create/edit modal (any
     authenticated staff role) to auto-fill / manually pick a requisition's
     HRBP from its business unit.
  2. The 'hrbp' role's own read-only surface: their requisitions and, per
     requisition, candidate name + stage + SLA colour only -- no scores,
     no advance/edit. Visibility is scoped by scope_requisitions_for_hrbp()
     (email primary, bu_id fallback, combined with OR) so the rule lives
     in exactly one place.
"""
import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_utils import get_current_user
from ..db import query, query_one
from ..services import excel_export
from ..services.period import period_start as _period_start
from ..services.sla import bulk_application_rag, PIPELINE_STAGES, PIPELINE_STAGE_LABELS, derive_pending_from
from . import reports_api as _rp

router = APIRouter(prefix="/api/hrbp", tags=["hrbp"])


def scope_requisitions_for_hrbp(user: dict):
    """WHERE fragment (aliased to `r`) + params scoping requisitions to an
    HRBP's own BU/company/email. Email is primary, home BUs and home
    companies are fallbacks, all combined with OR -- reused by every
    HRBP-scoped query so the rule lives once.

    All are re-read from the DB here rather than trusted from the JWT: the
    token is only baked in at login time (auth_utils.create_token), so an
    admin reassigning an HRBP's BUs/companies after they last logged in used
    to leave their dashboard scoped to stale values until they signed out
    and back in. An HRBP can own more than one home BU (app_user_bu) and/or
    be assigned a whole group company (app_user_company, from the
    Organisation screen) -- a company assignment widens the fallback to
    every BU under that company, never other companies. Email is matched
    case-insensitively since requisition.hrbp_email is copied from a
    separate `hrbp` lookup table (picked by a recruiter/TA on the
    requisition form) that has no FK to app_user -- a casing difference
    between the two was silently failing the match."""
    row = query_one("SELECT email FROM app_user WHERE id = %s", [user.get("sub")]) or {}
    email = row.get("email") or user.get("email")
    bu_ids = [r["bu_id"] for r in (query("SELECT bu_id FROM app_user_bu WHERE user_id = %s", [user.get("sub")]) or [])]
    company_ids = [r["company_id"] for r in (query("SELECT company_id FROM app_user_company WHERE user_id = %s", [user.get("sub")]) or [])]
    return (
        "(LOWER(r.hrbp_email) = LOWER(%s) OR r.bu_id = ANY(%s) "
        "OR r.bu_id IN (SELECT id FROM business_unit WHERE company_id = ANY(%s)))",
        [email, bu_ids, company_ids],
    )


def _require_hrbp(user: dict) -> None:
    if user.get("role") != "hrbp":
        raise HTTPException(403, "HRBP access only")


def _hrbp_bu_scope(user: dict, bu_id: str):
    """WHERE-AND fragment + params restricting a pivot query (aliased `r`) to
    this HRBP's own assigned requisitions AND the single selected BU."""
    where, params = scope_requisitions_for_hrbp(user)
    return f"AND ({where}) AND r.bu_id = %s", params + [bu_id]


def _assert_hrbp_bu(user: dict, bu_id: str) -> None:
    """403 unless the HRBP has at least one assigned requisition in bu_id --
    prevents viewing another BU's report by tampering with the bu_id param."""
    where, params = scope_requisitions_for_hrbp(user)
    row = query_one(
        f"SELECT 1 FROM requisition r WHERE r.bu_id = %s AND {where} LIMIT 1",
        [bu_id, *params],
    )
    if not row:
        raise HTTPException(403, "You have no requisitions assigned in that business unit")


@router.get("/bus")
def hrbp_bus(user: dict = Depends(get_current_user)):
    """Business units this HRBP actually has assigned requisitions in -- for
    the BU picker at the top of their Reports screen."""
    _require_hrbp(user)
    where, params = scope_requisitions_for_hrbp(user)
    return query(
        f"""SELECT DISTINCT bu.id, bu.name
            FROM requisition r JOIN business_unit bu ON bu.id = r.bu_id
            WHERE {where}
            ORDER BY bu.name""",
        params,
    )


# ─── Lookup endpoints (used by the requisition create/edit modal) ────────────

@router.get("")
def list_hrbp(user: dict = Depends(get_current_user)):
    """All active HRBPs -- for the manual-override dropdown on requisition create/edit."""
    return query(
        "SELECT id, full_name, email FROM hrbp WHERE is_active = true ORDER BY full_name"
    )


@router.get("/by-bu/{bu_id}")
def hrbp_for_bu(bu_id: str, user: dict = Depends(get_current_user)):
    """The mapped default HRBP for a business unit -- for requisition auto-fill.
    Returns {} if the BU has no default HRBP assigned yet."""
    row = query_one(
        """SELECT h.id, h.full_name, h.email
           FROM bu_hrbp_map m JOIN hrbp h ON h.id = m.hrbp_id
           WHERE m.bu_id = %s AND h.is_active = true""",
        [bu_id],
    )
    return row or {}


# ─── HRBP's own read-only requisition + candidate-status view ────────────────

@router.get("/requisitions")
def hrbp_requisitions(user: dict = Depends(get_current_user)):
    _require_hrbp(user)
    where, params = scope_requisitions_for_hrbp(user)
    return query(
        f"""SELECT r.id, r.title, r.req_code, r.status, r.roll_type,
                   b.code AS band, bu.name AS business_unit,
                   hm.full_name AS hiring_manager_name,
                   (SELECT string_agg(u.full_name, ', ' ORDER BY rr.is_owner DESC)
                      FROM requisition_recruiter rr JOIN app_user u ON u.id = rr.recruiter_id
                      WHERE rr.requisition_id = r.id) AS recruiter_names,
                   (SELECT COUNT(*) FROM application WHERE requisition_id = r.id) AS in_pipeline
            FROM requisition r
            JOIN band b ON b.id = r.band_id
            JOIN business_unit bu ON bu.id = r.bu_id
            LEFT JOIN app_user hm ON hm.id = r.hiring_manager_id
            WHERE {where} AND COALESCE(r.approval_status, 'approved') = 'approved'
            ORDER BY r.created_at DESC""",
        params,
    )


# ─── HRBP dashboard + reports (scoped to their own BU/email requisitions) ────

@router.get("/dashboard")
def hrbp_dashboard(user: dict = Depends(get_current_user)):
    _require_hrbp(user)
    return _hrbp_dashboard_data(user)


def _hrbp_dashboard_data(user: dict) -> dict:
    """KPI summary + stage funnel + hiring-manager breakdown for this HRBP's
    own requisitions -- same scope as /requisitions and /requisitions/{id}/candidates.
    Shared by /dashboard and /reports/excel so both stay in sync."""
    where, params = scope_requisitions_for_hrbp(user)

    open_reqs = query_one(
        f"""SELECT COUNT(*) AS n FROM requisition r
            WHERE {where} AND r.status = 'open'
              AND COALESCE(r.approval_status, 'approved') = 'approved'""",
        params,
    )
    total_pipeline = query_one(
        f"""SELECT COUNT(*) AS n FROM application a
            JOIN requisition r ON r.id = a.requisition_id
            WHERE {where}""",
        params,
    )
    ttf = query_one(
        f"""SELECT ROUND(AVG(
                EXTRACT(EPOCH FROM (se.occurred_at - a.applied_at)) / 86400.0
            )::numeric, 1) AS avg_days
            FROM application a
            JOIN stage_event se ON se.application_id = a.id AND se.to_status = 'hired'
            JOIN requisition r ON r.id = a.requisition_id
            WHERE {where}""",
        params,
    )
    funnel_rows = query(
        f"""SELECT a.status, COUNT(*) AS n
            FROM application a JOIN requisition r ON r.id = a.requisition_id
            WHERE {where}
            GROUP BY a.status""",
        params,
    ) or []
    funnel_counts = {row["status"]: row["n"] for row in funnel_rows}
    funnel = [
        {"stage": s, "label": PIPELINE_STAGE_LABELS.get(s, s), "count": funnel_counts.get(s, 0)}
        for s in PIPELINE_STAGES
    ]

    by_hm_rows = query(
        f"""SELECT hm.full_name AS hiring_manager_name,
                   COUNT(DISTINCT r.id) FILTER (WHERE r.status = 'open') AS open_reqs,
                   COUNT(a.id) AS pipeline
            FROM requisition r
            LEFT JOIN app_user hm ON hm.id = r.hiring_manager_id
            LEFT JOIN application a ON a.requisition_id = r.id
            WHERE {where} AND COALESCE(r.approval_status, 'approved') = 'approved'
            GROUP BY hm.full_name
            ORDER BY hm.full_name NULLS LAST""",
        params,
    ) or []

    return {
        "open_reqs": int((open_reqs or {}).get("n") or 0),
        "total_pipeline": int((total_pipeline or {}).get("n") or 0),
        "avg_time_to_fill_days": (ttf or {}).get("avg_days"),
        "funnel": funnel,
        "by_hiring_manager": [
            {
                "hiring_manager_name": row["hiring_manager_name"] or "Unassigned",
                "open_reqs": row["open_reqs"],
                "pipeline": row["pipeline"],
            }
            for row in by_hm_rows
        ],
    }


@router.get("/reports/pivot/{pivot_id}")
def hrbp_pivot(
    pivot_id: str,
    bu_id: str = Query(...),
    period: str = Query("yearly"),
    year: int = Query(default_factory=lambda: date.today().year),
    user: dict = Depends(get_current_user),
):
    """Same 8 pivots as /api/reports2/{ta,recruiter}/pivot/{id}, restricted to
    this HRBP's own assigned requisitions within the selected BU."""
    _require_hrbp(user)
    if pivot_id not in _rp.PIVOT_MAP:
        raise HTTPException(404, f"pivot_id must be 1-8, got {pivot_id!r}")
    _assert_hrbp_bu(user, bu_id)
    ps = _period_start(period, year)
    xwhere, xp = _hrbp_bu_scope(user, bu_id)
    if pivot_id == "6":
        return _rp._pivot6(year, ps, "", "", xwhere=xwhere, xp=xp)
    if pivot_id == "7":
        return _rp._pivot7(year, ps, "", [], xwhere=xwhere, xp=xp)
    return _rp.PIVOT_MAP[pivot_id][1](year, ps, "", [], xwhere=xwhere, xp=xp)


@router.get("/reports/by-hm")
def hrbp_by_hm(bu_id: str = Query(...), user: dict = Depends(get_current_user)):
    """Hiring-manager breakdown for the selected BU -- not one of the 8 standard
    pivots, added specifically for the HRBP report per Batch 1.2 requirements."""
    _require_hrbp(user)
    _assert_hrbp_bu(user, bu_id)
    where, params = scope_requisitions_for_hrbp(user)
    rows = query(
        f"""SELECT hm.full_name AS hiring_manager_name,
                   COUNT(DISTINCT r.id) FILTER (WHERE r.status = 'open') AS open_reqs,
                   COUNT(a.id) AS pipeline
            FROM requisition r
            LEFT JOIN app_user hm ON hm.id = r.hiring_manager_id
            LEFT JOIN application a ON a.requisition_id = r.id
            WHERE {where} AND r.bu_id = %s AND COALESCE(r.approval_status, 'approved') = 'approved'
            GROUP BY hm.full_name
            ORDER BY hm.full_name NULLS LAST""",
        params + [bu_id],
    ) or []
    return [
        {
            "hiring_manager_name": row["hiring_manager_name"] or "Unassigned",
            "open_reqs": row["open_reqs"],
            "pipeline": row["pipeline"],
        }
        for row in rows
    ]


@router.get("/reports/excel")
def hrbp_reports_excel(
    bu_id: str = Query(...),
    period: str = Query("yearly"),
    year: int = Query(default_factory=lambda: date.today().year),
    user: dict = Depends(get_current_user),
):
    """Excel export mirroring /api/reports2/{ta,recruiter}/excel -- same 8 pivots
    plus a By Hiring Manager sheet, restricted to this HRBP's assigned
    requisitions within the selected BU."""
    _require_hrbp(user)
    _assert_hrbp_bu(user, bu_id)
    ps = _period_start(period, year)
    xwhere, xp = _hrbp_bu_scope(user, bu_id)
    pivots = [
        _rp._pivot1(year, ps, "", [], xwhere=xwhere, xp=xp),
        _rp._pivot2(year, ps, "", [], xwhere=xwhere, xp=xp),
        _rp._pivot3(year, ps, "", [], xwhere=xwhere, xp=xp),
        _rp._pivot4(year, ps, "", [], xwhere=xwhere, xp=xp),
        _rp._pivot5(year, ps, "", [], xwhere=xwhere, xp=xp),
        _rp._pivot6(year, ps, "", "", xwhere=xwhere, xp=xp),
        _rp._pivot7(year, ps, "", [], xwhere=xwhere, xp=xp),
        [_rp._pivot8(year, ps, "", [], xwhere=xwhere, xp=xp)],
    ]
    bu_row = query_one("SELECT name FROM business_unit WHERE id = %s", [bu_id])
    bu_name = (bu_row or {}).get("name") or bu_id
    bu_slug = re.sub(r"[^A-Za-z0-9]+", "_", bu_name).strip("_") or "bu"
    wb = _rp._build_workbook(
        pivots,
        title=f"My BU Report — {bu_name} — {period.title()} {year}",
        generated_by=user.get("name") or user.get("email") or "",
    )
    excel_export.sheet_from_rows(
        wb, "By Hiring Manager",
        [
            {"Hiring Manager": h["hiring_manager_name"], "Open Reqs": h["open_reqs"], "Pipeline": h["pipeline"]}
            for h in hrbp_by_hm(bu_id=bu_id, user=user)
        ],
    )
    return excel_export.stream_workbook(wb, f"enternly_hrbp_bu_report_{bu_slug}_{year}_{period}.xlsx")


@router.get("/requisitions/{req_id}/candidates")
def hrbp_requisition_candidates(req_id: str, user: dict = Depends(get_current_user)):
    _require_hrbp(user)
    where, params = scope_requisitions_for_hrbp(user)
    req = query_one(
        f"""SELECT r.id, r.title, hm.full_name AS hiring_manager_name,
                   (SELECT string_agg(u.full_name, ', ' ORDER BY rr.is_owner DESC)
                      FROM requisition_recruiter rr JOIN app_user u ON u.id = rr.recruiter_id
                      WHERE rr.requisition_id = r.id) AS recruiter_names
            FROM requisition r
            LEFT JOIN app_user hm ON hm.id = r.hiring_manager_id
            WHERE r.id = %s AND {where}""",
        [req_id, *params],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    apps = query(
        """SELECT a.id, a.status, c.full_name, isr.status AS isr_status
           FROM application a
           JOIN candidate c ON c.id = a.candidate_id
           LEFT JOIN LATERAL (
               SELECT status FROM interview_schedule_request
               WHERE application_id = a.id ORDER BY created_at DESC LIMIT 1
           ) isr ON true
           WHERE a.requisition_id = %s
           ORDER BY a.applied_at DESC""",
        [req_id],
    ) or []

    rag = bulk_application_rag([str(a["id"]) for a in apps]) if apps else {}
    return {
        "requisition": {
            "id": req["id"],
            "title": req["title"],
            "hiring_manager_name": req["hiring_manager_name"],
            "recruiter_names": req["recruiter_names"],
        },
        "candidates": [
            {
                "id": a["id"],
                "candidate_name": a["full_name"],
                "status": a["status"],
                "sla": rag.get(str(a["id"]), {}).get("status", "green"),
                "pending_from": derive_pending_from(a["status"], a["isr_status"]),
            }
            for a in apps
        ],
    }
