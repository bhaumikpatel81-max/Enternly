"""
SLA / RAG (Red-Amber-Green) API — deadline tracking across pipeline.

Endpoints
---------
GET  /api/sla/config              — read SLA targets (Company Admin)
POST /api/sla/config              — save SLA targets (Company Admin)
GET  /api/sla/dashboard           — breach dashboard — all AMBER/RED items, role-scoped
POST /api/sla/app-rag-bulk        — RAG for a batch of application IDs
POST /api/sla/req-rag-bulk        — RAG for a batch of requisition IDs

Role scoping on /dashboard:
  recruiter    → only their own requisitions
  ta_manager   → all
  admin        → all
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import query
from ..auth_utils import get_current_user
from ..module_access import recruiter_has_module
from ..services import excel_export
from ..services.sla import (
    SLA_DEFAULTS,
    STAGE_SLA_KEY,
    TERMINAL,
    load_config,
    compute_rag,
    bulk_application_rag,
    bulk_requisition_rag,
)

router = APIRouter(prefix="/api/sla", tags=["sla"])

_ALLOWED_ROLES_WRITE = {"admin", "platform_admin", "company_admin"}
_ALLOWED_ROLES_READ  = {"admin", "platform_admin", "company_admin", "ta_manager", "recruiter"}


def _require_sla_write(user: dict = Depends(get_current_user)) -> dict:
    role = user.get("role")
    if role in _ALLOWED_ROLES_WRITE:
        return user
    if role == "recruiter" and recruiter_has_module(user.get("sub"), "sla_settings"):
        return user
    raise HTTPException(403, "Company Admin access required")


def _require_sla_read(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in _ALLOWED_ROLES_READ:
        raise HTTPException(403, "Recruiter, TA Manager or Admin access required")
    return user


# ── Pydantic models ───────────────────────────────────────────────────────────

class SLAConfigIn(BaseModel):
    stage_applied:       Optional[int] = None
    stage_screen:        Optional[int] = None
    stage_nexai_bot:     Optional[int] = None
    stage_shortlisted:   Optional[int] = None
    stage_interview:     Optional[int] = None
    stage_documentation: Optional[int] = None
    stage_offered:       Optional[int] = None
    stage_default:       Optional[int] = None
    req_time_to_fill:    Optional[int] = None
    approval_step:       Optional[int] = None


class BulkAppRagIn(BaseModel):
    app_ids: list[str]


class BulkReqRagIn(BaseModel):
    req_ids: list[str]


# ── Config endpoints ──────────────────────────────────────────────────────────

@router.get("/config")
def get_sla_config(user: dict = Depends(_require_sla_write)):
    """Return current SLA config merged with defaults."""
    rows = query("SELECT config_key, days, updated_at FROM sla_config WHERE tenant_id = %s", [user.get("tenant_id")]) or []
    stored = {r["config_key"]: r["days"] for r in rows}
    result = {}
    for k, default in SLA_DEFAULTS.items():
        result[k] = stored.get(k, default)
    return result


@router.post("/config")
def save_sla_config(body: SLAConfigIn, user: dict = Depends(_require_sla_write)):
    """Upsert SLA config values. Only provided (non-None) fields are written."""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No config values provided")
    for key, days in updates.items():
        if days < 1:
            raise HTTPException(400, f"{key}: days must be >= 1")
    tenant_id = user.get("tenant_id")
    # One batched upsert via unnest() instead of one INSERT per config key.
    keys = list(updates.keys())
    days_vals = [updates[k] for k in keys]
    query(
        """INSERT INTO sla_config (config_key, days, updated_by, tenant_id)
           SELECT k, d, %s, %s FROM unnest(%s::text[], %s::int[]) AS t(k, d)
           ON CONFLICT (tenant_id, config_key)
           DO UPDATE SET days = EXCLUDED.days,
                         updated_at = now(),
                         updated_by = EXCLUDED.updated_by""",
        [user["sub"], tenant_id, keys, days_vals],
        fetch=False,
    )
    return {"ok": True, "updated": keys}


# ── Bulk RAG endpoints ────────────────────────────────────────────────────────

@router.post("/app-rag-bulk")
def app_rag_bulk(body: BulkAppRagIn, user: dict = Depends(get_current_user)):
    """Return RAG status for a batch of application IDs."""
    if user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    if not body.app_ids:
        return {}
    if len(body.app_ids) > 500:
        raise HTTPException(400, "Too many app IDs (max 500)")
    cfg = load_config(user.get("tenant_id"))
    return bulk_application_rag(body.app_ids, cfg)


@router.post("/req-rag-bulk")
def req_rag_bulk(body: BulkReqRagIn, user: dict = Depends(get_current_user)):
    """Return RAG status for a batch of requisition IDs."""
    if user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    if not body.req_ids:
        return {}
    if len(body.req_ids) > 500:
        raise HTTPException(400, "Too many req IDs (max 500)")
    cfg = load_config(user.get("tenant_id"))
    return bulk_requisition_rag(body.req_ids, cfg)


# ── Breach dashboard ──────────────────────────────────────────────────────────

@router.get("/dashboard")
def sla_dashboard(user: dict = Depends(_require_sla_read)):
    """
    Return all AMBER and RED items for the breach dashboard.
    Role-scoped: recruiters see only their requisitions; ta_manager/admin see all.
    """
    role     = user.get("role")
    uid      = user.get("sub")
    cfg      = load_config(user.get("tenant_id"))

    breaches = []

    # ── 1. Candidate stage breaches ──────────────────────────────────────────
    stage_rows = _query_stage_breaches(role, uid)
    for r in (stage_rows or []):
        status  = r["current_stage"]
        if status in TERMINAL:
            continue
        sla_key = STAGE_SLA_KEY.get(status, "stage_default")
        target  = cfg.get(sla_key, cfg["stage_default"])
        rag     = compute_rag(r["elapsed_days"], target)
        if rag["status"] in ("amber", "red"):
            breaches.append({
                "type":         "stage",
                "app_id":       str(r["app_id"]),
                "req_id":       str(r["req_id"]),
                "item":         r["candidate_name"],
                "context":      f"{r['req_title']} — {_stage_label(status)}",
                "responsible":  r["recruiter_name"] or "Unassigned",
                "responsible_id": str(r["recruiter_id"]) if r.get("recruiter_id") else None,
                "elapsed_days": rag["elapsed_days"],
                "target_days":  rag["target_days"],
                "pct":          rag["pct"],
                "rag":          rag["status"],
            })

    # ── 2. Requisition time-to-fill breaches ────────────────────────────────
    req_target = cfg.get("req_time_to_fill", SLA_DEFAULTS["req_time_to_fill"])
    req_rows   = _query_req_breaches(role, uid)
    for r in (req_rows or []):
        rag = compute_rag(r["elapsed_days"], req_target)
        if rag["status"] in ("amber", "red"):
            breaches.append({
                "type":         "requisition",
                "req_id":       str(r["id"]),
                "item":         r["title"],
                "context":      f"{r['in_pipeline']} candidates in pipeline",
                "responsible":  r["recruiter_name"] or "Unassigned",
                "responsible_id": str(r["recruiter_id"]) if r.get("recruiter_id") else None,
                "elapsed_days": rag["elapsed_days"],
                "target_days":  rag["target_days"],
                "pct":          rag["pct"],
                "rag":          rag["status"],
            })

    # ── 3. Approval step breaches — per-step sla_days takes precedence ─────────
    appr_default = cfg.get("approval_step", SLA_DEFAULTS["approval_step"])
    appr_rows    = _query_approval_breaches(role, uid)
    for r in (appr_rows or []):
        # Use the step's own sla_days; fall back to global default
        step_target = int(r.get("sla_days") or appr_default)
        rag = compute_rag(r["elapsed_days"], step_target)
        if rag["status"] in ("amber", "red"):
            breaches.append({
                "type":         "approval",
                "offer_id":     str(r["offer_id"]),
                "req_id":       str(r["req_id"]),
                "item":         r["candidate_name"],
                "context":      f"{r['req_title']} — Step {r['sequence']}",
                "responsible":  r["approver_name"],
                "responsible_id": str(r["approver_id"]),
                "elapsed_days": rag["elapsed_days"],
                "target_days":  rag["target_days"],
                "pct":          rag["pct"],
                "rag":          rag["status"],
            })

    # Sort: red first, then amber; within each, highest elapsed first
    _order = {"red": 0, "amber": 1}
    breaches.sort(key=lambda x: (_order.get(x["rag"], 2), -x["elapsed_days"]))

    red_count   = sum(1 for b in breaches if b["rag"] == "red")
    amber_count = sum(1 for b in breaches if b["rag"] == "amber")

    return {
        "red":    red_count,
        "amber":  amber_count,
        "total":  len(breaches),
        "items":  breaches,
    }


@router.get("/excel")
def sla_excel(user: dict = Depends(_require_sla_read)):
    import openpyxl

    data = sla_dashboard(user=user)
    rows = [
        {
            "Type": b["type"], "Item": b["item"], "Context": b["context"],
            "Responsible": b["responsible"], "Elapsed Days": b["elapsed_days"],
            "Target Days": b["target_days"], "RAG": b["rag"].upper(),
        }
        for b in data["items"]
    ]

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    excel_export.sheet_from_rows(wb, "Breaches", rows)
    excel_export.build_summary_sheet(
        wb,
        title="SLA / Breach Dashboard",
        generated_by=user.get("name") or user.get("email") or "",
        generated_at=datetime.now(),
        filters_applied=[],
        rows=rows,
        measures_meta=[{"key": "Elapsed Days", "label": "Elapsed Days"}],
    )
    return excel_export.stream_workbook(wb, "enternly_sla_breaches.xlsx")


# ── Internal query helpers ────────────────────────────────────────────────────

def _stage_label(status: str) -> str:
    return status.replace("_", " ").title()


def _recruiter_filter_join(role: str, uid: str) -> tuple[str, list]:
    """Return extra JOIN + WHERE clause to scope results to recruiter's own reqs."""
    if role == "recruiter":
        return (
            "JOIN requisition_recruiter _rr ON _rr.requisition_id = a.requisition_id AND _rr.recruiter_id = %s",
            [uid],
        )
    return ("", [])


