"""
Custom Reports — a curated, filterable report builder covering requisitions,
applications/pipeline, interviews, offers, and stage transitions.

Safe by construction: every request is validated against the fixed catalog
in services/report_catalog.py before any SQL is built (see
services/report_query_builder.py) — the client can only ever reference
catalog keys, never raw SQL, and role-scoping is applied unconditionally
server-side before any client filter.

The Pipeline Funnel is intentionally NOT a separate endpoint here — it is
the "funnel" template below, a plain instance of the application/stage/count
report. See TEMPLATES.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import query
from ..auth_utils import get_current_user
from ..services import excel_export
from ..services.report_catalog import EXPLORES, public_catalog
from ..services.report_query_builder import validate_spec, build_query
from ..services.sla import PIPELINE_STAGES, PIPELINE_STAGE_LABELS

router = APIRouter(prefix="/api/custom-reports", tags=["custom_reports"])

_ALLOWED_ROLES = ("ta_manager", "admin", "recruiter", "hiring_manager")


class ReportSpec(BaseModel):
    entity: str
    dimensions: list[str] = []
    measures: list[dict] = []
    filters: list[dict] = []
    sort: dict | None = None
    limit: int | None = None
    raw_mode: bool = False


TEMPLATES = {
    "funnel": {
        "label": "Pipeline Funnel",
        "spec": {
            "entity": "application",
            "dimensions": ["stage"],
            "measures": [{"key": "count"}],
            "filters": [],
            "raw_mode": False,
        },
    },
    "diversity": {
        "label": "Diversity Hiring",
        "spec": {
            "entity": "application",
            "dimensions": ["gender"],
            "measures": [{"key": "count"}],
            "filters": [],
            "raw_mode": False,
        },
    },
    "recruiter_productivity": {
        "label": "Recruiter Productivity",
        "spec": {
            "entity": "application",
            "dimensions": ["applied_month"],
            "measures": [{"key": "count"}, {"key": "candidate_count"}],
            "filters": [],
            "raw_mode": False,
        },
    },
    "offer_pipeline": {
        "label": "Offer Pipeline",
        "spec": {
            "entity": "offer",
            "dimensions": ["status_bucket"],
            "measures": [{"key": "count"}, {"key": "avg_total_ctc"}],
            "filters": [],
            "raw_mode": False,
        },
    },
}


def _check_role(user: dict):
    if user["role"] not in _ALLOWED_ROLES:
        raise HTTPException(403, "Not authorized for Custom Reports")


def _is_funnel_spec(spec: dict) -> bool:
    return (
        spec["entity"] == "application"
        and not spec["raw_mode"]
        and spec["dimensions"] == ["stage"]
    )


def _with_conversion_pct(spec: dict, columns: list, rows: list):
    """
    The one deliberately special-cased piece of logic in an otherwise fully
    generic query engine: stage ORDER is business knowledge no generic
    aggregator can infer from the catalog. Ports the exact loop already
    used in kpi_api.kpi_dashboard's funnel block.
    """
    if not _is_funnel_spec(spec):
        return columns, rows

    order = [PIPELINE_STAGE_LABELS[s] for s in PIPELINE_STAGES]
    by_stage = {r["stage"]: r for r in rows}
    ordered_rows = []
    prev_count = None
    for label in order:
        row = by_stage.get(label, {"stage": label, "count": 0})
        count = row.get("count") or 0
        conv_pct = round(count / prev_count * 100, 1) if prev_count else None
        ordered_rows.append({**row, "count": count, "conv_pct": conv_pct})
        if count > 0:
            prev_count = count
    # Preserve any "Rejected/Other" bucket at the end, without a conv_pct
    if "Rejected/Other" in by_stage:
        ordered_rows.append({**by_stage["Rejected/Other"], "conv_pct": None})

    return columns + [{"key": "conv_pct", "label": "Conversion %", "type": "number"}], ordered_rows


def _execute_spec(spec: dict, user: dict, *, for_excel: bool):
    validated = validate_spec(spec, for_excel=for_excel)
    sql, params, columns = build_query(validated, user)
    rows = query(sql, params) or []
    columns, rows = _with_conversion_pct(validated, columns, rows)
    return validated, columns, rows


@router.get("/catalog")
def get_catalog(user: dict = Depends(get_current_user)):
    _check_role(user)
    return public_catalog()


@router.get("/templates")
def get_templates(user: dict = Depends(get_current_user)):
    _check_role(user)
    return {key: {"label": t["label"], "spec": t["spec"]} for key, t in TEMPLATES.items()}


@router.post("/run")
def run_report(spec: ReportSpec, user: dict = Depends(get_current_user)):
    _check_role(user)
    validated, columns, rows = _execute_spec(spec.dict(), user, for_excel=False)
    truncated = len(rows) >= validated["limit"]
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "meta": {
            "entity": validated["entity"],
            "generated_at": datetime.now().isoformat(),
            "generated_by": user.get("name") or user.get("email") or "",
            "role": user["role"],
            "filters_applied": validated["filters"],
        },
    }


@router.get("/excel")
def excel_report(spec: str = Query(..., description="URL-encoded JSON ReportSpec"), user: dict = Depends(get_current_user)):
    import json

    _check_role(user)
    try:
        raw_spec = json.loads(spec)
    except Exception:
        raise HTTPException(400, "spec must be valid JSON")

    validated, columns, rows = _execute_spec(raw_spec, user, for_excel=True)
    entity_label = EXPLORES[validated["entity"]]["label"]

    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    excel_export.sheet_from_rows(wb, "Data", rows, columns=[(c["label"], c["key"]) for c in columns])
    excel_export.build_summary_sheet(
        wb,
        title=f"Custom Report — {entity_label}",
        generated_by=user.get("name") or user.get("email") or "",
        generated_at=datetime.now(),
        filters_applied=validated["filters"],
        rows=rows,
        measures_meta=[{"key": c["key"], "label": c["label"]} for c in columns if c["type"] == "number"],
    )
    return excel_export.stream_workbook(wb, f"custom_report_{validated['entity']}.xlsx")
