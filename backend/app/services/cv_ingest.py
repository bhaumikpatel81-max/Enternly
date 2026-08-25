"""
Shared CV-repository ingest helper.

Originally lived only inside routers/cv_api.py (bulk-folder scan + the
per-recruiter IMAP "Scan My Email" endpoint). Pulled out into a service
module so the new background recruiter-mailbox poller
(services/recruiter_email_worker.py) can reuse the exact same
hash-dedup/extract/auto-map/store logic without a router importing another
router.
"""
import os
import uuid
from pathlib import Path
from typing import Optional

from ..db import query, query_one
from . import cv_parser as _parser

_CV_STORE = os.environ.get("CV_STORE_DIR", "/app/cv_store")

_SCAN_PAUSED_KEY = "cv_scan_paused"


def is_scan_paused() -> bool:
    """
    Global kill-switch for ALL CV-ingestion scanning — the per-recruiter
    IMAP poller (recruiter_email_worker.py), manual "Scan My Email", and
    "Scan Ingest Folder" all check this. Stopping one in-flight job only
    stops that job; this stops every scan path at once, including the
    automatic background poller that keeps running on its own timer
    regardless of what's happening in any single job's progress modal.
    """
    row = query_one("SELECT value FROM system_status WHERE key=%s", [_SCAN_PAUSED_KEY])
    return bool(row and row.get("value") == "true")


def set_scan_paused(paused: bool) -> None:
    query(
        """INSERT INTO system_status (key, value) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        [_SCAN_PAUSED_KEY, "true" if paused else "false"],
        fetch=False,
    )


def ingest_one(
    data: bytes,
    filename: str,
    source: str,
    uploaded_by: Optional[str],
    raw_text: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> dict:
    """
    Process one file: hash-check, extract, map, store.
    Returns a status dict: {status:'ok'|'duplicate'|'error', cv_id, mapped}.

    raw_text: pass the already-extracted text (e.g. the caller already ran
    extract_text() to classify the attachment) to avoid re-parsing the same
    PDF/DOCX a second time. None means "extract it here" (existing callers).

    tenant_id: whose CV repository this belongs to. Every interactive caller
    (upload, folder scan, recruiter mailbox poll) has an authenticated actor
    and should pass theirs; None falls back to the seed tenant via the
    column's own DB default (Migration 96), for the one caller — the plain
    Gmail CV-ingest poller in email_ingest.py — that resolves its own
    per-account tenant separately rather than through this parameter.
    """
    os.makedirs(_CV_STORE, exist_ok=True)

    ext = Path(filename).suffix.lower().lstrip(".")
    file_hash = _parser.sha256_hash(data)

    if tenant_id:
        existing = query_one(
            "SELECT id FROM cv_repository WHERE file_hash=%s AND tenant_id=%s", [file_hash, tenant_id]
        )
    else:
        existing = query_one(
            "SELECT id FROM cv_repository WHERE file_hash=%s", [file_hash]
        )
    if existing:
        return {"status": "duplicate", "filename": filename}

    if raw_text is None:
        raw_text = _parser.extract_text(data, ext)
    skills = _parser.extract_tier1_skills(raw_text)
    name   = _parser.parse_candidate_name(filename)

    cv_id   = str(uuid.uuid4())
    dest    = os.path.join(_CV_STORE, f"{cv_id}.{ext}")
    with open(dest, "wb") as f:
        f.write(data)

    # Auto-map by normalised full_name
    candidate_id = req_id = None
    map_status = "pool"
    if name:
        if tenant_id:
            cand = query_one(
                "SELECT id FROM candidate WHERE LOWER(TRIM(full_name)) = LOWER(TRIM(%s)) AND tenant_id=%s LIMIT 1",
                [name, tenant_id],
            )
        else:
            cand = query_one(
                "SELECT id FROM candidate WHERE LOWER(TRIM(full_name)) = LOWER(TRIM(%s)) LIMIT 1",
                [name],
            )
        if cand:
            candidate_id = str(cand["id"])
            app_row = query_one(
                """SELECT requisition_id FROM application
                   WHERE candidate_id=%s ORDER BY applied_at DESC LIMIT 1""",
                [candidate_id],
            )
            req_id     = str(app_row["requisition_id"]) if app_row and app_row["requisition_id"] else None
            map_status = "mapped"

    # cv_enricher.py's background queue explicitly skips rows with empty
    # raw_text (nothing for the LLM to read) and never revisits them — left
    # at the table's default enrich_status='pending', such a row would show
    # a permanent, misleading "AI processing…" in the UI that never
    # resolves. There's genuinely nothing to enrich, so mark it done (with
    # no extracted fields) right away; the existing low_content flag in
    # cv_api.py's search response already renders that state correctly
    # ("AI: no data") instead of a perpetual spinner.
    has_text = bool((raw_text or "").strip())
    enrich_status = "done" if not has_text else "pending"
    enriched_at_sql = "now()" if not has_text else "NULL"

    tenant_col, tenant_ph, tenant_val = ("tenant_id, ", "%s, ", [tenant_id]) if tenant_id else ("", "", [])
    query(
        f"""INSERT INTO cv_repository
           ({tenant_col}id, file_name, file_path, file_hash, file_ext, candidate_name,
            candidate_id, requisition_id, map_status, raw_text,
            text_vector, skills, source, uploaded_by, enrich_status, enriched_at)
           VALUES ({tenant_ph}%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                   to_tsvector('english', %s), %s, %s, %s, %s, {enriched_at_sql})""",
        [*tenant_val, cv_id, filename, dest, file_hash, ext, name,
         candidate_id, req_id, map_status, raw_text,
         raw_text or "", skills, source, uploaded_by, enrich_status],
        fetch=False,
    )

    # Attach to candidate if they have no CV yet
    if candidate_id:
        query(
            """UPDATE candidate SET cv_repository_id=%s
               WHERE id=%s AND cv_repository_id IS NULL""",
            [cv_id, candidate_id],
            fetch=False,
        )

    return {"status": "ok", "cv_id": cv_id, "mapped": map_status == "mapped"}
