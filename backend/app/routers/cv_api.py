"""
CV Repository — bulk ingest, search, file serve, API-token management.

Roles: ta_manager, recruiter, admin only (others → 403).
Auth: standard JWT OR long-lived API token (for the watcher script).
"""
import asyncio
import io
import os
import re
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from ..auth_utils import _decode, get_current_user
from ..db import query, query_one
from ..services import cv_parser as _parser
from ..services.cv_ingest import ingest_one as _ingest_one
from ..services.storage import get_storage, storage_response

from ..module_access import require_tenant_module

router = APIRouter(prefix="/api/cv", tags=["cv-repository"],
                    dependencies=[Depends(require_tenant_module("cv_repository"))])

_ALLOWED    = {"ta_manager", "recruiter", "admin"}
_bearer     = HTTPBearer(auto_error=False)
_SUPPORTED  = {".pdf", ".docx", ".doc"}
_CV_STORE   = os.environ.get("CV_STORE_DIR", "/app/cv_store")


def _cv_storage():
    return get_storage("cv", local_env_var="CV_STORE_DIR", local_default=_CV_STORE)


# ── Auth: JWT or long-lived API token ────────────────────────────────────────

def _cv_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: Optional[str] = None,
) -> dict:
    # Query-param fallback lets the frontend open an authenticated file
    # (view/download) in a new browser tab via a plain <a target="_blank">
    # link, where no custom Authorization header can be attached.
    token = creds.credentials if creds else token
    if not token:
        raise HTTPException(401, "Not authenticated")
    # Try JWT first
    try:
        payload = _decode(token)
        if payload.get("role") in _ALLOWED:
            return payload
        raise HTTPException(403, "CV Repository: ta_manager / recruiter / admin only")
    except HTTPException:
        raise
    except Exception:
        pass
    # Try long-lived API token
    row = query_one(
        "SELECT id, email, role, full_name FROM app_user WHERE api_token=%s",
        [token],
    )
    if row and row["role"] in _ALLOWED:
        return {"sub": str(row["id"]), "email": row["email"],
                "role": row["role"], "name": row["full_name"]}
    raise HTTPException(401, "Invalid or expired token")


def _require(user: dict):
    if user.get("role") not in _ALLOWED:
        raise HTTPException(403, "CV Repository: ta_manager / recruiter / admin only")


# ── Boolean query → tsquery ───────────────────────────────────────────────────

def _to_tsquery(raw: str) -> Optional[str]:
    """
    Convert user boolean search string to PostgreSQL tsquery.
    Supports: AND, OR, NOT keywords; "quoted phrases"; parentheses.
    Raises ValueError with a friendly message on syntax error.
    Returns None if query is empty.
    """
    q = raw.strip()
    if not q:
        return None

    # Phase 1: extract quoted phrases → phrase placeholders
    _phrases: list[str] = []

    def _repl_phrase(m: re.Match) -> str:
        words = m.group(1).split()
        if not words:
            return ""
        idx = len(_phrases)
        _phrases.append("(" + " <-> ".join(w.lower() for w in words) + ")")
        return f"__P{idx}__"

    q = re.sub(r'"([^"]*)"', _repl_phrase, q)

    # Phase 2: keyword operators
    q = re.sub(r'\bAND\b', "&", q, flags=re.IGNORECASE)
    q = re.sub(r'\bOR\b',  "|", q, flags=re.IGNORECASE)
    q = re.sub(r'\bNOT\b', "!", q, flags=re.IGNORECASE)

    # Phase 3: tokenize
    raw_tokens = re.split(r'([&|!()\s])', q)
    tokens = [t.strip() for t in raw_tokens if t and t.strip()]

    # Phase 4: build output with implicit & between adjacent value tokens
    _OPS = frozenset({"&", "|", "!", "(", ")"})
    out: list[str] = []
    for tok in tokens:
        if out:
            prev = out[-1]
            prev_ends_value = prev not in _OPS or prev == ")"
            tok_starts_value = tok not in _OPS or tok in ("(", "!")
            if prev_ends_value and tok_starts_value:
                out.append("&")
        out.append(tok.lower() if tok not in _OPS and not tok.startswith("__P") else tok)

    # Phase 5: validation
    depth = 0
    for i, tok in enumerate(out):
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("Unmatched ')' — check your parentheses")
        if i == 0 and tok in ("&", "|"):
            raise ValueError("Query cannot start with AND or OR")
        if i == len(out) - 1 and tok in ("&", "|"):
            raise ValueError("Query cannot end with AND or OR")
        if tok in ("&", "|") and i + 1 < len(out) and out[i + 1] in ("&", "|"):
            raise ValueError("Consecutive AND/OR operators are not allowed")
        if tok in ("&", "|") and i + 1 < len(out) and out[i + 1] == ")":
            raise ValueError("Operator before ')' is not allowed")
        if tok == "(" and i + 1 < len(out) and out[i + 1] in ("&", "|"):
            raise ValueError("Operator immediately after '(' is not allowed")

    if depth != 0:
        raise ValueError("Unmatched '(' — check your parentheses")
    if not out:
        return None

    result = " ".join(out)
    for i, ph in enumerate(_phrases):
        result = result.replace(f"__p{i}__", ph).replace(f"__P{i}__", ph)
    return result


# ── Ingest helpers ────────────────────────────────────────────────────────────
# _ingest_one now lives in services/cv_ingest.py (imported above) so the
# background recruiter-mailbox poller can share it without importing this
# router module.