def _query_stage_breaches(role: str, uid: str) -> list:
    extra_join, extra_params = _recruiter_filter_join(role, uid)
    sql = f"""
        SELECT
            a.id AS app_id,
            c.full_name AS candidate_name,
            a.status AS current_stage,
            r.id AS req_id,
            r.title AS req_title,
            EXTRACT(EPOCH FROM (
                now() - COALESCE(
                    (SELECT se.occurred_at
                     FROM stage_event se
                     WHERE se.application_id = a.id
                       AND se.to_status = a.status
                     ORDER BY se.occurred_at DESC
                     LIMIT 1),
                    a.applied_at
                )
            )) / 86400.0 AS elapsed_days,
            (SELECT u.full_name
             FROM requisition_recruiter rr
             JOIN app_user u ON u.id = rr.recruiter_id
             WHERE rr.requisition_id = a.requisition_id
               AND rr.is_owner = true
             LIMIT 1) AS recruiter_name,
            (SELECT u.id
             FROM requisition_recruiter rr
             JOIN app_user u ON u.id = rr.recruiter_id
             WHERE rr.requisition_id = a.requisition_id
               AND rr.is_owner = true
             LIMIT 1) AS recruiter_id
        FROM application a
        JOIN candidate c ON c.id = a.candidate_id
        JOIN requisition r ON r.id = a.requisition_id
        {extra_join}
        WHERE a.status NOT IN (
            'hired','rejected','on_hold',
            'joined','screen_rejected','dropped','offer_cancelled'
        )
        ORDER BY elapsed_days DESC
    """
    return query(sql, extra_params)


