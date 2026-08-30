"""
Pipeline state machine.

This is the heart of "one click hire": it advances an application through the
stages and writes a stage_event row on every transition. Those events power
all TAT reporting. Automated stages move themselves; human gates wait for a
recruiter action.
"""
import json
import os
import re as _re
from decimal import Decimal

from ..db import query, query_one
from . import screening, connectors
from .activity_log import log_activity


class NoPoachBlockedError(Exception):
    """Raised when the candidate's current employer is an active
    status='current' no-poach company -- callers must convert this into a
    clear 4xx response, not let it surface as a generic 500."""
    def __init__(self, company_name: str):
        self.company_name = company_name
        super().__init__(f"Candidate's current employer '{company_name}' is on the no-poach list")


def _json_safe(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"not serializable: {type(obj)}")


def log_event(application_id, from_status, to_status, actor_id=None, note=None):
    query(
        """INSERT INTO stage_event (application_id, from_status, to_status, actor_id, note)
           VALUES (%s, %s, %s, %s, %s)""",
        [application_id, from_status, to_status, actor_id, note],
        fetch=False,
    )


def _extract_ai_detail(breakdown: dict) -> dict:
    """Pull the AI reasoning fields out of a breakdown dict into their own dict."""
    return {
        k: breakdown.get(k)
        for k in ("strengths", "concerns", "rationale", "scored_by", "fallback_reason")
        if breakdown.get(k) is not None
    }


def _normalize_company(name: str) -> str:
    return _re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _check_no_poach_block(current_company, requisition_id=None, entity_id=None) -> None:
    """
    Hard gate run BEFORE the application row is created: if the candidate's
    current employer matches an active no_poach_company with status='current'
    (i.e. someone presently employed at a company under an active no-poach
    agreement), raise NoPoachBlockedError so the caller can reject the intake
    outright with a clear message -- rather than silently flagging it after
    the fact, which is all the old code did.

    Unlike the old best-effort design, a lookup failure here is NOT swallowed:
    it propagates so the recruiter/vendor sees a loud error and can retry,
    instead of a compliance check silently never having run.
    """
    if not (current_company and current_company.strip()):
        return
    norm = _normalize_company(current_company)
    if not norm:
        return
    match = query_one(
        """SELECT company_name, status FROM no_poach_company
           WHERE normalized_name = %s AND is_active = true""",
        [norm],
    )
    if match and match.get("status") == "current":
        log_activity(
            "candidate", "no_poach_blocked",
            entity_id=entity_id, requisition_id=requisition_id, actor_id=None, actor_role="system",
            detail={"current_company": current_company, "matched_company": match["company_name"]},
        )
        raise NoPoachBlockedError(match["company_name"])


def _flag_no_poach_and_rehire(application_id, candidate_id, current_company):
    """
    Runs AFTER intake for the softer signals that don't block: a 'past'-status
    no-poach match (informational -- candidate isn't currently poached from
    anywhere, just previously was) and a rehire-eligibility match. The hard
    'current'-status block already happened pre-insert via
    _check_no_poach_block(); status=='current' can't reach here since a match
    would have raised before this application existed.
    Writes matches into application.flags as {"no_poach": {...}, "rehire": {...}}.
    A failure here is now logged durably (not just print-swallowed) -- see
    log_activity calls below -- though it still doesn't unwind the intake,
    since the application row is already committed by this point.
    """
    try:
        if current_company and current_company.strip():
            norm = _normalize_company(current_company)
            if norm:
                match = query_one(
                    """SELECT company_name, status FROM no_poach_company
                       WHERE normalized_name = %s AND is_active = true""",
                    [norm],
                )
                if match:
                    query(
                        """UPDATE application
                             SET flags = flags || jsonb_build_object('no_poach',
                                   jsonb_build_object('matched_company', %s::text, 'status', %s::text))
                           WHERE id = %s""",
                        [match["company_name"], match["status"], application_id],
                        fetch=False,
                    )
    except Exception as exc:
        print(f"[intake] no-poach lookup failed for application {application_id}: {exc}")
        log_activity(
            "application", "no_poach_flag_failed",
            entity_id=application_id, application_id=application_id,
            actor_id=None, actor_role="system",
            detail={"current_company": current_company, "error": str(exc)},
        )

    try:
        cand = query_one("SELECT email, phone FROM candidate WHERE id = %s", [candidate_id])
        if cand and (cand.get("email") or cand.get("phone")):
            former = query_one(
                """SELECT emp_code, last_designation, exit_type, rehire_eligible
                   FROM former_employee
                   WHERE (email IS NOT NULL AND lower(email) = lower(%s))
                      OR (phone IS NOT NULL AND phone = %s)
                   LIMIT 1""",
                [cand.get("email") or "", cand.get("phone") or ""],
            )
            if former:
                query(
                    """UPDATE application
                         SET flags = flags || jsonb_build_object('rehire', jsonb_build_object(
                               'emp_code', %s::text, 'last_designation', %s::text,
                               'exit_type', %s::text, 'rehire_eligible', %s::boolean))
                       WHERE id = %s""",
                    [former.get("emp_code"), former.get("last_designation"),
                     former.get("exit_type"), former.get("rehire_eligible"), application_id],
                    fetch=False,
                )
    except Exception as exc:
        print(f"[intake] rehire lookup failed for application {application_id}: {exc}")


