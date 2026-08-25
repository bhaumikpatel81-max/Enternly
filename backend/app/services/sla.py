"""
SLA / RAG (Red-Amber-Green) helper.

RAG thresholds:
  GREEN  — elapsed < 80 % of target
  AMBER  — 80 % ≤ elapsed ≤ 100 % of target
  RED    — elapsed > 100 % (breached)

SLA config is stored in the sla_config table as (config_key, days).
Missing keys fall back to SLA_DEFAULTS.
"""
from ..db import query

# ── Ordered pipeline stages (single source of truth) ─────────────────────────
PIPELINE_STAGES = [
    "applied",
    "screen",
    "nexai_bot",
    "shortlisted",
    "interview",
    "documentation",
    "offered",
]

PIPELINE_STAGE_LABELS = {
    "applied":       "Applied",
    "screen":        "Screening",
    "nexai_bot":     "NexAI Interview",
    "shortlisted":   "Shortlisted",
    "interview":     "Interview",
    "documentation": "Documentation",
    "offered":       "Offered",
}

_TERMINAL_APP_STATUSES = ("hired", "rejected", "on_hold")


def derive_pending_from(app_status: str, isr_status: str | None) -> str:
    """Who the ball is with, right now -- derived at query time from
    application.status plus the most recent interview_schedule_request.status
    for that application. Deliberately not a stored column: kept here (a leaf
    service module both pipeline_api and hrbp_api already import from) rather
    than duplicated in each router, so it can never drift between the two
    places it's surfaced."""
    if app_status in _TERMINAL_APP_STATUSES:
        return "n/a"
    if isr_status == "awaiting_hm":
        return "hiring_manager"
    if isr_status == "awaiting_candidate":
        return "candidate"
    return "recruiter"

# "interview" is intentionally absent — advance endpoint handles it dynamically
# based on the requisition's round_config (number of panel levels configured).
NEXT_STAGE = {
    "applied":       "screen",
    "screen":        "nexai_bot",
    "nexai_bot":     "shortlisted",
    "shortlisted":   "interview",
    "documentation": "offered",
}

# ── Defaults (editable via TA-manager settings screen) ───────────────────────

SLA_DEFAULTS = {
    "stage_applied":       5,
    "stage_screen":        3,
    "stage_nexai_bot":     3,
    "stage_shortlisted":   2,
    "stage_interview":     5,
    "stage_documentation": 5,
    "stage_offered":       3,
    "stage_default":       5,
    "req_time_to_fill":   45,
    "approval_step":       2,
}

# application.status  →  sla_config key
STAGE_SLA_KEY = {
    "applied":       "stage_applied",
    "screen":        "stage_screen",
    "nexai_bot":     "stage_nexai_bot",
    "shortlisted":   "stage_shortlisted",
    "interview":     "stage_interview",
    "documentation": "stage_documentation",
    "offered":       "stage_offered",
    # Legacy aliases (Task 3 → Task 4 + any pre-migration records)
    "ai_screening":    "stage_screen",
    "screening":       "stage_screen",
    "screen_passed":   "stage_shortlisted",
    "hm_screening":    "stage_interview",
    "panel_interview": "stage_interview",
    "hr_round":        "stage_interview",
    "offer_approval":  "stage_documentation",
    "offer_stage":     "stage_documentation",
    "offer_on_hold":   "stage_offered",
    "selected":        "stage_interview",
    "interviewing":    "stage_interview",
}

# Statuses for which SLA tracking is suspended (candidate no longer active)
TERMINAL = frozenset({
    "hired", "rejected", "on_hold",
    # Legacy
    "joined", "screen_rejected", "dropped", "offer_cancelled",
})


# ── Config helpers ────────────────────────────────────────────────────────────

def load_config(tenant_id: str = None) -> dict:
    """Return merged SLA config (DB overrides take precedence over defaults).
    sla_config is tenant-scoped (Migration 96) -- tenant_id is optional only
    for callers with no request context; every router call site should pass
    the caller's own tenant_id."""
    if tenant_id:
        rows = query("SELECT config_key, days FROM sla_config WHERE tenant_id = %s", [tenant_id]) or []
    else:
        rows = query("SELECT config_key, days FROM sla_config") or []
    cfg = dict(SLA_DEFAULTS)
    for r in rows:
        cfg[r["config_key"]] = int(r["days"])
    return cfg


# ── RAG computation ───────────────────────────────────────────────────────────

def compute_rag(elapsed_days, target_days) -> dict:
    if elapsed_days is None or target_days is None or target_days <= 0:
        return {
            "status": "green",
            "pct": 0.0,
            "elapsed_days": 0.0,
            "target_days": int(target_days) if target_days else 0,
        }
    elapsed_days = float(elapsed_days)
    target_days  = int(target_days)
    pct = (elapsed_days / target_days) * 100.0
    if pct < 80.0:
        status = "green"
    elif pct <= 100.0:
        status = "amber"
    else:
        status = "red"
    return {
        "status":       status,
        "pct":          round(pct, 1),
        "elapsed_days": round(elapsed_days, 1),
        "target_days":  target_days,
    }


# ── Bulk application-stage RAG ────────────────────────────────────────────────

def bulk_application_rag(app_ids: list[str], sla_cfg: dict | None = None) -> dict:
    if not app_ids:
        return {}

    cfg = sla_cfg or load_config()

    placeholders = ", ".join(["%s"] * len(app_ids))
    rows = query(
        f"""
        SELECT
            a.id AS app_id,
            a.status,
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
            )) / 86400.0 AS elapsed_days
        FROM application a
        WHERE a.id IN ({placeholders})
        """,
        app_ids,
    )

    result = {}
    for r in (rows or []):
        app_id = str(r["app_id"])
        status = r["status"]
        if status in TERMINAL:
            result[app_id] = {"status": "green", "pct": 0.0,
                              "elapsed_days": 0.0, "target_days": 0}
            continue
        sla_key = STAGE_SLA_KEY.get(status, "stage_default")
        target  = cfg.get(sla_key, cfg.get("stage_default", 5))
        rag     = compute_rag(r["elapsed_days"], target)
        rag["stage"] = status
        result[app_id] = rag

    return result


# ── Bulk requisition RAG ──────────────────────────────────────────────────────

def bulk_requisition_rag(req_ids: list[str], sla_cfg: dict | None = None) -> dict:
    if not req_ids:
        return {}

    cfg    = sla_cfg or load_config()
    target = cfg.get("req_time_to_fill", SLA_DEFAULTS["req_time_to_fill"])

    placeholders = ", ".join(["%s"] * len(req_ids))
    rows = query(
        f"""
        SELECT
            id,
            status,
            EXTRACT(EPOCH FROM (
                now() - COALESCE(opened_at, created_at)
            )) / 86400.0 AS elapsed_days
        FROM requisition
        WHERE id IN ({placeholders})
        """,
        req_ids,
    )

    result = {}
    for r in (rows or []):
        req_id = str(r["id"])
        if r["status"] != "open":
            result[req_id] = {"status": "green", "pct": 0.0,
                              "elapsed_days": 0.0, "target_days": target}
        else:
            result[req_id] = compute_rag(r["elapsed_days"], target)

    return result
