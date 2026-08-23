"""
Custom Reports catalog — a fixed, allowlisted "semantic layer" over the
portal's data, deliberately NOT a general SQL/join builder.

Each entry in EXPLORES is one report grain (Requisitions, Applications,
Interviews, Offers, Stage Transitions) with:
  - from_sql:      a hardcoded FROM/JOIN chain (never client-influenced)
  - default_where: an always-applied filter (e.g. only approved requisitions)
  - dimensions:    fields that can be grouped by / selected as columns
  - measures:      pre-defined aggregate expressions (client picks the KEY,
                    never the aggregate function or raw SQL)
  - filters:       fields that can be filtered on, with declared type + ops
  - raw_columns:   an explicit allowlist for "give me everything" dumps

Every "sql" value below is a static string written by us — the client can
only ever reference catalog KEYS (e.g. "stage", "applied_at"), never SQL.
report_query_builder.validate_spec() enforces that before any query runs.

extra_join is attached to individual dimensions/measures/filters so a join
is only added to the query when a field that needs it is actually selected
— unused joins never execute.
"""
from .sla import PIPELINE_STAGES, PIPELINE_STAGE_LABELS, STAGE_SLA_KEY


# ── Canonical pipeline-stage SQL (single source of truth: sla.py) ────────────

def _canonical_stage_map() -> dict:
    """
    status value -> canonical FUNNEL stage key (one of PIPELINE_STAGES).

    Folds 'hired'/'joined' into 'offered', matching kpi_api.py's existing
    funnel semantics exactly (hired/joined candidates are counted within
    the "Offered" bucket there) so the Custom-Report funnel template
    reproduces the same numbers as the pre-existing KPI dashboard funnel.
    """
    mapping = {stage: stage for stage in PIPELINE_STAGES}
    for alias, sla_key in STAGE_SLA_KEY.items():
        stage = sla_key.replace("stage_", "", 1)
        if stage in PIPELINE_STAGES:
            mapping.setdefault(alias, stage)
    mapping["hired"] = "offered"
    mapping["joined"] = "offered"
    return mapping


def _stage_case_sql(column: str) -> str:
    mapping = _canonical_stage_map()
    whens = " ".join(
        f"WHEN {column} = '{status}' THEN '{PIPELINE_STAGE_LABELS[stage]}'"
        for status, stage in mapping.items()
    )
    return f"CASE {whens} ELSE 'Rejected/Other' END"


_STAGE_CASE_SQL = _stage_case_sql("a.status")
_TO_STAGE_CASE_SQL = _stage_case_sql("se.to_status")

STAGE_LABEL_ORDER = [PIPELINE_STAGE_LABELS[s] for s in PIPELINE_STAGES] + ["Rejected/Other"]


# ── Offer status buckets (ports kpi_api.py's hardcoded buckets) ──────────────

_OFFER_BUCKETS = {
    "pending":  ["pending_approval", "revising", "on_hold", "draft"],
    "approved": ["approved", "sent_to_darwinbox", "released", "accepted"],
    "rejected": ["rejected", "cancelled", "declined"],
}


def _offer_bucket_sql(column: str) -> str:
    whens = " ".join(
        f"WHEN {column} IN ({', '.join(repr(s) for s in statuses)}) THEN '{bucket}'"
        for bucket, statuses in _OFFER_BUCKETS.items()
    )
    return f"CASE {whens} ELSE 'other' END"


_OFFER_STATUS_BUCKET_SQL = _offer_bucket_sql("o.status")


# ── Explores ──────────────────────────────────────────────────────────────────