def intake_and_screen(
    requisition_id,
    candidate_id,
    resume_text,
    candidate_years,
    file_size_bytes: int = 0,
    current_company: str = None,
):
    """
    AUTOMATED. Runs when an application arrives: scores it, stores all screening
    columns, and parks it in the Gate-1 review queue. Returns the application row.
    file_size_bytes: raw byte count of the uploaded file (0 if unavailable),
    used by the parse-quality assessment to flag image-based resumes.
    current_company: used for both the no-poach hard block (status='current'
    matches raise NoPoachBlockedError before anything is created) and the
    softer no-poach/rehire flags recorded after intake. Passed on career-site,
    vendor, campus, and candidate-portal intake -- every entry point now
    collects it.

    Raises NoPoachBlockedError -- callers must catch it and return a clear
    4xx, not let it surface as an unhandled 500.
    """
    _check_no_poach_block(current_company, requisition_id, entity_id=candidate_id)

    req = query_one(
        """SELECT r.*, b.code AS band_code
           FROM requisition r
           JOIN band b ON b.id = r.band_id
           WHERE r.id = %s""",
        [requisition_id],
    )
    if not req:
        raise ValueError("requisition not found")

    score, breakdown = screening.score_application(
        resume_text, candidate_years, req, file_size_bytes
    )

    ai_fit_score      = breakdown.get("ai_fit_score")
    ai_screen_detail  = json.dumps(_extract_ai_detail(breakdown), default=_json_safe)
    avg_tenure_months = breakdown.get("avg_tenure_months")
    stability_score   = breakdown.get("stability_score")
    stability_status  = breakdown.get("stability_status", "not_applicable")

    app = query_one(
        """INSERT INTO application
             (requisition_id, candidate_id, match_score, score_breakdown,
              ai_fit_score, ai_screen_detail,
              avg_tenure_months, stability_score, stability_status,
              status)
           VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s, 'screen')
           ON CONFLICT (requisition_id, candidate_id) DO UPDATE
             SET match_score        = EXCLUDED.match_score,
                 score_breakdown    = EXCLUDED.score_breakdown,
                 ai_fit_score       = EXCLUDED.ai_fit_score,
                 ai_screen_detail   = EXCLUDED.ai_screen_detail,
                 avg_tenure_months  = EXCLUDED.avg_tenure_months,
                 stability_score    = EXCLUDED.stability_score,
                 stability_status   = EXCLUDED.stability_status
           RETURNING *""",
        [
            requisition_id, candidate_id, score,
            json.dumps(breakdown, default=_json_safe),
            ai_fit_score, ai_screen_detail,
            avg_tenure_months, stability_score, stability_status,
        ],
    )
    log_event(app["id"], "applied", "screen", note=f"auto-scored {score}")
    _flag_no_poach_and_rehire(app["id"], candidate_id, current_company)
    return app


