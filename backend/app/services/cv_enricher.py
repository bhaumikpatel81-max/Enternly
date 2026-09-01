"""
Tier-2 CV enrichment using Groq LLM.

Background asyncio task started at app startup.
Picks the oldest enrich_status='pending' row, calls Groq, updates the DB.
Rate cap: 20 req/min → sleep 3 s between calls (with backoff on 429).

Designed to be fully resumable: if the server restarts, rows still marked
'pending' will be picked up again on next startup.

Never crashes the app — all exceptions are caught and logged.
"""
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

from ..db import query, query_one

# ── LLM config (same Groq credentials used by interviewer_llm.py) ─────────────

def _make_client():
    import openai
    return openai.AsyncOpenAI(
        api_key=os.environ.get("GROQ_API_KEY"),
        base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
    )


def _model() -> str:
    return os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")


# ── Prompt ────────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a document classifier and resume parsing assistant.

FIRST decide whether the text below is genuinely a resume/CV belonging to
one person — as opposed to any other kind of document that might have been
scanned by mistake: an offer letter, appointment/relieving/increment/
experience letter, cover letter, invoice or bank statement or payslip,
a PAN card / Aadhaar / passport / other ID scan, a medical certificate or
fitness declaration, a photo, a report, a JOB DESCRIPTION / JD, or anything
else that isn't a CV.

Job descriptions are the trickiest to catch — they use almost the same
vocabulary as a resume (experience, skills, qualifications,
responsibilities) but describe a ROLE'S requirements, not one person's
actual background. Signals it's a JD, not a resume: written about "the
candidate" / "the ideal candidate" / "the incumbent" in third person rather
than "I" in first person; lists responsibilities the job entails rather
than work the person has actually done; has no specific person's name,
email, or phone number tied to a real career history; talks about what a
company is looking for rather than what someone has accomplished.

Candidates come from every function, not just engineering — sales,
presales, HR/recruitment, procurement, IT infrastructure/support, finance,
marketing, admin/operations, and more. Extract skills that reflect this
candidate's ACTUAL profession as shown in the text, not generic software
buzzwords. A presales/sales resume might have "solution selling", "rfp
response", "lead generation", "crm", "salesforce", "negotiation",
"client relationship management". An HR resume might have "recruitment",
"talent acquisition", "onboarding", "payroll", "hris", "employee
relations". A procurement resume might have "vendor management",
"sourcing", "contract negotiation", "sap mm", "inventory management",
"rfq". An IT-infrastructure/support resume might have "server
administration", "windows server", "active directory", "vmware",
"network administration", "helpdesk", "itil", "backup and recovery". Only
list skills genuinely evidenced in the text — never force-fit unrelated
technical/software terms onto a non-technical resume just because they're
common elsewhere.

Return ONLY a valid JSON object — no markdown fences, no prose before or after.

Required fields:
{
  "is_resume": <true only if this is genuinely someone's resume/CV, false for anything else>,
  "reason": "<a few words on why — e.g. \\"has experience/education/skills sections\\" or \\"this is an offer letter\\">",
  "candidate_name": "<the candidate's own full name as it appears in the resume, or null>",
  "email": "<the candidate's own email address, or null>",
  "phone": "<the candidate's own phone number as written, or null>",
  "skills": ["array", "of", "normalized", "lowercase", "skills", "matching", "this", "candidate's", "own", "profession"],
  "experience_years": <total years of professional experience as a number, or null>,
  "current_position": "<most recent job title, or null>",
  "location": "<city or state/country, or null>",
  "summary": "<one concise sentence describing the candidate's profile>"
}

candidate_name/email/phone must belong to the person the resume is about —
not a company, a reference, or a recruiter's contact. If is_resume is
false, set candidate_name/email/phone/skills/experience_years/
current_position/location/summary all to null/empty — do not guess values
from a document that isn't actually a resume."""


