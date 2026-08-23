"""
No-Poach List — admin-managed list of companies whose current employees may
not be sourced/hired, plus a view into which candidate applications match it.

The underlying table (no_poach_company) and the matching logic that stamps
application.flags->'no_poach' at intake time already existed
(see services/pipeline.py::_flag_no_poach_and_rehire). This router adds the
CRUD/upload surface that was missing: list, add, edit, deactivate, bulk
upload from Excel/CSV, and "show me the applications that match this
company" so recruiters can act on a hit directly from the list.

Roles: view (list/search/applications) = ta_manager, recruiter, admin.
       manage (add/edit/upload) = ta_manager, admin only.
"""
import io
import re

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from ..auth_utils import get_current_user
from ..db import query, query_one

router = APIRouter(prefix="/api/no-poach", tags=["no-poach"])

_VIEW_ROLES = {"ta_manager", "recruiter", "admin"}
_MANAGE_ROLES = {"ta_manager", "admin"}


def _require_view(user: dict):
    if user["role"] not in _VIEW_ROLES:
        raise HTTPException(403, "Not authorised")


def _require_manage(user: dict):
    if user["role"] not in _MANAGE_ROLES:
        raise HTTPException(403, "No-Poach List: ta_manager / admin only")


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class CompanyIn(BaseModel):
    company_name: str
    status: str | None = None
    location: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None


class CompanyPatch(BaseModel):
    company_name: str | None = None
    status: str | None = None
    location: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    is_active: bool | None = None


class BulkAction(BaseModel):
    ids: list[str]
    action: str  # 'deactivate' | 'reactivate' | 'set_current' | 'set_past' | 'delete'


@router.get("/stats")
async def stats(user: dict = Depends(get_current_user)):
    _require_view(user)
    row = query_one(
        """SELECT
             count(*) FILTER (WHERE is_active) AS active,
             count(*) FILTER (WHERE is_active AND status = 'current') AS current,
             count(*) FILTER (WHERE is_active AND status = 'past') AS past,
             count(*) AS total
           FROM no_poach_company"""
    )
    return row


@router.get("/list")
async def list_companies(
    q: str = Query(""),
    status: str = Query(""),
    active: str = Query("active"),
    user: dict = Depends(get_current_user),
):
    _require_view(user)
    where = []
    params = []
    if q.strip():
        where.append("company_name ILIKE %s")
        params.append(f"%{q.strip()}%")
    if status in ("past", "current"):
        where.append("status = %s")
        params.append(status)
    if active == "active":
        where.append("is_active = true")
    elif active == "inactive":
        where.append("is_active = false")
    # active == "all" -> no filter
    sql = "SELECT * FROM no_poach_company"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY company_name ASC LIMIT 500"
    return query(sql, params)


@router.post("", status_code=201)
async def add_company(body: CompanyIn, user: dict = Depends(get_current_user)):
    _require_manage(user)
    name = body.company_name.strip()
    if not name:
        raise HTTPException(400, "company_name is required")
    if body.status and body.status not in ("past", "current"):
        raise HTTPException(400, "status must be 'past' or 'current'")
    norm = _normalize(name)

    dup = query_one(
        "SELECT company_name, is_active FROM no_poach_company WHERE normalized_name = %s", [norm]
    )
    if dup:
        if dup["is_active"]:
            raise HTTPException(409, f"'{dup['company_name']}' is already on the no-poach list.")
        raise HTTPException(
            409,
            f"'{dup['company_name']}' already exists but is inactive — reactivate or edit it "
            "instead of adding it again.",
        )

    row = query_one(
        """INSERT INTO no_poach_company
             (company_name, normalized_name, status, location, effective_from, effective_to, source)
           VALUES (%s, %s, %s, %s, %s, %s, 'manual')
           RETURNING *""",
        [name, norm, body.status, body.location, body.effective_from, body.effective_to],
    )
    return row