def run_bot_round(application_id):
    """
    AUTOMATED (assistive). Runs the AI bot interview, stores the bot score, and
    computes the combined chart score. Does NOT advance past a gate.
    """
    app = query_one("SELECT * FROM application WHERE id = %s", [application_id])
    req = query_one("SELECT * FROM requisition WHERE id = %s", [app["requisition_id"]])
    result = connectors.run_bot_interview(app["candidate_id"], req.get("job_description") or "")

    # Improvement 6: use per-requisition weights (default 0.40/0.60)
    resume_w   = float(req.get("resume_weight")   or 0.40)
    interview_w = float(req.get("interview_weight") or 0.60)
    total_w = resume_w + interview_w
    if total_w > 0:                          # normalise in case weights don't sum to 1
        resume_w /= total_w
        interview_w /= total_w

    match    = float(app["match_score"] or 0)
    bot      = result["bot_score"]
    combined = round(resume_w * match + interview_w * bot, 1)

    query(
        """UPDATE application
             SET bot_score = %s, combined_score = %s, status = 'enteri_ai_bot'
           WHERE id = %s""",
        [bot, combined, application_id], fetch=False,
    )
    log_event(application_id, "screen", "enteri_ai_bot",
              note=f"bot {bot}, combined {combined}")
    return {"bot_score": bot, "combined_score": combined}


def advance(application_id, to_status, actor_id=None, note=None):
    """
    HUMAN GATE. A recruiter taps to move an application forward or out.
    Records who did it for the audit trail.
    """
    app = query_one("SELECT status FROM application WHERE id = %s", [application_id])
    from_status = app["status"] if app else None
    query("UPDATE application SET status = %s WHERE id = %s",
          [to_status, application_id], fetch=False)
    log_event(application_id, from_status, to_status, actor_id, note)
    return query_one("SELECT * FROM application WHERE id = %s", [application_id])


def top_chart(requisition_id, limit=50):
    """Ranked chart of candidates by combined score -- what the recruiter sees
    to decide who advances past the bot round."""
    return query(
        """SELECT a.id, c.full_name, c.gender, a.match_score, a.bot_score,
                  a.combined_score, a.status, a.panel_consensus
           FROM application a JOIN candidate c ON c.id = a.candidate_id
           WHERE a.requisition_id = %s
           ORDER BY a.combined_score DESC NULLS LAST
           LIMIT %s""",
        [requisition_id, limit],
    )


def update_manual_tenure(application_id: str, avg_tenure_months: float, actor_id=None):
    """
    Recruiter-provided average tenure for a 'pending_manual' application.
    Recomputes stability_score and match_score using the four-dimension weights.
    """
    app = query_one("SELECT * FROM application WHERE id = %s", [application_id])
    if not app:
        raise ValueError("application not found")

    bd = app.get("score_breakdown") or {}
    if isinstance(bd, str):
        bd = json.loads(bd)

    stability_s = screening.compute_stability_score(avg_tenure_months)

    skills_s = float(bd.get("skills_score") or 50.0)
    exp_s    = float(bd.get("experience_score") or 50.0)
    ai_s     = float(bd.get("ai_score") or 50.0)

    w_kw  = screening.SCORE_WEIGHT_KEYWORD
    w_exp = screening.SCORE_WEIGHT_EXPERIENCE
    w_ai  = screening.SCORE_WEIGHT_AI
    w_st  = screening.SCORE_WEIGHT_STABILITY

    new_score = round(
        skills_s * w_kw + exp_s * w_exp + ai_s * w_ai + stability_s * w_st, 1
    )

    bd.update({
        "stability_score":   round(stability_s, 1),
        "stability_status":  "computed",
        "avg_tenure_months": round(avg_tenure_months, 1),
        "weights": {
            "keyword": w_kw, "experience": w_exp,
            "ai": w_ai, "stability": w_st,
        },
    })

    query(
        """UPDATE application
             SET match_score       = %s,
                 score_breakdown   = %s::jsonb,
                 avg_tenure_months = %s,
                 stability_score   = %s,
                 stability_status  = 'computed'
           WHERE id = %s""",
        [
            new_score,
            json.dumps(bd, default=_json_safe),
            round(avg_tenure_months, 1),
            round(stability_s, 1),
            application_id,
        ],
        fetch=False,
    )
    log_event(
        application_id, None, None, actor_id,
        f"manual-tenure {avg_tenure_months:.0f}m → stability {stability_s:.0f}, score {new_score}",
    )
    return {
        "match_score":       new_score,
        "stability_score":   round(stability_s, 1),
        "avg_tenure_months": round(avg_tenure_months, 1),
        "stability_status":  "computed",
    }