# ── Enrichment loop ───────────────────────────────────────────────────────────

_SLEEP_BETWEEN  = 3.0   # 20 req/min cap
_BACKOFF_429    = [60, 120, 240]
_MAX_RETRIES    = 3
_IDLE_SLEEP     = 30.0  # sleep when no pending rows

# The background queue can afford to wait several minutes on a 429 — it has
# no one watching a progress bar. The ingest-time gate (classify_and_enrich,
# called synchronously from a live "Scan My Email"/"Scan Ingest Folder"/
# upload request) cannot: a long backoff there just makes the whole scan
# feel frozen. On rate-limit it gives up fast instead and falls back to the
# keyword-only verdict for that one file (still a real check, just not the
# AI's), leaving the row's enrich_status='pending' so the patient
# background loop still gets a full AI pass at it later.
_INGEST_MAX_RETRIES = 1
_INGEST_BACKOFF     = [5]


def _strip_fences(s: str) -> str:
    s = re.sub(r'^```(?:json)?\s*', '', s.strip(), flags=re.IGNORECASE)
    s = re.sub(r'\s*```$', '', s.strip())
    return s.strip()


async def _enrich_one(row_id: str, raw_text: str, max_retries: int = _MAX_RETRIES, backoff: list = None) -> Optional[dict]:
    """Call Groq and return parsed result, or None on unrecoverable failure."""
    backoff = _BACKOFF_429 if backoff is None else backoff
    client = _make_client()
    text_truncated = raw_text[:6000]  # keep well under context window
    retries = 0
    backoff_idx = 0

    while retries < max_retries:
        try:
            resp = await client.chat.completions.create(
                model=_model(),
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": f"Resume text:\n\n{text_truncated}"},
                ],
                temperature=0,
                max_tokens=512,
            )
            raw = resp.choices[0].message.content or ""
            cleaned = _strip_fences(raw)
            data = json.loads(cleaned)
            # Normalise types
            skills = [str(s).lower() for s in (data.get("skills") or []) if s]
            exp = data.get("experience_years")
            try:
                exp = float(exp) if exp is not None else None
            except (TypeError, ValueError):
                exp = None
            # Default true when the model omits the field (older prompt
            # behaviour / defensive) rather than silently rejecting.
            is_resume = data.get("is_resume")
            is_resume = True if is_resume is None else bool(is_resume)

            return {
                "is_resume":        is_resume,
                "reason":           str(data.get("reason") or "").strip(),
                "candidate_name":   str(data.get("candidate_name") or "").strip() or None,
                "email":            str(data.get("email") or "").strip().lower() or None,
                "phone":            str(data.get("phone") or "").strip() or None,
                "skills":           skills,
                "experience_years": exp,
                "current_position":  str(data.get("current_position") or "") or None,
                "location":         str(data.get("location") or "") or None,
                "ai_summary":       str(data.get("summary") or "") or None,
            }

        except Exception as exc:
            exc_str = str(exc)
            is_rate_limit = "429" in exc_str or "rate" in exc_str.lower()

            if is_rate_limit:
                sleep_sec = backoff[min(backoff_idx, len(backoff) - 1)]
                backoff_idx += 1
                retries += 1
                print(f"[cv-enricher] 429 for {row_id} — backing off {sleep_sec}s (attempt {retries}/{max_retries})")
                await asyncio.sleep(sleep_sec)
                continue

            # Parse failure — retry once
            if "json" in exc_str.lower() and retries < max_retries - 1:
                retries += 1
                await asyncio.sleep(2)
                continue

            retries += 1
            print(f"[cv-enricher] error for {row_id} (attempt {retries}): {exc}")

    return None  # exhausted retries