def _query_req_breaches(role: str, uid: str) -> list:
    if role == "recruiter":
        scope_sql = """
            AND r.id IN (
                SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
            )
        """
        params = [uid]
    else:
        scope_sql = ""
        params = []

    sql = f"""
        SELECT
            r.id,
            r.title,
            EXTRACT(EPOCH FROM (
                now() - COALESCE(r.opened_at, r.created_at)
            )) / 86400.0 AS elapsed_days,
            (SELECT u.full_name
             FROM requisition_recruiter rr
             JOIN app_user u ON u.id = rr.recruiter_id
             WHERE rr.requisition_id = r.id AND rr.is_owner = true
             LIMIT 1) AS recruiter_name,
            (SELECT u.id
             FROM requisition_recruiter rr
             JOIN app_user u ON u.id = rr.recruiter_id
             WHERE rr.requisition_id = r.id AND rr.is_owner = true
             LIMIT 1) AS recruiter_id,
            (SELECT COUNT(*) FROM application
             WHERE requisition_id = r.id) AS in_pipeline
        FROM requisition r
        WHERE r.status = 'open'
        {scope_sql}
        ORDER BY elapsed_days DESC
    """
    return query(sql, params)


def _query_approval_breaches(role: str, uid: str) -> list:
    if role == "recruiter":
        scope_sql = """
            AND r.id IN (
                SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
            )
        """
        params = [uid]
    else:
        scope_sql = ""
        params = []

    sql = f"""
        SELECT
            oas.id AS step_id,
            o.id AS offer_id,
            r.id AS req_id,
            c.full_name AS candidate_name,
            r.title AS req_title,
            u.full_name AS approver_name,
            u.id AS approver_id,
            o.current_step,
            oas.sequence,
            COALESCE(oas.sla_days, 2) AS sla_days,
            EXTRACT(EPOCH FROM (
                now() - COALESCE(
                    (SELECT prev.acted_at
                     FROM offer_approval_step prev
                     WHERE prev.offer_id = o.id
                       AND prev.sequence = oas.sequence - 1),
                    o.submitted_at,
                    o.updated_at,
                    now() - INTERVAL '1 day'
                )
            )) / 86400.0 AS elapsed_days
        FROM offer_approval_step oas
        JOIN offer o ON o.id = oas.offer_id
            AND o.status = 'pending_approval'
            AND o.current_step = oas.sequence
        JOIN application a ON a.id = o.application_id
        JOIN candidate c ON c.id = a.candidate_id
        JOIN requisition r ON r.id = a.requisition_id
        JOIN app_user u ON u.id = oas.approver_id
        WHERE oas.status = 'pending'
        {scope_sql}
        ORDER BY elapsed_days DESC
    """
    return query(sql, params)