def rescreen_application(application_id: str, actor_id=None):
    """
    Deliberate recruiter action: re-run AI screening for a single application
    using the candidate's stored resume file. Overwrites match_score and all
    screening columns. Does NOT touch bot_score / combined_score / status.
    """
    app  = query_one("SELECT * FROM application WHERE id = %s", [application_id])
    if not app:
        raise ValueError("application not found")

    cand = query_one("SELECT * FROM candidate WHERE id = %s", [app["candidate_id"]])
    req  = query_one(
        """SELECT r.*, b.code AS band_code
           FROM requisition r JOIN band b ON b.id = r.band_id
           WHERE r.id = %s""",
        [app["requisition_id"]],
    )

    # Resolve resume text: fast path from cv_repository.raw_text (already extracted),
    # fall back to re-parsing from disk if the row is missing or text is empty.
    resume_text = ""
    try:
        _cv = query_one(
            """SELECT cv.raw_text, cv.file_path
               FROM cv_repository cv
               WHERE cv.candidate_id = %s
               ORDER BY cv.created_at DESC LIMIT 1""",
            [str(cand["id"])],
        )
    except Exception:
        _cv = None

    if _cv and _cv.get("raw_text"):
        resume_text = _cv["raw_text"]
    elif cand.get("resume_url") or (_cv and _cv.get("file_path")):
        _resume_path = (_cv or {}).get("file_path") or cand.get("resume_url") or ""
        try:
            from .resume_parser import extract_text as _parse_resume
            with open(_resume_path, "rb") as fh:
                file_bytes = fh.read()
            filename = os.path.basename(_resume_path)
            resume_text, _ = _parse_resume(file_bytes, filename)
        except Exception as exc:
            print(f"[rescreen] Could not read resume for {application_id}: {exc}")

    # Recover candidate_years from stored breakdown if available
    candidate_years = None
    old_bd = app.get("score_breakdown") or {}
    if isinstance(old_bd, str):
        old_bd = json.loads(old_bd)
    yr = old_bd.get("years")
    if yr is not None:
        try:
            candidate_years = float(yr)
        except (TypeError, ValueError):
            pass

    score, breakdown = screening.score_application(resume_text, candidate_years, req)

    ai_fit_score      = breakdown.get("ai_fit_score")
    ai_screen_detail  = json.dumps(_extract_ai_detail(breakdown), default=_json_safe)
    avg_tenure_months = breakdown.get("avg_tenure_months")
    stability_score   = breakdown.get("stability_score")
    stability_status  = breakdown.get("stability_status", "not_applicable")

    query(
        """UPDATE application
             SET match_score       = %s,
                 score_breakdown   = %s::jsonb,
                 ai_fit_score      = %s,
                 ai_screen_detail  = %s::jsonb,
                 avg_tenure_months = %s,
                 stability_score   = %s,
                 stability_status  = %s
           WHERE id = %s""",
        [
            score,
            json.dumps(breakdown, default=_json_safe),
            ai_fit_score, ai_screen_detail,
            avg_tenure_months, stability_score, stability_status,
            application_id,
        ],
        fetch=False,
    )
    log_event(application_id, app["status"], app["status"],
              actor_id, f"re-screened: {score}")
    return {
        "match_score":       score,
        "breakdown":         breakdown,
        "stability_status":  stability_status,
    }