def _try_map_by_llm_name(cv_row_id: str, name: str) -> None:
    """
    The filename-derived name used at ingest time (services/cv_ingest.py)
    is often garbage or missing entirely (e.g. a resume saved as
    "Download.pdf" never matches anyone by filename). The LLM extracts the
    name from the resume's actual content instead — if this CV is still
    unmapped, retry the same candidate-lookup ingest already tried, now
    with a name that's far more likely to be correct.
    """
    row = query_one("SELECT map_status FROM cv_repository WHERE id=%s", [cv_row_id])
    if not row or row["map_status"] == "mapped":
        return
    cand = query_one(
        "SELECT id FROM candidate WHERE LOWER(TRIM(full_name)) = LOWER(TRIM(%s)) LIMIT 1",
        [name],
    )
    if not cand:
        return
    candidate_id = str(cand["id"])
    app_row = query_one(
        """SELECT requisition_id FROM application
           WHERE candidate_id=%s ORDER BY applied_at DESC LIMIT 1""",
        [candidate_id],
    )
    req_id = str(app_row["requisition_id"]) if app_row and app_row["requisition_id"] else None
    query(
        "UPDATE cv_repository SET candidate_id=%s, requisition_id=%s, map_status='mapped' WHERE id=%s",
        [candidate_id, req_id, cv_row_id], fetch=False,
    )
    query(
        "UPDATE candidate SET cv_repository_id=%s WHERE id=%s AND cv_repository_id IS NULL",
        [cv_row_id, candidate_id], fetch=False,
    )
    print(f"[cv-enricher] {cv_row_id} mapped to candidate {candidate_id} via LLM-extracted name")


def _persist_enrichment_result(row_id: str, result: dict) -> None:
    """
    Shared by both the background loop and the on-demand path (a recruiter
    opening a CV's profile before the background queue got to it) — same
    UPDATE, same LLM-name remap attempt, so behaviour is identical either
    way the enrichment actually ran.
    """
    set_clauses = [
        "skills = %s", "experience_years = %s", "current_position = %s",
        "location = %s", "ai_summary = %s",
        "enrich_status = 'done'", "enriched_at = now()",
    ]
    params = [
        result["skills"], result["experience_years"],
        result["current_position"], result["location"], result["ai_summary"],
    ]
    # Prefer the content-derived name/email/phone over the filename-guess
    # made at ingest time (services/cv_ingest.py) — a resume saved as
    # "Download.pdf" or "Cover Letter Akhilesh.pdf" gets a garbage/wrong
    # name from the filename alone, and never had email/phone at all.
    if result.get("candidate_name"):
        set_clauses.append("candidate_name = %s")
        params.append(result["candidate_name"])
    if result.get("email"):
        set_clauses.append("email = %s")
        params.append(result["email"])
    if result.get("phone"):
        set_clauses.append("phone = %s")
        params.append(result["phone"])
    params.append(row_id)

    query(
        f"UPDATE cv_repository SET {', '.join(set_clauses)} WHERE id = %s",
        params,
        fetch=False,
    )
    if result.get("candidate_name"):
        _try_map_by_llm_name(row_id, result["candidate_name"])


def _mark_enrichment_failed(row_id: str) -> None:
    query(
        "UPDATE cv_repository SET enrich_status='failed' WHERE id=%s",
        [row_id],
        fetch=False,
    )
    try:
        from .activity_log import log_activity
        log_activity(
            "cv_repository", "enrich_failed",
            entity_id=row_id, actor_id=None, actor_role="system", detail={},
        )
    except Exception:
        pass


