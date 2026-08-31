"""
KPI Dashboard — visual management dashboard with cards + charts.
Role-scoped: recruiter sees own; ta_manager/admin see all; HM sees their reqs.
Reuses stage_event, sla.py helpers, and existing DB schema — no new migrations.
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services.sla import (
    PIPELINE_STAGE_LABELS,
    STAGE_SLA_KEY,
    load_config,
    compute_rag,
)
from ..services.period import period_start as _period_start
from ..services.report_scope import scope_for as _scope
from ..services import excel_export

from ..module_access import require_tenant_module

router = APIRouter(prefix="/api/kpi", tags=["kpi"],
                    dependencies=[Depends(require_tenant_module("kpi_dashboard"))])

FUNNEL_STAGES = [
    "applied",
    "screen",
    "enteri_ai_bot",
    "shortlisted",
    "interview",
    "documentation",
    "offered",
]

_TERMINAL_STATUSES = (
    "hired", "rejected", "on_hold", "joined",
    "screen_rejected", "dropped", "offer_cancelled",
)


# ── Dashboard endpoint ────────────────────────────────────────────────────────

@router.get("/dashboard")
def kpi_dashboard(
    period: str = Query("yearly"),
    year: int = Query(default_factory=lambda: date.today().year),
    user: dict = Depends(get_current_user),
):
    if user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    role = user["role"]
    uid  = user["sub"]
    ps   = _period_start(period, year)
    sjoin, swhere, sjp, swp = _scope(role, uid, user.get("tenant_id"))

    # ── KPI cards ──────────────────────────────────────────────────────────

    open_reqs_row = query_one(
        f"""SELECT COUNT(DISTINCT r.id) AS n
            FROM requisition r {sjoin}
            WHERE r.status = 'open'
              AND COALESCE(r.approval_status, 'approved') = 'approved'
              {swhere}""",
        sjp + swp,
    )

    pos_fill_row = query_one(
        f"""SELECT COALESCE(SUM(r.openings), 0) AS n
            FROM requisition r {sjoin}
            WHERE r.status = 'open'
              AND COALESCE(r.approval_status, 'approved') = 'approved'
              {swhere}""",
        sjp + swp,
    )

    pipeline_row = query_one(
        f"""SELECT COUNT(DISTINCT a.id) AS n
            FROM application a
            JOIN requisition r ON r.id = a.requisition_id {sjoin}
            WHERE a.applied_at >= %s
              AND a.status NOT IN ({', '.join(['%s']*len(_TERMINAL_STATUSES))})
              {swhere}""",
        sjp + [ps] + list(_TERMINAL_STATUSES) + swp,
    )

    ttf_row = query_one(
        f"""SELECT ROUND(AVG(
                EXTRACT(EPOCH FROM (se.occurred_at - a.applied_at)) / 86400.0
            )::numeric, 1) AS avg_days
            FROM application a
            JOIN stage_event se ON se.application_id = a.id AND se.to_status = 'hired'
            JOIN requisition r ON r.id = a.requisition_id {sjoin}
            WHERE se.occurred_at >= %s
            {swhere}""",
        sjp + [ps] + swp,
    )

    # SLA breach counts — reuse sla service helpers
    sla_cfg = load_config(user.get("tenant_id"))
    breach_rows = query(
        f"""SELECT a.status,
                EXTRACT(EPOCH FROM (now() - COALESCE(
                    (SELECT se2.occurred_at FROM stage_event se2
                     WHERE se2.application_id = a.id
                       AND se2.to_status = a.status
                     ORDER BY se2.occurred_at DESC LIMIT 1),
                    a.applied_at
                ))) / 86400.0 AS elapsed_days
            FROM application a
            JOIN requisition r ON r.id = a.requisition_id {sjoin}
            WHERE a.status NOT IN ({', '.join(['%s']*len(_TERMINAL_STATUSES))})
              AND a.applied_at >= %s
              {swhere}""",
        sjp + list(_TERMINAL_STATUSES) + [ps] + swp,
    )
    red_count = amber_count = 0
    for row in (breach_rows or []):
        sla_key = STAGE_SLA_KEY.get(row["status"], "stage_default")
        target  = sla_cfg.get(sla_key, sla_cfg.get("stage_default", 5))
        rag     = compute_rag(row["elapsed_days"], target)
        if   rag["status"] == "red":   red_count   += 1
        elif rag["status"] == "amber": amber_count += 1

    kpi_cards = {
        "open_reqs":              int(open_reqs_row["n"]) if open_reqs_row else 0,
        "positions_to_fill":      int(pos_fill_row["n"])  if pos_fill_row  else 0,
        "candidates_in_pipeline": int(pipeline_row["n"])  if pipeline_row  else 0,
        "avg_time_to_fill_days": (
            float(ttf_row["avg_days"])
            if ttf_row and ttf_row["avg_days"] is not None
            else None
        ),
        "sla_breaches_red":   red_count,
        "sla_breaches_amber": amber_count,
    }

    # ── Funnel (current status distribution in the period) ─────────────────

    stage_rows = query(
        f"""SELECT a.status, COUNT(*) AS n
            FROM application a
            JOIN requisition r ON r.id = a.requisition_id {sjoin}
            WHERE a.applied_at >= %s
            {swhere}
            GROUP BY a.status""",
        sjp + [ps] + swp,
    )
    stage_map = {r["status"]: int(r["n"]) for r in (stage_rows or [])}

    funnel = []
    prev_count = None
    for s in FUNNEL_STAGES:
        count = stage_map.get(s, 0)
        if s == "offered":
            # candidates who passed through offered (now hired) count here too
            count += stage_map.get("hired", 0) + stage_map.get("joined", 0)
        conv_pct = None
        if prev_count and prev_count > 0:
            conv_pct = round(count / prev_count * 100, 1)
        funnel.append({
            "stage":    s,
            "label":    PIPELINE_STAGE_LABELS.get(s, s.replace("_", " ").title()),
            "count":    count,
            "conv_pct": conv_pct,
        })
        if count > 0:
            prev_count = count

    # ── Source effectiveness ───────────────────────────────────────────────

    source_rows = query(
        f"""SELECT COALESCE(c.source, 'unknown') AS source,
                   COUNT(DISTINCT a.id) AS total,
                   COUNT(DISTINCT a.id) FILTER (
                       WHERE a.status IN ('hired', 'joined')
                   ) AS hires
            FROM application a
            JOIN candidate c ON c.id = a.candidate_id
            JOIN requisition r ON r.id = a.requisition_id {sjoin}
            WHERE a.applied_at >= %s
            {swhere}
            GROUP BY c.source
            ORDER BY total DESC""",
        sjp + [ps] + swp,
    )
    source_effectiveness = [
        {"source": r["source"], "total": int(r["total"]), "hires": int(r["hires"])}
        for r in (source_rows or [])
    ]

    # ── Recruiter load ─────────────────────────────────────────────────────

    if role == "recruiter":
        own = query_one(
            """SELECT
                   COUNT(DISTINCT r.id) FILTER (
                       WHERE r.status = 'open'
                         AND COALESCE(r.approval_status, 'approved') = 'approved'
                   ) AS open_reqs,
                   COUNT(DISTINCT a.id) FILTER (
                       WHERE a.status NOT IN (
                           'hired','rejected','on_hold','joined',
                           'screen_rejected','dropped','offer_cancelled'
                       )
                   ) AS active_candidates
               FROM requisition_recruiter rr
               JOIN requisition r ON r.id = rr.requisition_id
               LEFT JOIN application a ON a.requisition_id = r.id
               WHERE rr.recruiter_id = %s""",
            [uid],
        )
        recruiter_load = [{
            "recruiter":         "You",
            "open_reqs":         int(own["open_reqs"])         if own else 0,
            "active_candidates": int(own["active_candidates"]) if own else 0,
        }]
    else:
        load_rows = query(
            """SELECT u.full_name AS recruiter,
                      COUNT(DISTINCT r.id) FILTER (
                          WHERE r.status = 'open'
                            AND COALESCE(r.approval_status, 'approved') = 'approved'
                      ) AS open_reqs,
                      COUNT(DISTINCT a.id) FILTER (
                          WHERE a.status NOT IN (
                              'hired','rejected','on_hold','joined',
                              'screen_rejected','dropped','offer_cancelled'
                          )
                      ) AS active_candidates
               FROM app_user u
               JOIN requisition_recruiter rr ON rr.recruiter_id = u.id
               JOIN requisition r ON r.id = rr.requisition_id
               LEFT JOIN application a ON a.requisition_id = r.id
               WHERE u.role IN ('recruiter','ta_manager') AND u.tenant_id = %s
               GROUP BY u.full_name, u.id
               ORDER BY open_reqs DESC""",
            [user.get("tenant_id")],
        )
        recruiter_load = [
            {
                "recruiter":         r["recruiter"],
                "open_reqs":         int(r["open_reqs"] or 0),
                "active_candidates": int(r["active_candidates"] or 0),
            }
            for r in (load_rows or [])
        ]

    # ── Offer stats ────────────────────────────────────────────────────────

    try:
        offer_row = query_one(
            f"""SELECT
                    COUNT(*) FILTER (WHERE o.status IN (
                        'pending_approval','revising','on_hold','draft'
                    )) AS pending,
                    COUNT(*) FILTER (WHERE o.status IN (
                        'approved','sent_to_darwinbox','released','accepted'
                    )) AS approved,
                    COUNT(*) FILTER (WHERE o.status IN (
                        'rejected','cancelled','declined'
                    )) AS rejected,
                    ROUND(AVG(
                        EXTRACT(EPOCH FROM (oas.last_acted - o.created_at)) / 86400.0
                    )::numeric, 1) AS avg_approval_days
                FROM offer o
                JOIN application a ON a.id = o.application_id
                JOIN requisition r ON r.id = a.requisition_id {sjoin}
                LEFT JOIN LATERAL (
                    SELECT MAX(acted_at) AS last_acted
                    FROM offer_approval_step
                    WHERE offer_id = o.id AND acted_at IS NOT NULL
                ) oas ON true
                WHERE o.created_at >= %s
                {swhere}""",
            sjp + [ps] + swp,
        )
        offer_stats = {
            "pending":           int(offer_row["pending"])  if offer_row else 0,
            "approved":          int(offer_row["approved"]) if offer_row else 0,
            "rejected":          int(offer_row["rejected"]) if offer_row else 0,
            "avg_approval_days": (
                float(offer_row["avg_approval_days"])
                if offer_row and offer_row["avg_approval_days"] is not None
                else None
            ),
        }
    except Exception:
        offer_stats = {"pending": 0, "approved": 0, "rejected": 0, "avg_approval_days": None}

    return {
        "kpi_cards":            kpi_cards,
        "funnel":               funnel,
        "source_effectiveness": source_effectiveness,
        "recruiter_load":       recruiter_load,
        "offer_stats":          offer_stats,
    }


# ── Excel export ──────────────────────────────────────────────────────────────

@router.get("/excel")
def kpi_excel(
    period: str = Query("yearly"),
    year: int = Query(default_factory=lambda: date.today().year),
    user: dict = Depends(get_current_user),
):
    if user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    import openpyxl

    data = kpi_dashboard(period=period, year=year, user=user)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # Sheet 1: KPI Summary (unchanged name/shape — pre-existing metric/value sheet)
    cards = data["kpi_cards"]
    excel_export.sheet_from_rows(
        wb, "KPI Summary",
        [
            {"Metric": "Open Requisitions",       "Value": cards["open_reqs"]},
            {"Metric": "Positions to Fill",        "Value": cards["positions_to_fill"]},
            {"Metric": "Candidates in Pipeline",   "Value": cards["candidates_in_pipeline"]},
            {"Metric": "Avg Time-to-Fill (days)",  "Value": cards["avg_time_to_fill_days"] or "N/A"},
            {"Metric": "SLA Breaches (Red)",       "Value": cards["sla_breaches_red"]},
            {"Metric": "SLA Warnings (Amber)",     "Value": cards["sla_breaches_amber"]},
        ],
    )

    # Sheet 2: Pipeline Funnel
    excel_export.sheet_from_rows(
        wb, "Pipeline Funnel",
        [
            {"Stage": f["label"], "Candidates": f["count"], "Conversion %": f["conv_pct"] if f["conv_pct"] is not None else ""}
            for f in data["funnel"]
        ],
    )

    # Sheet 3: Source Effectiveness
    excel_export.sheet_from_rows(
        wb, "Source Effectiveness",
        [
            {
                "Source": s["source"],
                "Total Candidates": s["total"],
                "Hires": s["hires"],
                "Hit Rate %": round(s["hires"] / s["total"] * 100, 1) if s["total"] else 0,
            }
            for s in data["source_effectiveness"]
        ],
    )

    # Sheet 4: Recruiter Load
    excel_export.sheet_from_rows(
        wb, "Recruiter Load",
        [
            {"Recruiter": r["recruiter"], "Open Reqs": r["open_reqs"], "Active Candidates": r["active_candidates"]}
            for r in data["recruiter_load"]
        ],
    )

    # Sheet 5: Offer Stats
    o = data["offer_stats"]
    excel_export.sheet_from_rows(
        wb, "Offer Stats",
        [
            {"Metric": "Pending",           "Value": o["pending"]},
            {"Metric": "Approved",          "Value": o["approved"]},
            {"Metric": "Rejected",          "Value": o["rejected"]},
            {"Metric": "Avg Approval Days", "Value": o["avg_approval_days"] or "N/A"},
        ],
    )

    excel_export.build_summary_sheet(
        wb,
        title=f"KPI Dashboard — {period.title()} {year}",
        generated_by=user.get("name") or user.get("email") or "",
        generated_at=datetime.now(),
        filters_applied=[{"key": "period", "op": "=", "value": period}, {"key": "year", "op": "=", "value": year}],
        rows=data["funnel"],
        measures_meta=[{"key": "count", "label": "Candidates"}, {"key": "conv_pct", "label": "Conversion %"}],
    )

    return excel_export.stream_workbook(wb, f"enternly_kpi_{year}_{period}.xlsx")