EXPLORES = {
    "requisition": {
        "label": "Requisitions",
        "from_sql": (
            "requisition r "
            "JOIN business_unit bu ON bu.id = r.bu_id "
            "JOIN group_company gc ON gc.id = bu.company_id "
            "LEFT JOIN app_user hm ON hm.id = r.hiring_manager_id"
        ),
        "default_where": "COALESCE(r.approval_status,'approved') = 'approved'",
        "dimensions": {
            "company":         {"sql": "gc.name", "label": "Company", "type": "string"},
            "bu":              {"sql": "bu.name", "label": "Business Unit", "type": "string"},
            "band":            {"sql": "b.code", "label": "Band", "type": "string",
                                 "extra_join": "JOIN band b ON b.id = r.band_id"},
            "roll_type":       {"sql": "r.roll_type", "label": "Roll Type", "type": "enum",
                                 "options": ["on_roll", "off_roll"]},
            "status":          {"sql": "r.status", "label": "Requisition Status", "type": "enum",
                                 "options": ["draft", "open", "on_hold", "closed", "cancelled"]},
            "priority":        {"sql": "r.priority", "label": "Priority", "type": "enum",
                                 "options": ["critical", "high", "medium", "low"]},
            "criticality":     {"sql": "r.criticality", "label": "Criticality", "type": "enum",
                                 "options": ["Low", "Medium", "High", "Critical"]},
            "fiscal_year":     {"sql": "r.fiscal_year", "label": "Fiscal Year", "type": "string"},
            "hiring_location": {"sql": "r.hiring_location", "label": "Location", "type": "string"},
            "hiring_manager":  {"sql": "hm.full_name", "label": "Hiring Manager", "type": "string"},
            "opened_month":    {"sql": "to_char(COALESCE(r.opened_at, r.created_at), 'YYYY-MM')",
                                 "label": "Opened Month", "type": "string"},
            "opened_year":     {"sql": "EXTRACT(year FROM COALESCE(r.opened_at, r.created_at))::int",
                                 "label": "Opened Year", "type": "number"},
        },
        "measures": {
            "count":      {"sql": "COUNT(DISTINCT r.id)", "label": "Requisitions"},
            "openings":   {"sql": "SUM(r.openings)", "label": "Total Openings"},
            "open_count": {"sql": "COUNT(DISTINCT r.id) FILTER (WHERE r.status = 'open')", "label": "Open Positions"},
            "avg_aging_days": {"sql": "ROUND(AVG(v.aging_days)::numeric, 1)", "label": "Avg Aging (days)",
                                "extra_join": "LEFT JOIN v_requisition_aging v ON v.id = r.id"},
        },
        "filters": {
            "opened_at": {"sql": "COALESCE(r.opened_at, r.created_at)", "type": "date", "ops": ["gte", "lte", "between"]},
            "status":    {"sql": "r.status", "type": "enum", "ops": ["eq", "in"],
                          "options": ["draft", "open", "on_hold", "closed", "cancelled"]},
            "roll_type": {"sql": "r.roll_type", "type": "enum", "ops": ["eq", "in"], "options": ["on_roll", "off_roll"]},
            "priority":  {"sql": "r.priority", "type": "enum", "ops": ["eq", "in"],
                          "options": ["critical", "high", "medium", "low"]},
            "criticality": {"sql": "r.criticality", "type": "enum", "ops": ["eq", "in"],
                            "options": ["Low", "Medium", "High", "Critical"]},
            "fiscal_year": {"sql": "r.fiscal_year", "type": "string", "ops": ["eq"]},
            "company":   {"sql": "gc.name", "type": "string", "ops": ["eq"]},
            "bu":        {"sql": "bu.name", "type": "string", "ops": ["eq"]},
        },
        "raw_columns": [
            ("Req Code", "r.req_code"), ("Title", "r.title"), ("Company", "gc.name"), ("BU", "bu.name"),
            ("Roll Type", "r.roll_type"), ("Status", "r.status"), ("Openings", "r.openings"),
            ("Hiring Manager", "hm.full_name"), ("Opened At", "r.opened_at"), ("Closed At", "r.closed_at"),
            ("Priority", "r.priority"), ("Criticality", "r.criticality"), ("Fiscal Year", "r.fiscal_year"),
            ("Location", "r.hiring_location"),
        ],
    },

    "application": {
        "label": "Applications / Pipeline",
        "from_sql": (
            "application a "
            "JOIN requisition r ON r.id = a.requisition_id "
            "JOIN candidate c ON c.id = a.candidate_id "
            "JOIN business_unit bu ON bu.id = r.bu_id "
            "JOIN group_company gc ON gc.id = bu.company_id"
        ),
        "default_where": "COALESCE(r.approval_status,'approved') = 'approved'",
        "dimensions": {
            "stage":       {"sql": _STAGE_CASE_SQL, "label": "Pipeline Stage", "type": "enum",
                             "options": STAGE_LABEL_ORDER},
            "status_raw":  {"sql": "a.status", "label": "Raw Status", "type": "enum"},
            "gender":      {"sql": "c.gender", "label": "Gender", "type": "enum",
                             "options": ["male", "female", "undisclosed"]},
            "candidate_source": {"sql": "COALESCE(c.source, 'unknown')", "label": "Candidate Source", "type": "string"},
            "company":     {"sql": "gc.name", "label": "Company", "type": "string"},
            "bu":          {"sql": "bu.name", "label": "Business Unit", "type": "string"},
            "applied_month": {"sql": "to_char(a.applied_at, 'YYYY-MM')", "label": "Applied Month", "type": "string"},
            "applied_year":  {"sql": "EXTRACT(year FROM a.applied_at)::int", "label": "Applied Year", "type": "number"},
        },
        "measures": {
            "count":             {"sql": "COUNT(*)", "label": "Applications"},
            "candidate_count":   {"sql": "COUNT(DISTINCT a.candidate_id)", "label": "Unique Candidates"},
            "avg_combined_score": {"sql": "ROUND(AVG(a.combined_score)::numeric, 1)", "label": "Avg Combined Score"},
            "avg_ai_fit_score":  {"sql": "ROUND(AVG(a.ai_fit_score)::numeric, 1)", "label": "Avg AI Fit Score"},
            "avg_ttf_days": {
                "sql": "ROUND(AVG(EXTRACT(EPOCH FROM (hire_evt.occurred_at - a.applied_at)) / 86400.0)::numeric, 1)",
                "label": "Avg Time-to-Hire (days)",
                "extra_join": "LEFT JOIN stage_event hire_evt ON hire_evt.application_id = a.id AND hire_evt.to_status = 'hired'",
            },
        },
        "filters": {
            "applied_at": {"sql": "a.applied_at", "type": "date", "ops": ["gte", "lte", "between"]},
            "stage":      {"sql": _STAGE_CASE_SQL, "type": "enum", "ops": ["eq", "in"], "options": STAGE_LABEL_ORDER},
            "gender":     {"sql": "c.gender", "type": "enum", "ops": ["eq", "in"],
                           "options": ["male", "female", "undisclosed"]},
            "company":    {"sql": "gc.name", "type": "string", "ops": ["eq"]},
            "bu":         {"sql": "bu.name", "type": "string", "ops": ["eq"]},
        },
        "raw_columns": [
            ("Candidate", "c.full_name"), ("Email", "c.email"), ("Requisition", "r.req_code"),
            ("Stage", _STAGE_CASE_SQL), ("Raw Status", "a.status"), ("Applied At", "a.applied_at"),
            ("Combined Score", "a.combined_score"), ("Company", "gc.name"), ("BU", "bu.name"),
            ("Gender", "c.gender"), ("Source", "COALESCE(c.source, 'unknown')"),
        ],
    },

    "interview": {
        "label": "Interviews",
        "from_sql": (
            "interview i "
            "JOIN application a ON a.id = i.application_id "
            "JOIN requisition r ON r.id = a.requisition_id "
            "JOIN candidate c ON c.id = a.candidate_id "
            "JOIN business_unit bu ON bu.id = r.bu_id "
            "JOIN group_company gc ON gc.id = bu.company_id"
        ),
        "dimensions": {
            "mode":     {"sql": "i.mode", "label": "Mode", "type": "enum",
                         "options": ["virtual", "in_person", "telephonic", "bot"]},
            "status":   {"sql": "i.status", "label": "Interview Status", "type": "enum",
                         "options": ["scheduled", "completed", "no_show", "cancelled"]},
            "company":  {"sql": "gc.name", "label": "Company", "type": "string"},
            "bu":       {"sql": "bu.name", "label": "Business Unit", "type": "string"},
            "scheduled_month": {"sql": "to_char(i.scheduled_at, 'YYYY-MM')", "label": "Scheduled Month", "type": "string"},
        },
        "measures": {
            "count":         {"sql": "COUNT(*)", "label": "Interviews"},
            "avg_duration":  {"sql": "ROUND(AVG(i.duration_min)::numeric, 0)", "label": "Avg Duration (min)"},
            "avg_overall_score": {
                # scalar subquery, not a JOIN — avoids row fan-out from multiple interviewers per interview
                "sql": "ROUND(AVG((SELECT AVG(sc.overall_score) FROM scorecard sc WHERE sc.interview_id = i.id))::numeric, 1)",
                "label": "Avg Scorecard Score",
            },
        },
        "filters": {
            "scheduled_at": {"sql": "i.scheduled_at", "type": "date", "ops": ["gte", "lte", "between"]},
            "mode":   {"sql": "i.mode", "type": "enum", "ops": ["eq", "in"],
                       "options": ["virtual", "in_person", "telephonic", "bot"]},
            "status": {"sql": "i.status", "type": "enum", "ops": ["eq", "in"],
                       "options": ["scheduled", "completed", "no_show", "cancelled"]},
            "company": {"sql": "gc.name", "type": "string", "ops": ["eq"]},
        },
        "raw_columns": [
            ("Candidate", "c.full_name"), ("Requisition", "r.req_code"), ("Mode", "i.mode"),
            ("Status", "i.status"), ("Scheduled At", "i.scheduled_at"), ("Duration (min)", "i.duration_min"),
            ("Company", "gc.name"), ("BU", "bu.name"),
        ],
    },

    "interview_reschedule": {
        "label": "Interview Reschedules",
        "from_sql": (
            "interview_reschedule irh "
            "JOIN interview i ON i.id = irh.interview_id "
            "JOIN application a ON a.id = irh.application_id "
            "JOIN requisition r ON r.id = a.requisition_id "
            "JOIN candidate c ON c.id = a.candidate_id "
            "JOIN business_unit bu ON bu.id = r.bu_id "
            "JOIN group_company gc ON gc.id = bu.company_id"
        ),
        "dimensions": {
            "requested_by": {"sql": "irh.requested_by", "label": "Requested By", "type": "enum",
                              "options": ["candidate", "panel", "staff"]},
            "candidate":    {"sql": "c.full_name", "label": "Candidate", "type": "string"},
            "requisition":  {"sql": "r.req_code", "label": "Requisition", "type": "string"},
            "company":      {"sql": "gc.name", "label": "Company", "type": "string"},
            "bu":           {"sql": "bu.name", "label": "Business Unit", "type": "string"},
            "reschedule_month": {"sql": "to_char(irh.created_at, 'YYYY-MM')", "label": "Reschedule Month", "type": "string"},
        },
        "measures": {
            "count": {"sql": "COUNT(*)", "label": "Reschedules"},
        },
        "filters": {
            "created_at":    {"sql": "irh.created_at", "type": "date", "ops": ["gte", "lte", "between"]},
            "requested_by":  {"sql": "irh.requested_by", "type": "enum", "ops": ["eq", "in"],
                               "options": ["candidate", "panel", "staff"]},
            "company":       {"sql": "gc.name", "type": "string", "ops": ["eq"]},
        },
        "raw_columns": [
            ("Candidate", "c.full_name"), ("Requisition", "r.req_code"), ("Requested By", "irh.requested_by"),
            ("Old Time", "irh.old_scheduled_at"), ("New Time", "irh.new_scheduled_at"),
            ("Rescheduled At", "irh.created_at"), ("Company", "gc.name"), ("BU", "bu.name"),
        ],
    },

    "offer": {
        "label": "Offers",
        "from_sql": (
            "offer o "
            "JOIN application a ON a.id = o.application_id "
            "JOIN requisition r ON r.id = a.requisition_id "
            "JOIN candidate c ON c.id = a.candidate_id "
            "JOIN business_unit bu ON bu.id = r.bu_id "
            "JOIN group_company gc ON gc.id = bu.company_id"
        ),
        "dimensions": {
            "status_bucket": {"sql": _OFFER_STATUS_BUCKET_SQL, "label": "Status", "type": "enum",
                               "options": ["pending", "approved", "rejected", "other"]},
            "company": {"sql": "gc.name", "label": "Company", "type": "string"},
            "bu":      {"sql": "bu.name", "label": "Business Unit", "type": "string"},
            "created_month": {"sql": "to_char(o.created_at, 'YYYY-MM')", "label": "Created Month", "type": "string"},
        },
        "measures": {
            "count":         {"sql": "COUNT(*)", "label": "Offers"},
            "avg_total_ctc": {"sql": "ROUND(AVG(o.total_ctc)::numeric, 0)", "label": "Avg Total CTC"},
            "sum_total_ctc": {"sql": "SUM(o.total_ctc)", "label": "Total CTC Offered"},
            "avg_approval_days": {
                "sql": "ROUND(AVG(EXTRACT(EPOCH FROM (oas.last_acted - o.created_at)) / 86400.0)::numeric, 1)",
                "label": "Avg Approval Days",
                "extra_join": (
                    "LEFT JOIN LATERAL ("
                    "  SELECT MAX(acted_at) AS last_acted FROM offer_approval_step"
                    "  WHERE offer_id = o.id AND acted_at IS NOT NULL"
                    ") oas ON true"
                ),
            },
        },
        "filters": {
            "created_at": {"sql": "o.created_at", "type": "date", "ops": ["gte", "lte", "between"]},
            "status_bucket": {"sql": _OFFER_STATUS_BUCKET_SQL, "type": "enum", "ops": ["eq", "in"],
                              "options": ["pending", "approved", "rejected", "other"]},
            "company": {"sql": "gc.name", "type": "string", "ops": ["eq"]},
        },
        "raw_columns": [
            ("Candidate", "c.full_name"), ("Requisition", "r.req_code"), ("Status", "o.status"),
            ("Total CTC", "o.total_ctc"), ("Created At", "o.created_at"), ("Company", "gc.name"),
        ],
    },

    "stage_event": {
        "label": "Stage Transitions (TAT)",
        "from_sql": (
            "stage_event se "
            "JOIN application a ON a.id = se.application_id "
            "JOIN requisition r ON r.id = a.requisition_id "
            "JOIN business_unit bu ON bu.id = r.bu_id "
            "JOIN group_company gc ON gc.id = bu.company_id "
            "LEFT JOIN app_user actor ON actor.id = se.actor_id"
        ),
        "dimensions": {
            "from_status": {"sql": "COALESCE(se.from_status, '(none)')", "label": "From Status", "type": "string"},
            "to_stage":    {"sql": _TO_STAGE_CASE_SQL, "label": "To Stage", "type": "enum", "options": STAGE_LABEL_ORDER},
            "occurred_month": {"sql": "to_char(se.occurred_at, 'YYYY-MM')", "label": "Occurred Month", "type": "string"},
            "actor":       {"sql": "COALESCE(actor.full_name, 'System')", "label": "Actor", "type": "string"},
            "company":     {"sql": "gc.name", "label": "Company", "type": "string"},
            "bu":          {"sql": "bu.name", "label": "Business Unit", "type": "string"},
        },
        "measures": {
            "count":     {"sql": "COUNT(*)", "label": "Transitions"},
            "app_count": {"sql": "COUNT(DISTINCT se.application_id)", "label": "Unique Applications"},
        },
        "filters": {
            "occurred_at": {"sql": "se.occurred_at", "type": "date", "ops": ["gte", "lte", "between"]},
            "to_stage":    {"sql": _TO_STAGE_CASE_SQL, "type": "enum", "ops": ["eq", "in"], "options": STAGE_LABEL_ORDER},
            "company":     {"sql": "gc.name", "type": "string", "ops": ["eq"]},
        },
        "raw_columns": [
            ("Requisition", "r.req_code"), ("From Status", "se.from_status"), ("To Status", "se.to_status"),
            ("Actor", "COALESCE(actor.full_name, 'System')"), ("Occurred At", "se.occurred_at"),
            ("Company", "gc.name"),
        ],
    },
}


def public_catalog() -> dict:
    """Client-safe projection — labels/types/options/ops only, never raw SQL."""
    out = {}
    for entity, explore in EXPLORES.items():
        out[entity] = {
            "label": explore["label"],
            "dimensions": [
                {"key": k, "label": d["label"], "type": d["type"], **({"options": d["options"]} if "options" in d else {})}
                for k, d in explore["dimensions"].items()
            ],
            "measures": [
                {"key": k, "label": m["label"]} for k, m in explore["measures"].items()
            ],
            "filters": [
                {
                    "key": k, "label": f.get("label", k), "type": f["type"], "ops": f["ops"],
                    **({"options": f["options"]} if "options" in f else {}),
                }
                for k, f in explore["filters"].items()
            ],
            "raw_columns": [label for label, _ in explore["raw_columns"]],
        }
    return out