async def classify_and_enrich(raw_text: str, label: str = "") -> tuple:
    """
    Ingest-time gate — used by every scan/upload path BEFORE a file is
    stored, not just the background queue. Cheap keyword pre-filter first
    (services/cv_parser.classify_resume_text — skips obvious junk with no
    LLM call spent), then ONE thorough Groq call that both decides
    is_resume and extracts every enrichment field in the same shot, so a
    genuine resume is fully enriched the moment it's scanned instead of
    sitting at "AI processing…" afterward.

    Returns (accept: bool, reason: str, llm_result: dict | None).
    llm_result is the full dict when the LLM actually ran (whether it
    accepted or rejected) so the caller can persist it immediately on
    accept; None when the keyword pre-filter already rejected the file (no
    LLM call made) or the LLM call failed outright — a Groq outage should
    not block scanning entirely, so on LLM failure this falls back to
    accepting the keyword verdict and leaves enrichment for the background
    queue to retry later.
    """
    from . import cv_parser as _parser
    kw_ok, kw_reason = _parser.classify_resume_text(raw_text, label)
    if not kw_ok:
        return False, kw_reason, None

    result = await _enrich_one(label or "ingest", raw_text, max_retries=_INGEST_MAX_RETRIES, backoff=_INGEST_BACKOFF)
    if result is None:
        return True, "llm_unavailable_fell_back_to_keyword_check", None
    if not result["is_resume"]:
        return False, f"ai: {result.get('reason') or 'determined not a resume'}", result

    # Belt-and-suspenders: even when the AI says is_resume=true, require it
    # to have actually found a candidate email or phone — a JD/SOP/policy
    # doc can still read resume-ish enough to fool the model on wording
    # alone, but it won't have any specific person's contact details tied
    # to it. The filename-hint exception mirrors classify_resume_text's,
    # for a real resume whose contact block just didn't extract cleanly.
    import re as _re
    fname_hint = bool(_re.search(r'(resume|cv|curriculum|vitae)', label or "", _re.IGNORECASE))
    if not (result.get("email") or result.get("phone") or fname_hint):
        return False, "ai_said_resume_but_no_candidate_contact_info_found", result

    return True, result.get("reason") or "ai_confirmed_resume", result


def classify_and_enrich_sync(raw_text: str, label: str = "") -> tuple:
    """Sync-context wrapper (bulk-folder scan, IMAP scan) — safe to call
    from a worker thread with no event loop already running in it, which
    is exactly how both of those paths run (FastAPI BackgroundTasks /
    asyncio.to_thread already offload them to a plain thread)."""
    return asyncio.run(classify_and_enrich(raw_text, label))


async def enrich_cv_now(cv_id: str) -> Optional[dict]:
    """
    On-demand enrichment for ONE CV, bypassing the background queue —
    used when a recruiter opens a CV's profile before the slow rate-limited
    background pass (one row every few seconds, shared across every pending
    CV) has gotten to it yet. Returns the parsed result dict, or None if
    there was nothing to enrich (no raw_text) or the LLM call failed.
    """
    row = await asyncio.to_thread(
        query_one,
        "SELECT raw_text, enrich_status FROM cv_repository WHERE id=%s",
        [cv_id],
    )
    if not row or not (row.get("raw_text") or "").strip():
        return None
    if row["enrich_status"] == "done":
        return None  # already enriched, nothing to do

    result = await _enrich_one(cv_id, row["raw_text"])
    if result is not None:
        await asyncio.to_thread(_persist_enrichment_result, cv_id, result)
    else:
        await asyncio.to_thread(_mark_enrichment_failed, cv_id)
    return result


def _write_heartbeat() -> None:
    """
    Lets any admin confirm this loop is actually alive in a given
    deployment (e.g. under `uvicorn --workers=8`, where only one of the N
    worker processes wins the singleton lock and runs this loop at all)
    without needing server shell/log access — GET /api/cv/stats surfaces
    how many seconds old this timestamp is.
    """
    try:
        query(
            """INSERT INTO system_status (key, value, updated_at)
               VALUES ('cv_enricher_heartbeat', now()::text, now())
               ON CONFLICT (key) DO UPDATE SET value = now()::text, updated_at = now()""",
            [], fetch=False,
        )
    except Exception as exc:
        print(f"[cv-enricher] heartbeat write failed: {exc}")


_HEARTBEAT_INTERVAL = 15.0