def ingest_and_link(
    data: bytes,
    filename: str,
    source: str,
    uploaded_by: Optional[str],
    candidate_id: str,
    req_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> dict:
    """
    Ingest a resume file and hard-link it to a known candidate + requisition.
    Always produces map_status='mapped'. Hash-deduplication still applies —
    if the same bytes already exist, the existing row is re-linked if unlinked.
    Returns: {status:'ok'|'duplicate'|'error', cv_id, mapped:True}.
    """
    ext = Path(filename).suffix.lower().lstrip(".")
    if not ext:
        ext = "bin"
    file_hash = _parser.sha256_hash(data)

    if tenant_id:
        existing = query_one(
            "SELECT id, candidate_id FROM cv_repository WHERE file_hash=%s AND tenant_id=%s", [file_hash, tenant_id]
        )
    else:
        existing = query_one(
            "SELECT id, candidate_id FROM cv_repository WHERE file_hash=%s", [file_hash]
        )
    if existing:
        # Existing row — update link if it's currently unmapped
        if not existing["candidate_id"] and candidate_id:
            query(
                """UPDATE cv_repository
                   SET candidate_id=%s, requisition_id=%s, map_status='mapped'
                   WHERE id=%s""",
                [candidate_id, req_id, str(existing["id"])],
                fetch=False,
            )
            query(
                "UPDATE candidate SET cv_repository_id=%s WHERE id=%s AND cv_repository_id IS NULL",
                [str(existing["id"]), candidate_id],
                fetch=False,
            )
        return {"status": "duplicate", "cv_id": str(existing["id"]), "mapped": True, "filename": filename}

    raw_text = _parser.extract_text(data, ext)
    skills   = _parser.extract_tier1_skills(raw_text)
    name     = _parser.parse_candidate_name(filename)

    cv_id = str(uuid.uuid4())
    dest  = _cv_storage().save(f"{cv_id}.{ext}", data)

    tenant_col, tenant_ph, tenant_val = ("tenant_id, ", "%s, ", [tenant_id]) if tenant_id else ("", "", [])
    query(
        f"""INSERT INTO cv_repository
           ({tenant_col}id, file_name, file_path, file_hash, file_ext, candidate_name,
            candidate_id, requisition_id, map_status, raw_text,
            text_vector, skills, source, uploaded_by)
           VALUES ({tenant_ph}%s,%s,%s,%s,%s,%s,%s,%s,'mapped',%s,
                   to_tsvector('english', %s), %s, %s, %s)""",
        [*tenant_val, cv_id, filename, dest, file_hash, ext, name,
         candidate_id, req_id, raw_text,
         raw_text or "", skills, source, uploaded_by],
        fetch=False,
    )

    query(
        "UPDATE candidate SET cv_repository_id=%s WHERE id=%s AND cv_repository_id IS NULL",
        [cv_id, candidate_id],
        fetch=False,
    )

    return {"status": "ok", "cv_id": cv_id, "mapped": True}


def _job_cancel_requested(job_id: str) -> bool:
    from ..services.cv_ingest import is_scan_paused
    # The global master-stop pauses every job automatically, not just ones
    # someone remembered to individually cancel.
    if is_scan_paused():
        return True
    row = query_one("SELECT cancel_requested FROM cv_ingest_jobs WHERE id=%s", [job_id])
    return bool(row and row.get("cancel_requested"))


def _run_ingest_job(job_id: str, folder: str, uploaded_by: Optional[str], tenant_id: Optional[str] = None):
    """Background worker for scan-folder ingestion."""
    paths: list[Path] = []
    for root, _, files in os.walk(folder):
        for fn in files:
            if Path(fn).suffix.lower() in _SUPPORTED:
                paths.append(Path(root) / fn)

    query(
        "UPDATE cv_ingest_jobs SET total=%s WHERE id=%s",
        [len(paths), job_id], fetch=False,
    )

    processed = mapped = pooled = duplicates = skipped = 0
    errors: list[dict] = []
    cancelled = False

    for p in paths:
        if _job_cancel_requested(job_id):
            cancelled = True
            break
        try:
            data = p.read_bytes()
            ext = p.suffix.lower().lstrip(".")

            # A folder used as a "CV inbox" collects more than just resumes
            # in practice (ID scans, payslips, bank statements, offer
            # letters, plain photos) — every file goes through the same
            # keyword-pre-filter + thorough AI classify_and_enrich() gate
            # as the email scan and manual upload, so nothing gets stored
            # on a filename/keyword heuristic alone.
            raw_text = _parser.extract_text(data, ext)

            from ..services.cv_enricher import classify_and_enrich_sync
            accept, reason, llm_result = classify_and_enrich_sync(raw_text, p.name)
            if not accept:
                skipped += 1
                errors.append({"file": p.name, "error": f"skipped (not a CV — {reason})"})
                continue

            r = _ingest_one(data, p.name, "bulk_folder", uploaded_by, raw_text=raw_text, tenant_id=tenant_id)
            if r["status"] == "duplicate":
                duplicates += 1
            elif r["status"] == "ok":
                processed += 1
                if r["mapped"]:
                    mapped += 1
                else:
                    pooled += 1
                if llm_result and llm_result.get("is_resume"):
                    from ..services.cv_enricher import _persist_enrichment_result
                    _persist_enrichment_result(r["cv_id"], llm_result)
            else:
                errors.append({"file": p.name, "error": r.get("error", "unknown")})
        except Exception as exc:
            errors.append({"file": p.name, "error": str(exc)})
        finally:
            # In a `finally` (not just after the try/except) so the "not a
            # CV" branch's `continue` still persists its skip — otherwise
            # that file's progress update never runs and a skip right
            # before the loop ends could silently vanish from the job row.
            import json
            query(
                """UPDATE cv_ingest_jobs
                   SET processed=%s, mapped=%s, pooled=%s, duplicates=%s, skipped=%s, errors=%s::jsonb
                   WHERE id=%s""",
                [processed, mapped, pooled, duplicates, skipped,
                 json.dumps(errors), job_id],
                fetch=False,
            )

    query(
        "UPDATE cv_ingest_jobs SET status=%s WHERE id=%s",
        ["cancelled" if cancelled else "done", job_id], fetch=False,
    )


# ── Combined candidate/CV view ────────────────────────────────────────────────
# Shared by /stats and /search so the stat tiles always match what the list
# below them shows: every candidate, whether or not they have an actual
# cv_repository row yet (empty-paste application, a silently-failed ingest,
# legacy data, etc.) — synthesised via the second UNION branch.

_COMBINED_CV_CTE = """
    WITH combined AS (
        SELECT
            cv.id                   AS cv_id,
            cv.file_name,
            cv.candidate_name,
            cv.map_status,
            cv.enrich_status,
            cv.experience_years,
            cv.current_position,
            cv.location,
            cv.ai_summary,
            cv.skills,
            cv.text_vector,
            cv.source,
            cv.created_at,
            cv.candidate_id,
            COALESCE(cv.requisition_id, lat_app.requisition_id) AS requisition_id,
            lat_app.status          AS candidate_stage,
            lat_app.id              AS application_id,
            lat_app.source          AS app_source,
            cv.tenant_id            AS tenant_id
        FROM cv_repository cv
        LEFT JOIN LATERAL (
            SELECT id, status, requisition_id, source FROM application
            WHERE candidate_id = cv.candidate_id
            ORDER BY applied_at DESC LIMIT 1
        ) lat_app ON cv.candidate_id IS NOT NULL

        UNION ALL

        SELECT
            NULL::uuid              AS cv_id,
            NULL                    AS file_name,
            c.full_name             AS candidate_name,
            'mapped'                AS map_status,
            NULL                    AS enrich_status,
            NULL::numeric           AS experience_years,
            NULL                    AS current_position,
            NULL                    AS location,
            NULL                    AS ai_summary,
            NULL::text[]            AS skills,
            NULL::tsvector          AS text_vector,
            'no_cv'                 AS source,
            c.created_at,
            c.id                    AS candidate_id,
            lat_app.requisition_id  AS requisition_id,
            lat_app.status          AS candidate_stage,
            lat_app.id              AS application_id,
            lat_app.source          AS app_source,
            c.tenant_id             AS tenant_id
        FROM candidate c
        LEFT JOIN LATERAL (
            SELECT id, status, requisition_id, source FROM application
            WHERE candidate_id = c.id
            ORDER BY applied_at DESC LIMIT 1
        ) lat_app ON true
        WHERE NOT EXISTS (
            SELECT 1 FROM cv_repository x WHERE x.candidate_id = c.id
        )
    )
"""


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
def cv_stats(user: dict = Depends(_cv_auth)):
    row = query_one(
        f"""{_COMBINED_CV_CTE}
        SELECT
               COUNT(*)                                            AS total,
               COUNT(*) FILTER (WHERE map_status='mapped')        AS mapped,
               COUNT(*) FILTER (WHERE map_status='pool')          AS pool,
               COUNT(*) FILTER (
                   WHERE enrich_status='done'
                     AND (COALESCE(array_length(skills,1),0) > 0
                          OR experience_years IS NOT NULL
                          OR current_position IS NOT NULL
                          OR location IS NOT NULL)
               )                                                   AS enriched,
               COUNT(*) FILTER (WHERE enrich_status='pending')    AS pending,
               COUNT(*) FILTER (WHERE enrich_status='failed')     AS failed
           FROM combined
           WHERE tenant_id = %s""",
        [user.get("tenant_id")],
    )
    total    = int(row["total"] or 0)
    enriched = int(row["enriched"] or 0)

    # Whether the cv_enricher background loop is actually alive right now —
    # under `uvicorn --workers=N`, only one of the N processes wins the
    # startup singleton lock and runs it, so this can't just be inferred
    # from "is the app up". The loop writes this heartbeat every iteration
    # (including idle ticks), so a stale value means the loop isn't
    # running in ANY worker process, not just that it's between rows.
    hb = query_one(
        "SELECT value FROM system_status WHERE key='cv_enricher_heartbeat'", [],
    )
    enricher_seconds_since_heartbeat = None
    if hb and hb.get("value"):
        try:
            from datetime import datetime, timezone
            last = datetime.fromisoformat(hb["value"])
            enricher_seconds_since_heartbeat = (datetime.now(timezone.utc) - last).total_seconds()
        except Exception:
            pass

    # Root-cause breadcrumbs for a dead/crash-looping loop — populated by
    # main.py's _try_acquire_bg_worker_lock (connection-level failure
    # acquiring the singleton lock) and _track_bg_task (the cv_enricher
    # asyncio task itself raising and dying). Surfacing the actual
    # exception string here means the next stall is diagnosable from the
    # browser, no server log/shell access needed.
    lock_err  = query_one("SELECT value FROM system_status WHERE key='bg_lock_last_error'", [])
    crash_err = query_one("SELECT value FROM system_status WHERE key='bg_task_status:cv_enricher'", [])

    # A stale heartbeat with NO lock/crash error logged is exactly what a
    # LEAKED advisory lock looks like: some backend connection still holds
    # it (so every fresh worker legitimately loses the pg_try_advisory_lock
    # race — not an error, just never a winner) without actually running
    # the enrichment loop behind it (e.g. its asyncio task died some other
    # way, or the connection is an orphan from a container that didn't
    # fully exit). Whoever holds the lock right now is directly visible
    # here — no server shell access needed to find or clear it.
    from .. import main as _main_mod
    lock_holder = query_one(
        """SELECT l.pid,
                  a.client_addr::text                              AS client_addr,
                  a.state,
                  EXTRACT(EPOCH FROM (now() - a.backend_start))::int AS conn_age_seconds,
                  a.query
           FROM pg_locks l
           JOIN pg_stat_activity a ON a.pid = l.pid
           WHERE l.locktype = 'advisory' AND l.objid = %s AND l.granted""",
        [_main_mod._BG_WORKER_LOCK_KEY],
    )

    return {
        "total":        total,
        "mapped":       int(row["mapped"] or 0),
        "pool":         int(row["pool"] or 0),
        "enriched":     enriched,
        "pending":      int(row["pending"] or 0),
        "failed":       int(row["failed"] or 0),
        "enriched_pct": round(enriched / total * 100, 1) if total else 0,
        # >90s stale means the enricher loop isn't running anywhere (it
        # heartbeats at least every _IDLE_SLEEP=30s when idle).
        "enricher_alive": (enricher_seconds_since_heartbeat is not None
                            and enricher_seconds_since_heartbeat < 90),
        "enricher_seconds_since_heartbeat": enricher_seconds_since_heartbeat,
        "enricher_lock_error":  (lock_err or {}).get("value"),
        "enricher_crash_error": (crash_err or {}).get("value"),
        "enricher_lock_holder_pid":         (lock_holder or {}).get("pid"),
        "enricher_lock_holder_state":       (lock_holder or {}).get("state"),
        "enricher_lock_holder_conn_age_s":  (lock_holder or {}).get("conn_age_seconds"),
    }


@router.post("/force-clear-enrichment-lock")
def force_clear_enrichment_lock(user: dict = Depends(_cv_auth)):
    """
    Kills whatever backend connection currently holds the cv_enricher
    singleton advisory lock. Exists for exactly the failure mode the
    /stats diagnostics above can reveal but not fix on their own: a
    connection that's still holding the lock (so every fresh worker
    legitimately loses the pg_try_advisory_lock race — not an error, just
    never a winner) without actually running the enrichment loop behind
    it — e.g. an orphaned connection from a container that didn't fully
    exit. Killing that one specific connection lets Postgres release the
    lock immediately, so the next watchdog retry (within ~30s, on any
    already-running worker) picks it up — no full app/container restart
    needed.
    """
    if user.get("role") not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only ta_manager or admin can clear the enrichment lock")

    from .. import main as _main_mod
    holder = query_one(
        """SELECT l.pid
           FROM pg_locks l
           WHERE l.locktype = 'advisory' AND l.objid = %s AND l.granted""",
        [_main_mod._BG_WORKER_LOCK_KEY],
    )
    if not holder:
        return {"cleared": False, "detail": "No connection currently holds the lock."}

    query("SELECT pg_terminate_backend(%s)", [holder["pid"]], fetch=False)
    return {"cleared": True, "pid": holder["pid"]}


@router.post("/retry-failed-enrichment")
def retry_failed_enrichment(user: dict = Depends(_cv_auth)):
    """
    Reset enrich_status='failed' rows back to 'pending' so the background
    cv_enricher picks them up again. There was previously no way to recover
    a CV that exhausted its retries (e.g. during a transient Groq outage).
    """
    if user.get("role") not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only ta_manager or admin can retry failed enrichment")

    rows = query(
        """UPDATE cv_repository SET enrich_status='pending'
           WHERE enrich_status='failed' AND tenant_id=%s
           RETURNING id""",
        [user.get("tenant_id")],
    )
    return {"requeued": len(rows or [])}


def _retag_tier1_all(tenant_id: str = None) -> None:
    """
    Runs in a background task, AFTER the triggering HTTP response has
    already gone back to the client. Doing this loop inline in the request
    handler (its first version) held one request open for the entire pass
    over every row (1962 individual SELECT-then-UPDATE round trips) and a
    reverse-proxy/gateway timeout in front of the app cut the connection
    before it finished, surfacing as an opaque HTTP 503 to the recruiter
    who clicked the button. A background task keeps the same work off the
    request/response cycle entirely.
    """
    where = "WHERE raw_text IS NOT NULL AND raw_text != ''"
    params = []
    if tenant_id:
        where += " AND tenant_id = %s"
        params.append(tenant_id)
    rows = query(f"SELECT id, raw_text FROM cv_repository {where}", params)
    for row in rows or []:
        skills = _parser.extract_tier1_skills(row["raw_text"])
        query("UPDATE cv_repository SET skills=%s WHERE id=%s", [skills, row["id"]], fetch=False)
    print(f"[cv-api] reenrich-all: Tier-1 retagged {len(rows or [])} CV(s) in the background")


@router.post("/reenrich-all")
def reenrich_all(background_tasks: BackgroundTasks, user: dict = Depends(_cv_auth)):
    """
    Two-part fix, both against already-cached raw_text (no re-upload,
    no external calls for the first part):

    1. Instantly recompute each row's Tier-1 keyword-matched skills with
       the CURRENT skills_dictionary.json — a synchronous, in-process regex
       pass, no Groq call — so the Top Skills shown in the UI improve right
       away instead of everyone staring at stale/wrong tags for the ~1.5-2h
       it takes the slow, rate-limited (~20/min) Tier-2 LLM queue to grind
       through the whole table. Runs as a background task (see
       _retag_tier1_all) so this endpoint responds immediately regardless
       of table size.
    2. Requeue enrich_status back to 'pending' so Tier-2 refines every row
       in the background afterward, same as before.
    """
    if user.get("role") not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only ta_manager or admin can re-enrich all CVs")
    tenant_id = user.get("tenant_id")

    total = query_one(
        "SELECT count(*) AS n FROM cv_repository WHERE raw_text IS NOT NULL AND raw_text != '' AND tenant_id = %s",
        [tenant_id],
    )
    requeued = query(
        """UPDATE cv_repository SET enrich_status='pending'
           WHERE enrich_status='done' AND tenant_id = %s
           RETURNING id""",
        [tenant_id],
    )
    background_tasks.add_task(_retag_tier1_all, tenant_id)
    return {"requeued": len(requeued or []), "retagging_started_for": int(total["n"] or 0)}


# ── Search ────────────────────────────────────────────────────────────────────

@router.get("/search")
def cv_search(
    q:          Optional[str] = None,
    skills:     Optional[str] = None,
    min_exp:    Optional[float] = None,
    map_status: Optional[str] = None,
    limit:      int = 50,
    offset:     int = 0,
    user: dict = Depends(_cv_auth),
):
    limit  = max(1, min(limit, 500))
    offset = max(0, offset)
    conditions: list[str] = ["combined.tenant_id = %s"]
    params: list = [user.get("tenant_id")]

    if q and q.strip():
        try:
            tsq = _to_tsquery(q)
        except ValueError as exc:
            raise HTTPException(400, f"Search syntax error: {exc}")
        if tsq:
            conditions.append("combined.text_vector @@ to_tsquery('english', %s)")
            params.append(tsq)

    if skills:
        skill_list = [s.strip().lower() for s in skills.split(",") if s.strip()]
        if skill_list:
            conditions.append("combined.skills && %s")
            params.append(skill_list)

    if min_exp is not None:
        conditions.append("combined.experience_years >= %s")
        params.append(min_exp)

    if map_status in ("mapped", "pool"):
        conditions.append("combined.map_status = %s")
        params.append(map_status)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Candidates always show up here, even before/without any cv_repository
    # row (empty-paste applications, a silently-failed ingest, legacy data,
    # etc.) — the second UNION branch synthesises a placeholder row for any
    # candidate not already covered by cv_repository. Skill/experience/text
    # filters naturally exclude those placeholders since they carry NULLs.
    combined_cte = _COMBINED_CV_CTE

    count_row = query_one(
        f"""{combined_cte}
        SELECT COUNT(*) AS n
        FROM combined
        {where}""",
        params,
    )
    total = int((count_row or {}).get("n") or 0)

    rows = query(
        f"""{combined_cte}
        SELECT
            combined.*,
            c.full_name            AS cand_full_name,
            r.req_code, r.title    AS req_title,
            rc_info.recruiter_name
        FROM combined
        LEFT JOIN candidate   c ON c.id = combined.candidate_id
        LEFT JOIN requisition r ON r.id = combined.requisition_id
        LEFT JOIN LATERAL (
            SELECT ru.full_name AS recruiter_name
            FROM requisition_recruiter rr2
            JOIN app_user ru ON ru.id = rr2.recruiter_id
            WHERE rr2.requisition_id = combined.requisition_id
            ORDER BY rr2.is_owner DESC NULLS LAST LIMIT 1
        ) rc_info ON true
        {where}
        ORDER BY combined.created_at DESC
        LIMIT %s OFFSET %s""",
        params + [limit, offset],
    ) or []

    from ..services.source_labels import attach_source_labels
    attach_source_labels(rows, "app_source")

    def _row(r):
        top_skills = list((r["skills"] or [])[:8])
        # enrich_status='done' only means the LLM call succeeded — it can still
        # return empty fields for a resume with no real content (e.g. test/
        # placeholder text, a scanned image PDF with no OCR text). Flag that
        # case separately so the UI doesn't show a misleading "AI ✓".
        low_content = (
            r["enrich_status"] == "done"
            and not top_skills
            and not r["current_position"]
            and not r["location"]
            and r["experience_years"] is None
        )
        return {
            "id":              str(r["cv_id"]) if r["cv_id"] else None,
            "file_name":       r["file_name"],
            "candidate_name":  r["candidate_name"],
            "map_status":      r["map_status"],
            "enrich_status":   r["enrich_status"],
            "low_content":     low_content,
            "experience_years": r["experience_years"],
            "current_position": r["current_position"],
            "location":        r["location"],
            "ai_summary":      r["ai_summary"],
            "top_skills":      top_skills,
            "source":          r["source"],
            "source_label":    r["source_label"],
            "recruiter_name":  r["recruiter_name"],
            "created_at":      r["created_at"].isoformat() if r["created_at"] else None,
            "candidate_id":    str(r["candidate_id"]) if r["candidate_id"] else None,
            "cand_full_name":  r["cand_full_name"],
            "requisition_id":  str(r["requisition_id"]) if r["requisition_id"] else None,
            "req_code":        r["req_code"],
            "req_title":       r["req_title"],
            "candidate_stage": r["candidate_stage"],
            "application_id":  str(r["application_id"]) if r["application_id"] else None,
        }

    return {"total": total, "results": [_row(r) for r in rows]}


# ── Job progress ──────────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}")
def get_job(job_id: str, user: dict = Depends(_cv_auth)):
    row = query_one(
        "SELECT * FROM cv_ingest_jobs WHERE id=%s", [job_id]
    )
    if not row:
        raise HTTPException(404, "Job not found")
    return {
        "id":          str(row["id"]),
        "status":      row["status"],
        "total":       row["total"],
        "processed":   row["processed"],
        "mapped":      row["mapped"],
        "pooled":      row["pooled"],
        "duplicates":  row["duplicates"],
        "skipped":     row["skipped"],
        "errors":      row["errors"] or [],
        "created_at":  row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: dict = Depends(_cv_auth)):
    """
    Stop a running "Scan Ingest Folder" / "Scan My Email" job early — e.g.
    the recruiter watching the progress modal spots the wrong kind of file
    being picked up and wants to halt before it processes the rest of the
    batch. Cooperative: sets a flag the loop checks between files/messages,
    so it stops within one file/message, not instantly mid-file. Whatever
    was already ingested before the stop stays — use bulk-delete to remove
    anything wrongly added.
    """
    row = query_one("SELECT status, uploaded_by FROM cv_ingest_jobs WHERE id=%s", [job_id])
    if not row:
        raise HTTPException(404, "Job not found")
    is_owner = row.get("uploaded_by") and str(row["uploaded_by"]) == user["sub"]
    if not is_owner and user.get("role") not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only the job's owner or ta_manager/admin can stop it")
    if row["status"] != "running":
        return {"ok": True, "already": row["status"]}

    query("UPDATE cv_ingest_jobs SET cancel_requested=TRUE WHERE id=%s", [job_id], fetch=False)
    return {"ok": True}


# ── Master stop — halts EVERY scan path, not just one job ────────────────────

@router.get("/scan-paused")
def get_scan_paused(user: dict = Depends(_cv_auth)):
    from ..services.cv_ingest import is_scan_paused
    return {"paused": is_scan_paused()}


@router.post("/master-stop")
def master_stop(user: dict = Depends(_cv_auth)):
    """
    Stop ALL CV scanning immediately: every currently-running "Scan Ingest
    Folder" / "Scan My Email" job, AND the automatic per-recruiter mailbox
    poller (recruiter_email_worker.py), which runs on its own 5-minute timer
    independent of any single job's Stop button — cancelling one job never
    touched it, which is why scanning kept happening after a per-job stop.
    Stays paused until /master-resume is called.
    """
    if user.get("role") not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only ta_manager or admin can stop all scanning")
    from ..services.cv_ingest import set_scan_paused
    set_scan_paused(True)
    rows = query(
        "UPDATE cv_ingest_jobs SET cancel_requested=TRUE WHERE status='running' RETURNING id"
    ) or []
    return {"ok": True, "paused": True, "jobs_stopped": len(rows)}


@router.post("/master-resume")
def master_resume(user: dict = Depends(_cv_auth)):
    if user.get("role") not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only ta_manager or admin can resume scanning")
    from ..services.cv_ingest import set_scan_paused
    set_scan_paused(False)
    return {"ok": True, "paused": False}


# ── Gmail status (per-user App Password) ────────────────────────────────────

@router.get("/email-status")
def email_status(user: dict = Depends(_cv_auth)):
    row = query_one(
        "SELECT gmail_address, gmail_app_password FROM app_user WHERE id = %s",
        [user["sub"]],
    )
    gmail_addr = (row or {}).get("gmail_address") or ""
    has_pw     = bool((row or {}).get("gmail_app_password"))
    configured = bool(gmail_addr and has_pw)
    return {
        "configured":    configured,
        "gmail_address": gmail_addr,
        "accounts":      [gmail_addr] if configured else [],
    }


# ── IMAP email scan (per-user Gmail + App Password) ──────────────────────────

@router.post("/scan-email")
def scan_email(background_tasks: BackgroundTasks, user: dict = Depends(_cv_auth)):
    """
    Scan the calling user's Gmail inbox via IMAP for CV attachments
    (PDF / DOCX / DOC).  Returns a job_id to poll via /api/cv/jobs/{id}.
    """
    from ..services.cv_ingest import is_scan_paused
    if is_scan_paused():
        raise HTTPException(409, "CV scanning is paused (master stop is active). Resume it first in CV Repository.")

    row = query_one(
        "SELECT gmail_address, gmail_app_password, gmail_last_scan_at FROM app_user WHERE id = %s",
        [user["sub"]],
    )
    if not row or not row.get("gmail_address") or not row.get("gmail_app_password"):
        raise HTTPException(400, "No Gmail App Password configured for your account. Ask your admin to set it.")

    job_id = str(uuid.uuid4())
    query(
        """INSERT INTO cv_ingest_jobs (id, status, total, processed, mapped,
                pooled, duplicates, errors, uploaded_by)
           VALUES (%s, 'running', 0, 0, 0, 0, 0, '[]', %s)""",
        [job_id, user["sub"]],
        fetch=False,
    )
    background_tasks.add_task(
        _run_imap_ingest, job_id,
        row["gmail_address"], row["gmail_app_password"], row.get("gmail_last_scan_at"),
        user["sub"], user.get("tenant_id"),
    )
    return {"job_id": job_id}


def _run_imap_ingest(job_id: str, gmail: str, app_password: str, last_scan_at, uploaded_by: str, tenant_id: Optional[str] = None):
    """
    Background task: IMAP scan + ingest CV attachments from one Gmail inbox.
    Delegates the actual scan/classify/ingest to services/cv_email_scan.py,
    which is shared with the recruiter_email_worker background poller.
    """
    import json
    from ..services.cv_email_scan import scan_gmail_inbox

    def _set_job(**kw):
        sets = ", ".join(f"{k}=%s" for k in kw)
        query(
            f"UPDATE cv_ingest_jobs SET {sets} WHERE id=%s",
            list(kw.values()) + [job_id],
            fetch=False,
        )

    try:
        totals = scan_gmail_inbox(
            gmail, app_password, uploaded_by, since=last_scan_at,
            should_stop=lambda: _job_cancel_requested(job_id),
            tenant_id=tenant_id,
        )
    except Exception as exc:
        _set_job(status="done", errors=json.dumps([{"file": "IMAP login", "error": str(exc)}]))
        return

    # Only advance the checkpoint on an uninterrupted scan — a stopped scan
    # may not have walked the whole window, so the next run should still
    # cover it rather than treating it as already-seen.
    if not totals.get("cancelled"):
        query(
            "UPDATE app_user SET gmail_last_scan_at = now() WHERE id = %s",
            [uploaded_by], fetch=False,
        )

    total = totals["processed"] + totals["duplicates"] + len(totals["errors"])
    _set_job(
        status="cancelled" if totals.get("cancelled") else "done",
        total=total,
        processed=totals["processed"],
        mapped=totals["mapped"],
        pooled=totals["pooled"],
        duplicates=totals["duplicates"],
        skipped=totals["skipped"],
        errors=json.dumps(totals["errors"]),
    )


# ── File serve ────────────────────────────────────────────────────────────────

@router.get("/{cv_id}/file")
def serve_cv_file(cv_id: str, user: dict = Depends(_cv_auth)):
    row = query_one(
        "SELECT file_path, file_name, file_ext FROM cv_repository WHERE id=%s AND tenant_id=%s",
        [cv_id, user.get("tenant_id")],
    )
    if not row or not row["file_path"]:
        raise HTTPException(404, "CV not found")
    media_types = {
        "pdf":  "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc":  "application/msword",
        "txt":  "text/plain",
    }
    mt = media_types.get(row["file_ext"] or "", "application/octet-stream")
    # "View" (viewAuth, opens in a new tab) needs the browser to render the
    # file instead of forcing a download dialog. The separate "↓" download
    # button goes through downloadAuth's blob+`download` attribute, which
    # forces a save regardless of this header — so inline here doesn't
    # affect that button.
    resp = storage_response(
        _cv_storage(), row["file_path"],
        filename=row["file_name"] or f"cv_{cv_id}.{row['file_ext']}",
        media_type=mt, inline=True,
    )
    if resp is None:
        raise HTTPException(404, "File not found in storage")
    return resp


@router.delete("/{cv_id}")
def delete_cv(cv_id: str, user: dict = Depends(_cv_auth)):
    """
    Remove a wrongly-added CV Repository entry (bad upload, duplicate,
    mis-scanned file). No table has a NOT-NULL FK into cv_repository — the
    only reverse link (candidate.cv_repository_id) is ON DELETE SET NULL —
    so this is a plain hard delete plus removing the file on disk.
    """
    row = query_one("SELECT file_path FROM cv_repository WHERE id=%s AND tenant_id=%s", [cv_id, user.get("tenant_id")])
    if not row:
        raise HTTPException(404, "CV not found")

    query("DELETE FROM cv_repository WHERE id=%s", [cv_id], fetch=False)

    if row["file_path"]:
        _cv_storage().delete(row["file_path"])

    return {"ok": True}


# ── CV profile (click a name → see what the AI extracted) ───────────────────

@router.get("/{cv_id}/profile")
async def get_cv_profile(cv_id: str, user: dict = Depends(_cv_auth)):
    """
    Full extracted profile for one CV — used by the "click a candidate name"
    modal so a recruiter can see what's actually in a pooled resume before
    deciding whether/where to map it, instead of only seeing the filename.

    If enrichment hasn't reached this row yet (still 'pending' — the
    background pass is rate-limited and shared across every pending CV, so
    a big backlog can sit for a while), this runs it on-demand right now
    rather than making the recruiter wait for the queue.
    """
    row = query_one(
        """SELECT id, file_name, candidate_name, email, phone, skills,
                  experience_years, current_position, location, ai_summary,
                  map_status, enrich_status, source, created_at,
                  candidate_id, requisition_id
           FROM cv_repository WHERE id=%s AND tenant_id=%s""",
        [cv_id, user.get("tenant_id")],
    )
    if not row:
        raise HTTPException(404, "CV not found")

    if row["enrich_status"] == "pending":
        from ..services.cv_enricher import enrich_cv_now
        try:
            await enrich_cv_now(cv_id)
            row = query_one(
                """SELECT id, file_name, candidate_name, email, phone, skills,
                          experience_years, current_position, location, ai_summary,
                          map_status, enrich_status, source, created_at,
                          candidate_id, requisition_id
                   FROM cv_repository WHERE id=%s AND tenant_id=%s""",
                [cv_id, user.get("tenant_id")],
            )
        except Exception as exc:
            print(f"[cv-profile] on-demand enrichment failed for {cv_id}: {exc}")

    req_info = None
    if row["requisition_id"]:
        req_info = query_one(
            "SELECT req_code, title FROM requisition WHERE id=%s", [row["requisition_id"]]
        )

    return {
        "id":                str(row["id"]),
        "file_name":         row["file_name"],
        "candidate_name":    row["candidate_name"],
        "email":             row["email"],
        "phone":             row["phone"],
        "skills":            row["skills"] or [],
        "experience_years":  row["experience_years"],
        "current_position":  row["current_position"],
        "location":          row["location"],
        "ai_summary":        row["ai_summary"],
        "map_status":        row["map_status"],
        "enrich_status":     row["enrich_status"],
        "source":            row["source"],
        "created_at":        row["created_at"].isoformat() if row["created_at"] else None,
        "candidate_id":      str(row["candidate_id"]) if row["candidate_id"] else None,
        "requisition_id":    str(row["requisition_id"]) if row["requisition_id"] else None,
        "req_code":          (req_info or {}).get("req_code"),
        "req_title":         (req_info or {}).get("title"),
    }


class MapToRequisitionIn(BaseModel):
    requisition_id: str


def _map_one_cv_to_requisition(cv_id: str, requisition_id: str, tenant_id: str = None) -> dict:
    """
    Take a CV Repository entry (mapped or still in the pool) and enter it
    into a chosen requisition's real pipeline — same intake_and_screen()
    scoring path every other application source (career site, candidate
    portal, vendor, campus) goes through, so it shows up in that
    requisition's Candidates list with a real match score, not just a
    repository tag.

    If this CV isn't linked to a candidate yet, one is created (or reused,
    matched by email/phone) from the AI-extracted candidate_name/email/
    phone — which requires an email to already be on the row; if
    enrichment hasn't found one, the profile modal should be opened first
    (GET /{cv_id}/profile runs enrichment on-demand) rather than failing
    here with an unhelpful 422.

    Shared by the single-CV map endpoint and the bulk-map endpoint so both
    stay in lockstep with the one real intake path.
    """
    if tenant_id:
        row = query_one(
            """SELECT candidate_name, email, phone, raw_text, experience_years,
                      candidate_id, file_path
               FROM cv_repository WHERE id=%s AND tenant_id=%s""",
            [cv_id, tenant_id],
        )
    else:
        row = query_one(
            """SELECT candidate_name, email, phone, raw_text, experience_years,
                      candidate_id, file_path
               FROM cv_repository WHERE id=%s""",
            [cv_id],
        )
    if not row:
        raise HTTPException(404, "CV not found")

    if tenant_id:
        req = query_one("SELECT id FROM requisition WHERE id=%s AND tenant_id=%s", [requisition_id, tenant_id])
    else:
        req = query_one("SELECT id FROM requisition WHERE id=%s", [requisition_id])
    if not req:
        raise HTTPException(404, "Requisition not found")

    file_size_bytes = 0
    if row["file_path"]:
        file_size_bytes = _cv_storage().size(row["file_path"]) or 0

    candidate_id = str(row["candidate_id"]) if row["candidate_id"] else None
    if not candidate_id:
        if not row["email"]:
            raise HTTPException(
                422,
                "No email found for this candidate yet — open their profile first "
                "so the AI can extract one, or this CV can't become a real application.",
            )
        from ..services.candidate_dedup import dedup_or_create_candidate
        candidate_id = str(dedup_or_create_candidate(
            full_name=row["candidate_name"] or "Unknown Candidate",
            email=row["email"],
            phone=row["phone"],
            gender=None,
            source="cv_repository",
            resume_url=None,
            requisition_id=requisition_id,
        ))
        query(
            "UPDATE candidate SET cv_repository_id=%s WHERE id=%s AND cv_repository_id IS NULL",
            [cv_id, candidate_id], fetch=False,
        )

    from ..services.pipeline import intake_and_screen, NoPoachBlockedError
    try:
        app_row = intake_and_screen(
            requisition_id, candidate_id, row["raw_text"] or "",
            row["experience_years"], file_size_bytes, current_company=None,
        )
    except NoPoachBlockedError as exc:
        raise HTTPException(409, str(exc))

    query(
        "UPDATE cv_repository SET candidate_id=%s, requisition_id=%s, map_status='mapped' WHERE id=%s",
        [candidate_id, requisition_id, cv_id], fetch=False,
    )

    # Same "Application Received — JD" confirmation a career-site applicant
    # gets, so a pool candidate mapped onto a requisition here isn't left
    # without one just because they entered through a different door.
    cand_row = query_one("SELECT full_name, email FROM candidate WHERE id=%s", [candidate_id])
    if cand_row and cand_row.get("email"):
        from ..services.jd_email import send_application_received_jd_email
        send_application_received_jd_email(cand_row["full_name"], cand_row["email"], requisition_id)

    return {
        "ok": True,
        "candidate_id": candidate_id,
        "application_id": str(app_row["id"]),
        "match_score": app_row.get("match_score"),
        "status": app_row.get("status"),
    }


@router.post("/{cv_id}/map")
def map_cv_to_requisition(cv_id: str, body: MapToRequisitionIn, user: dict = Depends(_cv_auth)):
    return _map_one_cv_to_requisition(cv_id, body.requisition_id, user.get("tenant_id"))


class BulkDeleteIn(BaseModel):
    cv_ids: list[str]


@router.post("/bulk-delete")
def bulk_delete_cv(body: BulkDeleteIn, user: dict = Depends(_cv_auth)):
    """
    Delete many CV Repository entries in one call — e.g. clearing out a
    batch that was ingested before the content classifier existed and picked
    up non-resume attachments. Restricted to ta_manager/admin (not plain
    recruiter) since the blast radius is much larger than the single-CV
    delete above. Same underlying effect as calling DELETE /{cv_id} once per
    id: hard-delete the row plus its file on disk, best-effort per id so one
    bad id/missing file doesn't abort the rest of the batch.
    """
    if user.get("role") not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only ta_manager or admin can bulk-delete CVs")
    if not body.cv_ids:
        raise HTTPException(400, "No cv_ids provided")
    if len(body.cv_ids) > 2000:
        raise HTTPException(400, "Too many ids in one request (max 2000)")

    rows = query(
        "SELECT id, file_path FROM cv_repository WHERE id = ANY(%s::uuid[]) AND tenant_id=%s",
        [body.cv_ids, user.get("tenant_id")],
    ) or []
    found_ids = [str(r["id"]) for r in rows]
    not_found = [cid for cid in body.cv_ids if cid not in found_ids]

    if found_ids:
        query("DELETE FROM cv_repository WHERE id = ANY(%s::uuid[])", [found_ids], fetch=False)

    removed_files = 0
    storage = _cv_storage()
    for r in rows:
        if r["file_path"]:
            storage.delete(r["file_path"])
            removed_files += 1

    return {"deleted": len(found_ids), "not_found": not_found, "files_removed": removed_files}


class BulkMapIn(BaseModel):
    cv_ids: list[str]
    requisition_id: str


@router.post("/bulk-map")
def bulk_map_cv(body: BulkMapIn, user: dict = Depends(_cv_auth)):
    """
    Map many CV Repository entries onto one requisition in a single call —
    e.g. a recruiter sourcing a batch of pooled resumes for a new opening.
    Open to the same roles as the single-CV map endpoint (ta_manager,
    recruiter, admin — enforced by _cv_auth), unlike bulk-delete which is
    restricted further. Each id runs through the same intake_and_screen()
    path as the single-CV endpoint, so successes appear immediately as
    applied candidates on the requisition's pipeline; one bad id (missing
    email, no-poach block, etc.) doesn't abort the rest of the batch.
    """
    if not body.cv_ids:
        raise HTTPException(400, "No cv_ids provided")
    if len(body.cv_ids) > 2000:
        raise HTTPException(400, "Too many ids in one request (max 2000)")

    req = query_one("SELECT id FROM requisition WHERE id=%s AND tenant_id=%s", [body.requisition_id, user.get("tenant_id")])
    if not req:
        raise HTTPException(404, "Requisition not found")

    mapped, failed = [], []
    for cv_id in body.cv_ids:
        try:
            result = _map_one_cv_to_requisition(cv_id, body.requisition_id, user.get("tenant_id"))
            mapped.append({"cv_id": cv_id, **result})
        except HTTPException as exc:
            failed.append({"cv_id": cv_id, "error": exc.detail})

    return {"mapped": len(mapped), "failed": failed, "results": mapped}


# ── Upload (multiple files) ───────────────────────────────────────────────────

@router.post("/upload")
async def upload_cvs(
    files: list[UploadFile] = File(...),
    user:  dict = Depends(_cv_auth),
):
    """
    Manual multi-file upload (drag-and-drop). Originally skipped the resume
    classifier on the assumption that a human hand-picking files could be
    trusted — in practice recruiters drag in a whole batch of downloaded
    files at once (offer letters, ID scans, etc. mixed in with real CVs)
    without vetting each one, so every file goes through the same
    keyword-pre-filter + thorough AI classify_and_enrich() gate as every
    other ingestion path before it's stored, no exceptions.
    """
    from ..services.cv_enricher import classify_and_enrich, _persist_enrichment_result

    results = []
    for f in files:
        if Path(f.filename or "").suffix.lower() not in _SUPPORTED:
            results.append({"filename": f.filename, "status": "skipped",
                            "reason": "unsupported file type"})
            continue
        data = await f.read()
        ext = Path(f.filename or "").suffix.lower().lstrip(".")

        raw_text = await asyncio.to_thread(_parser.extract_text, data, ext)
        accept, reason, llm_result = await classify_and_enrich(raw_text, f.filename or "")
        if not accept:
            results.append({"filename": f.filename, "status": "skipped",
                            "reason": f"not a CV — {reason}"})
            continue

        r = await asyncio.to_thread(
            _ingest_one, data, f.filename, "upload", user["sub"], raw_text, user.get("tenant_id")
        )
        if llm_result and llm_result.get("is_resume"):
            await asyncio.to_thread(_persist_enrichment_result, r["cv_id"], llm_result)
        results.append({**r, "filename": f.filename})
    ok      = sum(1 for r in results if r.get("status") == "ok")
    dup     = sum(1 for r in results if r.get("status") == "duplicate")
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    return {"processed": ok, "duplicates": dup, "skipped": skipped, "details": results}


# ── Scan folder ───────────────────────────────────────────────────────────────

@router.post("/scan-folder")
def scan_folder(
    background_tasks: BackgroundTasks,
    user: dict = Depends(_cv_auth),
):
    if user.get("role") not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only ta_manager or admin can trigger folder scan")

    from ..services.cv_ingest import is_scan_paused
    if is_scan_paused():
        raise HTTPException(409, "CV scanning is paused (master stop is active). Resume it first in CV Repository.")

    inbox = os.environ.get("CV_INBOX_DIR", "/app/cv_inbox")
    if not os.path.isdir(inbox):
        raise HTTPException(400, f"Inbox folder not found: {inbox}")

    import json
    job_id = str(uuid.uuid4())
    query(
        """INSERT INTO cv_ingest_jobs (id, status, total, processed, mapped,
           pooled, duplicates, errors) VALUES (%s,'running',0,0,0,0,0,'[]'::jsonb)""",
        [job_id], fetch=False,
    )
    background_tasks.add_task(_run_ingest_job, job_id, inbox, user["sub"], user.get("tenant_id"))
    return {"job_id": job_id, "message": "Ingest job started"}


# ── Backfill: sync uploaded candidate resumes into CV Repository ──────────────

@router.post("/backfill-candidates")
def backfill_candidates(user: dict = Depends(_cv_auth)):
    """
    Idempotent — walks all candidates that have a resume file stored on disk
    (resume_url points to a file in UPLOADS_DIR) and ingests each one into
    cv_repository with map_status='mapped' and source='application'.
    Hash dedupe ensures running twice is safe.
    Returns counts: {processed, duplicates, skipped, errors}.
    """
    if user.get("role") not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only ta_manager or admin can run backfill")

    rows = query(
        """SELECT c.id AS candidate_id, c.resume_url, c.full_name,
                  a.requisition_id
           FROM candidate c
           LEFT JOIN LATERAL (
               SELECT requisition_id FROM application
               WHERE candidate_id = c.id
               ORDER BY applied_at DESC LIMIT 1
           ) a ON true
           WHERE c.resume_url IS NOT NULL
             AND c.resume_url != ''
             AND c.tenant_id = %s
           ORDER BY c.id""",
        [user.get("tenant_id")],
    ) or []

    processed = duplicates = skipped = 0
    errors: list[dict] = []

    for row in rows:
        resume_path = row["resume_url"]
        if not resume_path or not os.path.isfile(resume_path):
            skipped += 1
            continue
        try:
            data = Path(resume_path).read_bytes()
            filename = Path(resume_path).name
            r = ingest_and_link(
                data=data,
                filename=filename,
                source="application",
                uploaded_by=user["sub"],
                candidate_id=str(row["candidate_id"]),
                req_id=str(row["requisition_id"]) if row["requisition_id"] else None,
                tenant_id=user.get("tenant_id"),
            )
            if r["status"] == "duplicate":
                duplicates += 1
            else:
                processed += 1
        except Exception as exc:
            errors.append({"candidate_id": str(row["candidate_id"]), "error": str(exc)})

    return {
        "processed":  processed,
        "duplicates": duplicates,
        "skipped":    skipped,
        "errors":     errors,
        "total_candidates": len(rows),
    }


# ── Generate / regenerate long-lived API token ────────────────────────────────

@router.post("/generate-token")
def generate_api_token(user: dict = Depends(get_current_user)):
    """Generate (or replace) a long-lived API token for the current user.
    Used by the watcher script running on recruiter PCs.
    """
    _require(user)
    import secrets as _secrets
    token = _secrets.token_urlsafe(40)
    query(
        "UPDATE app_user SET api_token=%s WHERE id=%s",
        [token, user["sub"]], fetch=False,
    )
    return {"api_token": token,
            "note": "Store this securely — it grants upload access to your account."}