@router.patch("/{company_id}")
async def edit_company(company_id: str, body: CompanyPatch, user: dict = Depends(get_current_user)):
    _require_manage(user)
    existing = query_one("SELECT * FROM no_poach_company WHERE id = %s", [company_id])
    if not existing:
        raise HTTPException(404, "Company not found")
    if body.status and body.status not in ("past", "current"):
        raise HTTPException(400, "status must be 'past' or 'current'")

    fields, params = [], []
    if body.company_name is not None and body.company_name.strip():
        new_norm = _normalize(body.company_name)
        dup = query_one(
            "SELECT company_name FROM no_poach_company WHERE normalized_name = %s AND id <> %s",
            [new_norm, company_id],
        )
        if dup:
            raise HTTPException(409, f"'{dup['company_name']}' already uses that name.")
        fields.append("company_name = %s"); params.append(body.company_name.strip())
        fields.append("normalized_name = %s"); params.append(new_norm)
    if body.status is not None:
        fields.append("status = %s"); params.append(body.status)
    if body.location is not None:
        fields.append("location = %s"); params.append(body.location)
    if body.effective_from is not None:
        fields.append("effective_from = %s"); params.append(body.effective_from or None)
    if body.effective_to is not None:
        fields.append("effective_to = %s"); params.append(body.effective_to or None)
    if body.is_active is not None:
        fields.append("is_active = %s"); params.append(body.is_active)

    if not fields:
        return existing

    params.append(company_id)
    row = query_one(
        f"UPDATE no_poach_company SET {', '.join(fields)} WHERE id = %s RETURNING *",
        params,
    )
    return row


@router.post("/bulk")
async def bulk_action(body: BulkAction, user: dict = Depends(get_current_user)):
    _require_manage(user)
    if not body.ids:
        raise HTTPException(400, "No companies selected")

    if body.action == "deactivate":
        query("UPDATE no_poach_company SET is_active = false WHERE id = ANY(%s::uuid[])",
              [body.ids], fetch=False)
    elif body.action == "reactivate":
        query("UPDATE no_poach_company SET is_active = true WHERE id = ANY(%s::uuid[])",
              [body.ids], fetch=False)
    elif body.action == "set_current":
        query("UPDATE no_poach_company SET status = 'current' WHERE id = ANY(%s::uuid[])",
              [body.ids], fetch=False)
    elif body.action == "set_past":
        query("UPDATE no_poach_company SET status = 'past' WHERE id = ANY(%s::uuid[])",
              [body.ids], fetch=False)
    elif body.action == "delete":
        query("DELETE FROM no_poach_company WHERE id = ANY(%s::uuid[])",
              [body.ids], fetch=False)
    else:
        raise HTTPException(400, f"Unknown action '{body.action}'")

    return {"affected": len(body.ids), "action": body.action}


@router.get("/{company_id}/applications")
async def matching_applications(company_id: str, user: dict = Depends(get_current_user)):
    """Live match against application.current_company — not the frozen intake-time flag."""
    _require_view(user)
    company = query_one("SELECT * FROM no_poach_company WHERE id = %s", [company_id])
    if not company:
        raise HTTPException(404, "Company not found")
    if not company["normalized_name"]:
        return []
    return query(
        """SELECT a.id AS application_id, a.status, a.applied_at, a.current_company,
                  a.current_designation, c.id AS candidate_id, c.full_name, c.email, c.phone,
                  r.id AS requisition_id, r.req_code, r.title AS requisition_title
           FROM application a
           JOIN candidate c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE regexp_replace(lower(a.current_company), '[^a-z0-9]', '', 'g') = %s
           ORDER BY a.applied_at DESC""",
        [company["normalized_name"]],
    )