async def _heartbeat_ticker():
    """
    Writes the heartbeat on its own independent clock, decoupled from
    whatever the main loop below is doing. Without this, the heartbeat was
    only written once per outer-loop iteration, BEFORE calling
    _enrich_one() for that row — and _enrich_one's own Groq 429 backoff can
    legitimately sleep up to 60s+120s=180s working through retries for a
    single row. That made a perfectly healthy loop stuck in rate-limit
    backoff look identical to a genuinely dead one (heartbeat stale for
    100-250+ seconds), which is exactly the false alarm GET /api/cv/stats
    kept reporting. This ticker just proves the asyncio task itself is
    still alive and scheduled, independent of how long any single row
    takes to process.
    """
    while True:
        await asyncio.to_thread(_write_heartbeat)
        await asyncio.sleep(_HEARTBEAT_INTERVAL)


_CLAIM_GRACE_MINUTES = 10  # a row stuck at 'processing' past this is presumed crashed and re-claimable


def _claim_next_pending() -> Optional[dict]:
    """
    Atomically claim the oldest eligible row: a fresh 'pending' row, or one
    stuck at 'processing' past the grace window (a prior claimer crashed
    mid-enrichment). The UPDATE...WHERE id=(SELECT...FOR UPDATE SKIP LOCKED)
    compare-and-swap means two concurrent callers (an overlapping Arq tick,
    or another process) can never claim the same row -- required once
    REDIS_URL is set and this runs as a queued job instead of the original
    single-process assumption. Rows never get stuck forever: this is the
    same claim-then-timestamp pattern already used by
    enteri_ai_render_worker.py's render_claimed_at.
    """
    return query_one(
        """UPDATE cv_repository
           SET enrich_status = 'processing', enrich_claimed_at = now()
           WHERE id = (
               SELECT id FROM cv_repository
               WHERE raw_text IS NOT NULL AND raw_text != ''
                 AND (
                   enrich_status = 'pending'
                   OR (enrich_status = 'processing'
                       AND enrich_claimed_at < now() - (%s || ' minutes')::interval)
                 )
               ORDER BY created_at ASC
               LIMIT 1
               FOR UPDATE SKIP LOCKED
           )
           RETURNING id, raw_text""",
        [_CLAIM_GRACE_MINUTES],
    )


async def run_one_pass() -> str:
    """
    Claim-and-enrich exactly one row. Returns "idle" | "enriched" | "failed"
    so the fallback loop's sleep choice matches exactly what it did before
    this was extracted. Extracted so both start_enricher (Redis-less
    fallback mode) and the Arq queued job (worker.py, Redis mode) call the
    same logic.
    """
    row = await asyncio.to_thread(_claim_next_pending)
    if not row:
        return "idle"

    row_id = str(row["id"])
    result = await _enrich_one(row_id, row["raw_text"])

    if result is not None:
        await asyncio.to_thread(_persist_enrichment_result, row_id, result)
        print(f"[cv-enricher] enriched {row_id}")
        return "enriched"
    else:
        await asyncio.to_thread(_mark_enrichment_failed, row_id)
        print(f"[cv-enricher] failed to enrich {row_id}")
        return "failed"


async def start_enricher():
    """
    Infinite background loop — picks pending CV rows and enriches them.
    Safe to restart: picks up where it left off from DB state.
    """
    print("[cv-enricher] background enricher started")
    heartbeat_task = asyncio.create_task(_heartbeat_ticker())
    try:
        while True:
            try:
                outcome = await run_one_pass()
                await asyncio.sleep(_IDLE_SLEEP if outcome == "idle" else _SLEEP_BETWEEN)

            except asyncio.CancelledError:
                print("[cv-enricher] task cancelled, shutting down")
                return
            except Exception as exc:
                # Must never crash — log and keep running
                print(f"[cv-enricher] unexpected error: {exc}")
                await asyncio.sleep(10)
    finally:
        heartbeat_task.cancel()