@router.post("/upload")
async def upload_list(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """
    Upsert from .xlsx/.xls/.csv. Re-uploading the same or a refreshed file is
    always safe -- rows are matched on normalized company name and updated in
    place, so this is the "list can be refreshed any time" entry point.

    Recognised columns (header row required, case-insensitive):
      company_name (required), status (past|current), location,
      effective_from, effective_to (YYYY-MM-DD)
    """
    _require_manage(user)
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(400, "Only .xlsx, .xls or .csv files are accepted")

    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large — maximum 10 MB")

    rows = _parse_rows(data, file.filename)
    if not rows:
        raise HTTPException(400, "No data rows found (or missing a 'company_name' column)")

    inserted = updated = skipped = 0
    errors: list[str] = []
    duplicates_in_file: list[str] = []
    seen_in_file = set()

    for i, row in enumerate(rows, start=2):
        name = (row.get("company_name") or "").strip()
        if not name:
            skipped += 1
            continue
        status = (row.get("status") or "").strip().lower() or None
        if status and status not in ("past", "current"):
            errors.append(f"Row {i}: invalid status '{status}' (must be past/current) — skipped")
            skipped += 1
            continue
        norm = _normalize(name)
        if not norm:
            skipped += 1
            continue
        if norm in seen_in_file:
            duplicates_in_file.append(name)
            skipped += 1
            continue
        seen_in_file.add(norm)

        # A file with no status column shouldn't leave a *new* company with a
        # blank status -- default brand-new rows to 'current' (matches the
        # existing CLI importer's behaviour). Re-uploads that omit status must
        # NOT stomp an existing row's status back to null/current, so this
        # only applies when the company doesn't exist yet.
        existing = query_one(
            "SELECT id FROM no_poach_company WHERE normalized_name = %s", [norm]
        )
        effective_status = status or ("current" if not existing else None)

        result = query_one(
            """INSERT INTO no_poach_company
                 (company_name, normalized_name, status, location, effective_from, effective_to, source)
               VALUES (%s, %s, %s, %s, %s, %s, 'upload')
               ON CONFLICT (normalized_name) DO UPDATE
                 SET company_name   = EXCLUDED.company_name,
                     status         = COALESCE(EXCLUDED.status, no_poach_company.status),
                     location       = COALESCE(EXCLUDED.location, no_poach_company.location),
                     effective_from = COALESCE(EXCLUDED.effective_from, no_poach_company.effective_from),
                     effective_to   = COALESCE(EXCLUDED.effective_to, no_poach_company.effective_to),
                     is_active      = true
               RETURNING (xmax = 0) AS was_insert""",
            [name, norm, effective_status, row.get("location") or None,
             row.get("effective_from") or None, row.get("effective_to") or None],
        )
        if result["was_insert"]:
            inserted += 1
        else:
            updated += 1

    if duplicates_in_file:
        errors.append(
            "Duplicate company name(s) within the file (only the first occurrence "
            "of each was kept): " + ", ".join(duplicates_in_file)
        )

    return {"total_rows": len(rows), "inserted": inserted, "updated": updated,
            "skipped": skipped, "errors": errors}


def _parse_rows(data: bytes, filename: str) -> list[dict]:
    if filename.lower().endswith(".csv"):
        return _parse_csv(data)
    return _parse_excel(data)


_HEADER_ALIASES = {
    "company_name": (
        "company_name", "company name", "company", "name of the company",
        "employer", "employer name", "organisation", "organization",
        "vendor name", "client name", "name of company",
    ),
    "status": ("status", "employment status", "employer status"),
    "location": (
        "location", "location of the company", "city", "place",
        "company location", "office location",
    ),
    "effective_from": ("effective_from", "effective from", "start date", "from date", "from"),
    "effective_to": ("effective_to", "effective to", "end date", "to date", "to"),
}

# Fallback substring matching for headers that don't exactly match an alias
# above but clearly express the same idea (e.g. "Name of the Company (Pvt.)").
_HEADER_CONTAINS = {
    "company_name": ("company", "employer", "vendor", "organisation", "organization"),
    "location": ("location", "city"),
}


def _find_header_indices(header_row) -> dict:
    idx = {}
    for i, cell in enumerate(header_row):
        if cell is None:
            continue
        key = str(cell).strip().lower()
        if not key:
            continue
        for canonical, names in _HEADER_ALIASES.items():
            if canonical not in idx and key in names:
                idx[canonical] = i
    # Second pass: substring fallback, only for columns not already resolved,
    # and skip columns already claimed by an exact match on another field.
    claimed = set(idx.values())
    for i, cell in enumerate(header_row):
        if cell is None or i in claimed:
            continue
        key = str(cell).strip().lower()
        if not key:
            continue
        for canonical, needles in _HEADER_CONTAINS.items():
            if canonical not in idx and any(n in key for n in needles):
                idx[canonical] = i
                claimed.add(i)
    return idx


def _parse_excel(data: bytes) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        ws = wb.active
        raw_rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    if not raw_rows:
        return []
    idx = _find_header_indices(raw_rows[0])
    if "company_name" not in idx:
        return []
    out = []
    for raw_row in raw_rows[1:]:
        if raw_row is None or all(v is None or str(v).strip() == "" for v in raw_row):
            continue
        out.append(_row_dict(raw_row, idx))
    return out


def _parse_csv(data: bytes) -> list[dict]:
    import csv
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    all_rows = list(reader)
    if not all_rows:
        return []
    idx = _find_header_indices(all_rows[0])
    if "company_name" not in idx:
        return []
    out = []
    for raw_row in all_rows[1:]:
        if not raw_row or all(not str(v).strip() for v in raw_row):
            continue
        out.append(_row_dict(raw_row, idx))
    return out


def _row_dict(raw_row, idx: dict) -> dict:
    def cell(key):
        i = idx.get(key)
        if i is None or i >= len(raw_row):
            return None
        v = raw_row[i]
        return str(v).strip() if v is not None else None
    return {
        "company_name": cell("company_name"),
        "status": cell("status"),
        "location": cell("location"),
        "effective_from": cell("effective_from"),
        "effective_to": cell("effective_to"),
    }
