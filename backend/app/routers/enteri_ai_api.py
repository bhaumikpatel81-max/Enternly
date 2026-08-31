"""
Enteri AI — voice-first interview bot (14a).

Question generation is rule-based (JD + key skills).
Scoring is keyword + depth + communication weighted model.
The face/avatar (14b) is intentionally NOT built here.
"""
import base64
import html
import io
import json
import os
import secrets
import tempfile
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services import avatar as _avatar_svc
from ..services import tts as _tts_svc
from ..services import prerender as _prerender_svc
from ..services.connectors import send_email
from ..services import interviewer_llm as _llm_svc
from ..services.email_templates import render_template as _render_email_tmpl
from ..services.activity_log import log_activity
from ..services import proctoring_scorer as _proc_scorer

from ..module_access import require_tenant_module

router = APIRouter(prefix="/api/enteri-ai", tags=["enteri_ai"],
                    dependencies=[Depends(require_tenant_module("enteri_ai_tracker"))])

# ── Phase 3, Part E — server-side proctoring judge, GATED OFF ─────────────
# When False (must stay false in production until Phase 7), terminate_invite_
# session() behaves EXACTLY as before this phase: it trusts the browser's
# self-reported strike_count unconditionally. When True (dev/test only), the
# server re-computes via proctoring_scorer and only actually terminates when
# its own ledger supports it — see terminate_invite_session() below.
SERVER_SIDE_PROCTORING_JUDGE = True


def _weight_or_default(value, default: float) -> float:
    """Like `value or default` but doesn't treat an explicit 0 as unset."""
    return float(value) if value is not None else default


def _mark_invite_attempt_completed(application_id: str) -> None:
    """
    Close out the one-and-done attempt on whichever invite token drove this
    session (the currently in_progress one), so validate/begin permanently
    block re-opening the same link. A recruiter reissue (resend_enteri_ai_invite)
    is the only way to grant a fresh attempt afterwards.
    """
    query(
        """UPDATE enteri_ai_invite
           SET attempt_status = 'completed', attempt_completed_at = now()
           WHERE application_id = %s AND attempt_status = 'in_progress'""",
        [application_id], fetch=False,
    )


async def _synthesize_reply_audio(text: str) -> Optional[str]:
    """
    Synthesize `text` with the real en-IN neural voice (tts.py) and return it
    as a base64 MP3 string for the frontend to play directly — this guarantees
    the Indian-English accent regardless of what voices the candidate's browser/OS
    happens to have installed (browser SpeechSynthesis voice packs vary per machine
    and were falling back to US/UK female voices). Returns None on any failure so
    the frontend's browser-TTS fallback still takes over.
    """
    audio_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            audio_path = tf.name
        await _tts_svc.synthesize_speech(text, audio_path)
        with open(audio_path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return None
    finally:
        if audio_path:
            try:
                os.unlink(audio_path)
            except Exception:
                pass


def _build_invite_html(name: str, job: str, company: str, invite_url: str) -> str:
    from ..services.email_layout import build_branded_email

    # Preserves the exact original copy verbatim (including the <strong>48
    # hours</strong> emphasis, which build_branded_email's normal about_text
    # slot would have HTML-escaped away) as a pre-built raw block.
    about_html = f"""
          <p style="font-size:20px;font-weight:700;color:#111827;font-family:Arial,Helvetica,sans-serif;margin:0 0 20px 0">About this Interview</p>
          <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f8faff;border-radius:12px;margin-bottom:30px">
            <tr><td style="padding:22px;font-size:14px;line-height:1.8;color:#4b5563;font-family:Arial,Helvetica,sans-serif">
              Rather than asking you to upload additional documents, we&#8217;ll guide you through a short adaptive interview. Your responses help our hiring team better understand your experience and communication style alongside your application.
              <br><br>
              Please complete your interview within <strong>48 hours</strong>. You may use a desktop or mobile device with a stable internet connection. Enteri AI never auto-rejects &#8212; every score is reviewed by a human recruiter.
            </td></tr>
          </table>"""

    return build_branded_email(
        eyebrow="Application Tracking System",
        hero_title_html="Your Interview<br>is Ready.",
        hero_subtitle="We&#8217;ve reviewed your application and would like to invite you to the next stage of our hiring process.",
        hero_footer_label=job, hero_footer_value=company,
        detail_cells=[
            ("Candidate", name), ("Status", "Shortlisted"),
            ("Duration", "25 Minutes"), ("Available", "48 Hours"),
        ],
        steps=[
            ("Application Received", "Your application has been submitted.", "done"),
            ("Application Reviewed", "Our recruitment team reviewed your profile.", "done"),
            ("Interview Ready", "Complete your AI-assisted interview.", "current"),
            ("Technical Evaluation", "", "pending"),
            ("Final Discussion", "", "pending"),
        ],
        extra_body_html=about_html,
        cta_label="Start My AI Interview", cta_link=invite_url,
    )

# ── Question templates ────────────────────────────────────────────────────────

_SKILL_Q = [
    "Describe a project where you applied {skill} and what you achieved.",
    "What are the most common challenges you face with {skill}, and how do you overcome them?",
    "How do you stay current with developments in {skill}?",
    "Rate your experience level with {skill} and walk me through how you've used it.",
    "Give me a concrete example of a problem you solved using {skill}.",
]

_GENERIC_Q = [
    "Tell me about yourself and the experience most relevant to this role.",
    "Describe a time you handled a tight deadline or competing priorities.",
    "What is your biggest professional achievement in the last two years?",
    "Where do you see your career heading in the next two to three years?",
    "Why are you interested in this role specifically?",
]


def _generate_questions(key_skills: list, job_description: str, screening_questions: list = None) -> list:
    questions = []

    # Recruiter-defined screening questions come first (highest priority)
    for sq in (screening_questions or []):
        sq = sq.strip()
        if not sq:
            continue
        questions.append({
            "seq": len(questions) + 1,
            "text": sq,
            "expected_keywords": [],
            "source": "recruiter",
        })

    # Opening generic question (only if no recruiter questions yet)
    if not questions:
        questions.append({
            "seq": 1,
            "text": _GENERIC_Q[0],
            "expected_keywords": ["experience", "background", "role", "work", "team"],
            "source": "auto",
        })

    # Skill-based questions — fill remaining slots up to a total cap of 10
    cap = 10
    auto_skill_slots = max(0, cap - len(questions) - 2)  # reserve 2 for generic closing
    for i, skill in enumerate(key_skills[:auto_skill_slots]):
        tmpl = _SKILL_Q[i % len(_SKILL_Q)]
        questions.append({
            "seq": len(questions) + 1,
            "text": tmpl.format(skill=skill),
            "expected_keywords": [w.lower() for w in skill.split()] + ["project", "used", "built", "implemented"],
            "source": "auto",
        })

    # JD-derived context question
    if job_description and len(questions) < cap - 1:
        jd_words = [w for w in job_description.split() if len(w) > 5][:6]
        if jd_words:
            questions.append({
                "seq": len(questions) + 1,
                "text": f"Tell me about your experience relevant to: {', '.join(jd_words[:4])}.",
                "expected_keywords": [w.lower() for w in jd_words],
                "source": "auto",
            })

    # Closing generic questions (at most 2, if space remains)
    for gq in _GENERIC_Q[1:3]:
        if len(questions) >= cap:
            break
        questions.append({
            "seq": len(questions) + 1,
            "text": gq,
            "expected_keywords": ["deadline", "priority", "achievement", "result", "impact", "career"],
            "source": "auto",
        })

    # Re-number sequences in order
    for i, q in enumerate(questions):
        q["seq"] = i + 1

    return questions[:cap]


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_transcript(questions: list, transcript: list) -> tuple:
    answer_map = {t["seq"]: t.get("answer", "") for t in transcript}
    per_q = []
    for q in questions:
        answer = answer_map.get(q["seq"], "").lower()
        keywords = q.get("expected_keywords", [])
        words = answer.split()
        hits = sum(1 for k in keywords if k in answer)
        relevance   = min(hits / max(len(keywords), 1), 1.0)
        depth       = min(len(words) / 50.0, 1.0)
        communication = 1.0 if len(words) >= 10 else (len(words) / 10.0)
        q_score = round((relevance * 0.5 + depth * 0.3 + communication * 0.2) * 10, 1)
        per_q.append(q_score)

    raw_score = round(sum(per_q) / max(len(per_q), 1) * 10, 1)
    detail = {
        "per_question": per_q,
        "questions_answered": len([t for t in transcript if t.get("answer", "").strip()]),
        "total_questions": len(questions),
    }
    return min(raw_score, 100.0), detail


# ── Pydantic models ───────────────────────────────────────────────────────────

class StartSessionIn(BaseModel):
    application_id: str


class TranscriptEntry(BaseModel):
    seq: int
    question: str
    answer: str


class SubmitSessionIn(BaseModel):
    transcript: list[TranscriptEntry]


class QuestionIn(BaseModel):
    seq: int
    text: str
    expected_keywords: list[str] = []


class RequisitionQuestionsIn(BaseModel):
    questions: list[QuestionIn]


class ConverseIn(BaseModel):
    candidate_text: Optional[str] = None
    # True when the frontend ended this answer via the silence auto-timeout
    # rather than the candidate's own "I'm done" / Enter — the scoring LLM is
    # told to go easy on turns that may have been cut off mid-thought.
    possibly_truncated: bool = False


class TerminateSessionIn(BaseModel):
    token: str
    strike_count: int
    reason: str = ""


_APPEAL_MIN_LENGTH = 20


class AppealIn(BaseModel):
    explanation: str

    @field_validator("explanation")
    @classmethod
    def _explanation_must_have_content(cls, v):
        # Phase 5, Fix 1 — runs at request-parsing time, BEFORE create_appeal's
        # body ever executes, so an invalid submission never reaches (and
        # never consumes) the one-appeal-per-session slot below.
        stripped = (v or "").strip()
        if len(stripped) < _APPEAL_MIN_LENGTH:
            raise ValueError(
                f"explanation must be at least {_APPEAL_MIN_LENGTH} characters of actual content"
            )
        return stripped


class AppealUpdateIn(BaseModel):
    status: Optional[str] = None
    recruiter_notes: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _enteri_ai_mode() -> str:
    return os.environ.get("ENTERI_AI_MODE", "scripted").lower()


def _recruiter_owns_req(user: dict, requisition_id: str) -> bool:
    """
    True if this user may act on the given requisition.
    ta_manager/admin: always True. recruiter: only if assigned via
    requisition_recruiter. Any other role: False.
    """
    role = user.get("role")
    if role in ("ta_manager", "admin"):
        return True
    if role == "recruiter":
        row = query_one(
            """SELECT 1 FROM requisition_recruiter
               WHERE requisition_id = %s AND recruiter_id = %s""",
            [requisition_id, user["sub"]],
        )
        return bool(row)
    return False


def _application_req_id(application_id: str):
    """Resolve an application's requisition_id, or None if not found."""
    row = query_one(
        "SELECT requisition_id FROM application WHERE id = %s",
        [application_id],
    )
    return row["requisition_id"] if row else None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/sessions", status_code=201)
def start_session(body: StartSessionIn, _user: dict = Depends(get_current_user)):
    if _user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    app_row = query_one(
        """SELECT a.id, a.requisition_id, r.key_skills, r.job_description,
                  COALESCE(r.screening_questions, '{}') AS screening_questions
           FROM application a JOIN requisition r ON r.id = a.requisition_id
           WHERE a.id = %s""",
        [body.application_id],
    )
    if not app_row:
        raise HTTPException(404, "Application not found")

    key_skills = app_row["key_skills"] or []
    jd = app_row["job_description"] or ""
    sq = [q for q in (app_row["screening_questions"] or []) if q and q.strip()]
    questions = _generate_questions(key_skills, jd, sq)

    # Upsert session (one per application)
    existing = query_one(
        "SELECT id FROM enteri_ai_session WHERE application_id = %s",
        [body.application_id],
    )
    if existing:
        query(
            """UPDATE enteri_ai_session
               SET questions = %s::jsonb, status = 'in_progress',
                   started_at = now(), transcript = NULL,
                   raw_score = NULL, score_detail = NULL
               WHERE id = %s""",
            [json.dumps(questions), existing["id"]],
            fetch=False,
        )
        session_id = existing["id"]
    else:
        row = query_one(
            """INSERT INTO enteri_ai_session
               (application_id, requisition_id, questions, status, started_at)
               VALUES (%s, %s, %s::jsonb, 'in_progress', now())
               RETURNING id""",
            [body.application_id, app_row["requisition_id"], json.dumps(questions)],
        )
        session_id = row["id"]

    return {"session_id": session_id, "questions": questions}


@router.post("/sessions/{session_id}/submit")
def submit_session(
    session_id: str,
    body: SubmitSessionIn,
    _user: dict = Depends(get_current_user),
):
    if _user.get("role") == "hrbp":
        raise HTTPException(403, "Not available to the HRBP role")
    sess = query_one(
        "SELECT id, application_id, questions FROM enteri_ai_session WHERE id = %s",
        [session_id],
    )
    if not sess:
        raise HTTPException(404, "Session not found")

    questions = sess["questions"] if isinstance(sess["questions"], list) else []
    transcript = [t.dict() for t in body.transcript]
    raw_score, detail = _score_transcript(questions, transcript)

    query(
        """UPDATE enteri_ai_session
           SET transcript = %s::jsonb, raw_score = %s, score_detail = %s::jsonb,
               status = 'completed', completed_at = now()
           WHERE id = %s""",
        [json.dumps(transcript), raw_score, json.dumps(detail), session_id],
        fetch=False,
    )

    # Update application bot_score and combined_score (Improvement 6: use req weights)
    app_row = query_one(
        """SELECT a.match_score, r.resume_weight, r.interview_weight
           FROM application a
           JOIN requisition r ON r.id = a.requisition_id
           WHERE a.id = %s""",
        [sess["application_id"]],
    )
    match       = float((app_row or {}).get("match_score") or 0)
    resume_w    = _weight_or_default((app_row or {}).get("resume_weight"), 0.40)
    interview_w = _weight_or_default((app_row or {}).get("interview_weight"), 0.60)
    total_w = resume_w + interview_w
    if total_w > 0:
        resume_w /= total_w
        interview_w /= total_w
    combined = round(resume_w * match + interview_w * raw_score, 1)
    # Campus fallback: if candidate skipped resume upload, combined_score = bot_score only
    _campus_no_resume = query_one(
        "SELECT id FROM campus_candidate WHERE application_id=%s AND resume_uploaded=FALSE LIMIT 1",
        [sess["application_id"]],
    )
    if _campus_no_resume:
        combined = raw_score
    query(
        "UPDATE application SET bot_score = %s, combined_score = %s WHERE id = %s",
        [raw_score, combined, sess["application_id"]],
        fetch=False,
    )

    return {"session_id": session_id, "raw_score": raw_score, "score_detail": detail}


@router.get("/sessions/{session_id}")
def get_session(session_id: str, user: dict = Depends(get_current_user)):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    row = query_one("SELECT * FROM enteri_ai_session WHERE id = %s", [session_id])
    if not row:
        raise HTTPException(404, "Session not found")
    if not _recruiter_owns_req(user, row["requisition_id"]):
        raise HTTPException(404, "Session not found or not accessible")
    return row


@router.get("/sessions/{session_id}/render-status")
def get_render_status(session_id: str, _user: dict = Depends(get_current_user)):
    """
    Return avatar pre-render status and per-question video URLs for a session.
    Frontend polls this before the candidate starts to determine if MP4s are ready.
    render_status values: pending | rendering | ready | partial | failed
    A 'failed' or 'partial' status is not an error — the orb takes over for any
    question whose video_url is null or status is 'failed'.
    """
    row = query_one(
        "SELECT render_status, question_videos FROM enteri_ai_session WHERE id = %s",
        [session_id],
    )
    if not row:
        raise HTTPException(404, "Session not found")
    return {
        "session_id": session_id,
        "render_status": row.get("render_status") or "pending",
        "question_videos": row.get("question_videos") or [],
    }


@router.get("/sessions")
def list_sessions(
    user: dict = Depends(get_current_user),
    status: Optional[str] = None,
    score_min: Optional[float] = None,
    score_max: Optional[float] = None,
):
    """Role-scoped list of Enteri AI sessions with candidate info. Filterable."""
    role = user["role"]
    uid  = user["sub"]

    if role not in ("recruiter", "hiring_manager", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised to view Enteri AI sessions")

    join_parts  = []
    where_parts = []
    params: list = []

    where_parts.append("r.tenant_id = %s")
    params.append(user.get("tenant_id"))

    # Role scoping
    if role == "recruiter":
        join_parts.append(
            "JOIN requisition_recruiter rr_scope "
            "ON rr_scope.requisition_id = r.id AND rr_scope.recruiter_id = %s"
        )
        params.append(uid)
    elif role == "hiring_manager":
        where_parts.append("r.hiring_manager_id = %s")
        params.append(uid)
    # ta_manager / admin: sees all (now bounded to their own tenant)

    # Optional filters
    if status == "pending":
        where_parts.append("ns.status IN ('pending','in_progress')")
    elif status:
        where_parts.append("ns.status = %s")
        params.append(status)

    if score_min is not None:
        where_parts.append("ns.raw_score >= %s")
        params.append(score_min)

    if score_max is not None:
        where_parts.append("ns.raw_score <= %s")
        params.append(score_max)

    join_sql  = "\n    ".join(join_parts)
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    return query(
        f"""
        SELECT ns.id, ns.status, ns.raw_score, ns.created_at,
               ns.started_at, ns.completed_at,
               ROUND(
                 EXTRACT(EPOCH FROM (ns.completed_at - ns.started_at)) / 60.0
                 ::numeric, 1
               ) AS duration_min,
               c.full_name   AS candidate_name,
               c.email       AS candidate_email,
               r.title       AS req_title,
               r.id          AS req_id,
               a.id          AS app_id,
               rec.full_name AS recruiter_name,
               ps.id         AS proctoring_session_id,
               ps.flag_count AS proctor_flag_count,
               (ps.id IS NOT NULL) AS has_proctoring
        FROM enteri_ai_session ns
        JOIN application  a   ON a.id  = ns.application_id
        JOIN candidate    c   ON c.id  = a.candidate_id
        JOIN requisition  r   ON r.id  = ns.requisition_id
        LEFT JOIN proctoring_session ps ON ps.enteri_ai_session_id = ns.id
        {join_sql}
        LEFT JOIN LATERAL (
            SELECT u2.full_name
            FROM requisition_recruiter rr2
            JOIN app_user u2 ON u2.id = rr2.recruiter_id
            WHERE rr2.requisition_id = r.id
            ORDER BY rr2.is_owner DESC NULLS LAST LIMIT 1
        ) rec ON true
        {where_sql}
        ORDER BY ns.created_at DESC
        LIMIT 200
        """,
        params,
    )


# ── Per-Requisition Question Editor ──────────────────────────────────────────

@router.get("/requisitions/{req_id}/questions")
def get_req_questions(
    req_id: str,
    defaults: bool = False,
    user: dict = Depends(get_current_user),
):
    """
    Return the question set for a requisition.
    - defaults=False (default): return the saved custom set if one exists (saved=True),
      otherwise return auto-generated defaults without persisting (saved=False).
    - defaults=True: always return auto-generated defaults regardless of any saved set.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    if not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Requisition not found")

    req = query_one(
        "SELECT key_skills, job_description FROM requisition WHERE id = %s",
        [req_id],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    if not defaults:
        saved = query_one(
            "SELECT questions, updated_at FROM requisition_questions WHERE requisition_id = %s",
            [req_id],
        )
        if saved:
            return {
                "saved": True,
                "questions": saved["questions"],
                "updated_at": saved["updated_at"].isoformat() if saved["updated_at"] else None,
            }

    auto = _generate_questions(
        req.get("key_skills") or [], req.get("job_description") or ""
    )
    return {"saved": False, "questions": auto, "updated_at": None}


@router.put("/requisitions/{req_id}/questions")
def save_req_questions(
    req_id: str,
    body: RequisitionQuestionsIn,
    user: dict = Depends(get_current_user),
):
    """Upsert the custom question set for a requisition."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    if not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Requisition not found")

    if not query_one("SELECT id FROM requisition WHERE id = %s", [req_id]):
        raise HTTPException(404, "Requisition not found")

    if not body.questions:
        raise HTTPException(400, "At least one question is required")

    bad = [i + 1 for i, q in enumerate(body.questions) if not q.text.strip()]
    if bad:
        raise HTTPException(400, f"Question(s) {bad} have empty text")

    questions = [
        {"seq": i + 1, "text": q.text.strip(), "expected_keywords": q.expected_keywords}
        for i, q in enumerate(body.questions)
    ]
    query(
        """INSERT INTO requisition_questions (requisition_id, questions, updated_at, updated_by)
           VALUES (%s, %s::jsonb, now(), %s)
           ON CONFLICT (requisition_id)
           DO UPDATE SET questions   = EXCLUDED.questions,
                         updated_at = now(),
                         updated_by = EXCLUDED.updated_by""",
        [req_id, json.dumps(questions), user["sub"]],
        fetch=False,
    )
    return {"saved": True, "questions": questions}


@router.delete("/requisitions/{req_id}/questions")
def delete_req_questions(req_id: str, user: dict = Depends(get_current_user)):
    """Remove the saved question set — future invites revert to auto-generation."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    if not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Requisition not found")

    query(
        "DELETE FROM requisition_questions WHERE requisition_id = %s",
        [req_id],
        fetch=False,
    )
    return {"ok": True}


# ── Session Transcript (recruiter read-only) ─────────────────────────────────

@router.get("/sessions/{session_id}/transcript")
def get_session_transcript(session_id: str, user: dict = Depends(get_current_user)):
    """
    Return the full transcript or conversation for a completed Enteri AI session.
    Recruiter JWT required. Recruiters may only access sessions on their requisitions;
    TA managers and admins see all.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    scope_join = ""
    params: list = []
    if user["role"] == "recruiter":
        scope_join = (
            "JOIN requisition_recruiter rr "
            "  ON rr.requisition_id = r.id AND rr.recruiter_id = %s"
        )
        params.append(user["sub"])
    params.append(session_id)

    row = query_one(
        f"""
        SELECT ns.id, ns.transcript, ns.conversation,
               ns.raw_score, ns.score_detail, ns.status, ns.completed_at,
               c.full_name  AS candidate_name,
               c.email      AS candidate_email,
               r.title      AS requisition,
               r.id         AS requisition_id
        FROM enteri_ai_session ns
        JOIN application a  ON a.id = ns.application_id
        JOIN candidate   c  ON c.id = a.candidate_id
        JOIN requisition r  ON r.id = a.requisition_id
        {scope_join}
        WHERE ns.id = %s
        """,
        params,
    )
    if not row:
        raise HTTPException(404, "Session not found or not accessible")

    # Infer mode from which data column is populated
    mode = "conversational" if row.get("conversation") else "scripted"

    return {
        "session_id":     str(row["id"]),
        "mode":           mode,
        "status":         row["status"],
        "completed_at":   row["completed_at"].isoformat() if row["completed_at"] else None,
        "candidate_name": row["candidate_name"],
        "candidate_email":row["candidate_email"],
        "requisition":    row["requisition"],
        "requisition_id": str(row["requisition_id"]),
        "raw_score":      float(row["raw_score"]) if row["raw_score"] is not None else None,
        "score_detail":   row["score_detail"] or {},
        "transcript":     row["transcript"]   or [],
        "conversation":   row["conversation"] or [],
    }


# ── Enteri AI Invite Tracker ─────────────────────────────────────────────────────

@router.get("/invite-tracker")
def invite_tracker(user: dict = Depends(get_current_user)):
    """
    Returns all Enteri AI invites with status breakdown.
    Recruiters see only their requisitions; TA managers / admins see all.
    """
    role = user["role"]
    uid  = user["sub"]

    scope_join  = ""
    params: list = []

    if role == "recruiter":
        scope_join  = "JOIN requisition_recruiter rr_s ON rr_s.requisition_id = r.id AND rr_s.recruiter_id = %s"
        params.append(uid)
    elif role not in ("ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    scope_where = "WHERE r.tenant_id = %s"
    params.append(user.get("tenant_id"))

    # DISTINCT ON (a.id) keeps only the most-recently-sent invite per application,
    # so re-sending an invite never inflates the tracker row count.
    rows = query(
        f"""
        SELECT * FROM (
            SELECT DISTINCT ON (a.id)
                ni.id            AS invite_id,
                ni.invited_at,
                ni.expires_at,
                ni.used_at,
                c.full_name      AS candidate_name,
                c.email          AS candidate_email,
                r.id             AS req_id,
                r.title          AS requisition,
                a.id             AS app_id,
                ns.id            AS session_id,
                ns.status        AS session_status,
                ns.started_at,
                ns.completed_at,
                ns.raw_score,
                ub.full_name     AS invited_by,
                ps.id            AS proctoring_session_id,
                ps.flag_count    AS proctor_flag_count,
                CASE
                  WHEN ns.status = 'terminated_proctoring' THEN 'terminated'
                  WHEN ns.status = 'completed'    THEN 'completed'
                  WHEN ni.used_at IS NOT NULL      THEN 'in_progress'
                  WHEN ni.expires_at < now()       THEN 'expired'
                  WHEN latest_se.to_status = 'enteri_ai_invite_failed' THEN 'send_failed'
                  ELSE 'pending'
                END              AS invite_status,
                pa.status        AS appeal_status
            FROM enteri_ai_invite ni
            JOIN application  a  ON a.id  = ni.application_id
            JOIN candidate    c  ON c.id  = a.candidate_id
            JOIN requisition  r  ON r.id  = a.requisition_id
            {scope_join}
            LEFT JOIN enteri_ai_session     ns ON ns.application_id = a.id
            LEFT JOIN proctoring_session ps ON ps.enteri_ai_session_id = ns.id
            LEFT JOIN proctoring_appeal pa ON pa.application_id = a.id
            LEFT JOIN app_user          ub ON ub.id = ni.created_by
            -- Latest stage_event per application — a since-resent candidate's
            -- newer 'enteri_ai_bot' (or another) event naturally outranks an older
            -- 'enteri_ai_invite_failed' one here, so a fixed candidate stops
            -- reading as send_failed.
            LEFT JOIN LATERAL (
                SELECT se.to_status
                FROM stage_event se
                WHERE se.application_id = a.id
                ORDER BY se.occurred_at DESC
                LIMIT 1
            ) latest_se ON true
            {scope_where}
            ORDER BY a.id, ni.invited_at DESC, ps.created_at DESC
        ) latest_invite
        ORDER BY invited_at DESC
        """,
        params,
    )

    # Build summary counts
    counts = {"total": 0, "pending": 0, "in_progress": 0, "completed": 0, "expired": 0,
               "terminated": 0, "send_failed": 0}
    for r in (rows or []):
        counts["total"] += 1
        s = r.get("invite_status", "pending")
        if s in counts:
            counts[s] += 1

    return {"summary": counts, "invites": rows or []}


# ── Base-URL helpers ──────────────────────────────────────────────────────────

def _get_base_url() -> tuple[str, str]:
    """
    Resolve the effective base URL for candidate invite links.
    Returns (url, source) where source is 'db' | 'env' | 'default'.
    Reads from system_settings at call time — never cached, so a Settings
    save takes effect immediately for the next invite without a restart.
    """
    from ..services.connectors import _load_email_cfg
    db_val = (_load_email_cfg().get("base_url") or "").strip()
    if db_val:
        return db_val.rstrip("/"), "db"
    env_val = os.environ.get("APP_BASE_URL", "").strip()
    if env_val:
        return env_val.rstrip("/"), "env"
    return "http://localhost:8080", "default"


def _is_localhost(url: str) -> bool:
    return any(x in url for x in ("localhost", "127.0.0.1", "0.0.0.0"))


@router.get("/base-url-status")
def base_url_status(user: dict = Depends(get_current_user)):
    """Return the currently-resolved invite base URL and whether it is a localhost URL."""
    url, source = _get_base_url()
    return {
        "effective_base_url": url,
        "is_localhost": _is_localhost(url),
        "source": source,
    }


# ── Candidate Invite Flow ─────────────────────────────────────────────────────

def _write_invite_failure_event(app_id: str, user: dict, email_error: str) -> None:
    """
    Records an honest 'send failed' stage_event for an invite attempt (template
    render error or SMTP send error alike) without advancing the application —
    this is the single signal the invite tracker keys off of to distinguish a
    failed send from a merely-unopened invite. Uses a synthetic to_status (not
    a real pipeline stage) so SLA/time-in-stage queries, which match
    stage_event.to_status against the application's *current* status, don't
    mistake this for a real transition.
    """
    _cur = query_one("SELECT status FROM application WHERE id=%s", [app_id])
    if _cur and _cur["status"] in ("applied", "screen", "ai_screening", "screening", "screen_passed"):
        query(
            "INSERT INTO stage_event (application_id, from_status, to_status, actor_id, note) VALUES (%s,%s,'enteri_ai_invite_failed',%s,%s)",
            [app_id, _cur["status"], user["sub"],
             f"Enteri AI invite send failed — awaiting resend ({email_error})"],
            fetch=False,
        )


def _do_single_invite(app_id: str, user: dict, background_tasks: BackgroundTasks) -> dict:
    """
    Core invite logic — shared by single-invite and bulk-invite endpoints.
    Returns the invite result dict; does NOT raise HTTP exceptions so bulk
    callers can record per-item failures without aborting the batch.
    """

    app_row = query_one(
        """SELECT a.id, a.status, c.full_name, c.email,
                  r.id AS requisition_id, r.title AS job_title,
                  r.key_skills, r.job_description,
                  gc.name AS company
           FROM application a
           JOIN candidate   c  ON c.id = a.candidate_id
           JOIN requisition r  ON r.id = a.requisition_id
           JOIN business_unit bu ON bu.id = r.bu_id
           JOIN group_company gc ON gc.id = bu.company_id
           WHERE a.id = %s""",
        [app_id],
    )
    if not app_row:
        return {"status": "error", "reason": "Application not found", "app_id": app_id}
    if not app_row["email"]:
        return {"status": "skipped", "reason": "no_email", "app_id": app_id,
                "candidate_name": app_row.get("full_name")}

    # Skip if an active (non-expired, non-used) invite already exists
    active = query_one(
        """SELECT id FROM enteri_ai_invite
           WHERE application_id=%s AND used_at IS NULL AND expires_at > now()
           LIMIT 1""",
        [app_id],
    )
    if active:
        return {"status": "skipped", "reason": "active_invite_exists",
                "app_id": app_id, "candidate_name": app_row.get("full_name")}

    token = secrets.token_urlsafe(32)
    query(
        """INSERT INTO enteri_ai_invite (application_id, token, created_by)
           VALUES (%s, %s, %s)""",
        [app_id, token, user["sub"]],
        fetch=False,
    )

    # Create the enteri_ai_session now (if not already present) so avatar videos can
    # be pre-rendered before the candidate opens their link.
    # start_invited_session preserves these questions, keeping video URLs valid.
    #
    # Question source priority:
    #   1. Saved custom set on requisition_questions (recruiter has edited it).
    #   2. Auto-generation from key_skills + job_description (original behaviour,
    #      used for every requisition that has never been edited).
    _saved_qs = query_one(
        "SELECT questions FROM requisition_questions WHERE requisition_id = %s",
        [app_row["requisition_id"]],
    )
    _questions = (
        list(_saved_qs["questions"]) if _saved_qs
        else _generate_questions(app_row.get("key_skills") or [], app_row.get("job_description") or "")
    )
    _existing_sess = query_one(
        "SELECT id FROM enteri_ai_session WHERE application_id = %s", [app_id]
    )
    if _existing_sess:
        _prerender_session_id = _existing_sess["id"]
    else:
        _sess_row = query_one(
            """INSERT INTO enteri_ai_session
               (application_id, requisition_id, questions, status)
               VALUES (%s, %s, %s::jsonb, 'pending') RETURNING id""",
            [app_id, app_row["requisition_id"], json.dumps(_questions)],
        )
        _prerender_session_id = _sess_row["id"]

    # Fire avatar pre-render as a background task -- the fast path, usually
    # done before the candidate opens their link. Completely safe when GPU is
    # not deployed — pipeline logs a warning and exits, leaving all
    # question_videos as failed so the frontend orb takes over.
    # If this in-process task is silently dropped by a worker restart before
    # it runs, services/enteri_ai_render_worker.py is a durable periodic sweep
    # that retries any session stuck at render_status='pending' (or 'failed'
    # with attempts left) -- see migration 65.
    background_tasks.add_task(
        _prerender_svc.prerender_interview_videos, _prerender_session_id
    )

    # Resolve base URL from DB settings (reads live — no restart needed after save)
    base_url, _bu_source = _get_base_url()
    if _is_localhost(base_url):
        print(
            f"[enteri-ai-invite] WARNING: invite URL is localhost ({base_url}) — "
            f"candidate {app_row['email']} will receive a broken link. "
            f"Set Public Base URL in Admin → Settings before sending real invites."
        )
    invite_url = f"{base_url}/enteri-ai-interview?token={token}"

    name    = app_row["full_name"]
    job     = app_row["job_title"]
    company = app_row["company"]

    from ..services.connectors import resolve_global_placeholders as _resolve_globals
    _globals = _resolve_globals(req_id=str(app_row["requisition_id"]), actor=user)
    _reply_to = _globals.get("recruiter_email") or None

    try:
        email_subject, plain = _render_email_tmpl("enteri_ai_invite", {
            "candidate_name": name,
            "job_title":      job,
            "company_name":   company,
            "interview_link": invite_url,
        }, req_id=str(app_row["requisition_id"]), actor=user)
    except ValueError as _tmpl_err:
        email_sent  = False
        email_error = (
            f"Email template has unfillable placeholder: {_tmpl_err}. "
            "Fix the 'Enteri AI Invite' template in Email Templates settings."
        )
        print(f"[enteri-ai-invite] {email_error}")
        log_activity(
            "enteri_ai_session", "enteri_ai_invite_failed",
            application_id=app_id, requisition_id=app_row["requisition_id"],
            actor_id=user["sub"], actor_role=user.get("role"),
            detail={"reason": "template_error", "error": email_error},
        )
        _write_invite_failure_event(app_id, user, email_error)
        return {
            "invite_url":     invite_url,
            "sent_to":        app_row["email"],
            "email_sent":     False,
            "email_error":    email_error,
            "candidate_name": name,
            "job_title":      job,
        }

    html = _build_invite_html(name=name, job=job, company=company, invite_url=invite_url)

    try:
        send_email(app_row["email"], email_subject, plain, html=html, reply_to=_reply_to, tenant_id=user.get("tenant_id"))
        email_sent  = True
        email_error = None
    except Exception as exc:
        email_sent  = False
        email_error = str(exc)
        print(f"[enteri-ai-invite] Email delivery failed: {exc}")

    log_activity(
        "enteri_ai_session", "enteri_ai_invite_sent",
        application_id=app_id, requisition_id=app_row["requisition_id"],
        actor_id=user["sub"], actor_role=user.get("role"),
        detail={"email_sent": email_sent, "email_error": email_error, "sent_to": app_row["email"]},
    )

    # Advance application to enteri_ai_bot stage only if the invite email actually went
    # out — a failed send must not silently move the candidate forward. On failure,
    # log an honest stage_event with a synthetic to_status (not a real pipeline
    # stage) so SLA/time-in-stage queries, which match stage_event.to_status against
    # the application's *current* status, don't mistake this for a real transition.
    _cur = query_one("SELECT status FROM application WHERE id=%s", [app_id])
    if _cur and _cur["status"] in ("applied", "screen", "ai_screening", "screening", "screen_passed"):
        if email_sent:
            query(
                "INSERT INTO stage_event (application_id, from_status, to_status, actor_id, note) VALUES (%s,%s,'enteri_ai_bot',%s,'Enteri AI invite sent')",
                [app_id, _cur["status"], user["sub"]], fetch=False,
            )
            query("UPDATE application SET status='enteri_ai_bot' WHERE id=%s", [app_id], fetch=False)
        else:
            _write_invite_failure_event(app_id, user, email_error)

    return {
        "invite_url":     invite_url,
        "sent_to":        app_row["email"],
        "email_sent":     email_sent,
        "email_error":    email_error if not email_sent else None,
        "candidate_name": name,
        "job_title":      job,
    }


@router.post("/invite/send/{app_id}", status_code=201)
def create_enteri_ai_invite(
    app_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Recruiter sends an AI interview invite link to the candidate's email."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req_id = _application_req_id(app_id)
    if req_id is None:
        raise HTTPException(404, "Application not found")
    if not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Application not found")
    result = _do_single_invite(app_id, user, background_tasks)
    # Surface skip/error reasons as HTTP errors for single-invite UI
    if result.get("status") == "error":
        raise HTTPException(404, result.get("reason", "Not found"))
    if result.get("status") == "skipped":
        raise HTTPException(400, result.get("reason", "Skipped"))
    return result


class BulkInviteIn(BaseModel):
    application_ids: list[str]


@router.post("/bulk-invite")
def bulk_enteri_ai_invite(
    body: BulkInviteIn,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """
    Send Enteri AI invites to multiple candidates.
    Role scope: recruiter only for applications on their assigned reqs.
    Returns per-application results: sent / skipped(reason) / failed(error).
    """
    import time

    role = user["role"]
    uid  = user["sub"]
    if role not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    results: list[dict] = []
    for app_id in body.application_ids:
        # Recruiter scope check — must own the requisition via requisition_recruiter
        if role == "recruiter":
            scope = query_one(
                """SELECT 1 FROM application a
                   JOIN requisition_recruiter rr ON rr.requisition_id = a.requisition_id
                   WHERE a.id=%s AND rr.recruiter_id=%s""",
                [app_id, uid],
            )
            if not scope:
                results.append({
                    "app_id":  app_id,
                    "status":  "skipped",
                    "reason":  "not_your_requisition",
                })
                continue

        # Check stage is appropriate for Enteri AI invites
        app_check = query_one(
            "SELECT status, id FROM application WHERE id=%s", [app_id]
        )
        if not app_check:
            results.append({"app_id": app_id, "status": "error", "reason": "not_found"})
            continue
        if app_check["status"] not in ("applied", "screen", "enteri_ai_bot"):
            results.append({
                "app_id":  app_id,
                "status":  "skipped",
                "reason":  f"wrong_stage:{app_check['status']}",
            })
            continue

        try:
            r = _do_single_invite(app_id, user, background_tasks)
            results.append({**r, "app_id": app_id})
        except Exception as exc:
            results.append({"app_id": app_id, "status": "failed", "reason": str(exc)})

        # ~1s delay between invites to avoid SMTP burst
        time.sleep(1.0)

    sent    = sum(1 for r in results if r.get("email_sent"))
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    failed  = sum(1 for r in results if r.get("status") in ("error", "failed"))
    return {
        "sent":    sent,
        "skipped": skipped,
        "failed":  failed,
        "results": results,
    }


@router.post("/resend-invite/{app_id}", status_code=201)
def resend_enteri_ai_invite(
    app_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """
    Creates a fresh invite token for an application and resends the email.
    Used for expired/pending-too-long/stuck-in-progress invites, and — per the
    one-attempt-per-invite policy (see start_invited_session) — this is also
    the ONLY way to grant a candidate a new attempt after they've already
    completed (or been proctoring-terminated on) a prior one.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    # Revoke every not-yet-revoked prior invite for this application so its
    # token permanently blocks re-entry (validate_invite / start_invited_session
    # both key off attempt_status) even if the candidate still has it open.
    query(
        """UPDATE enteri_ai_invite
           SET attempt_status = 'revoked',
               expires_at = LEAST(expires_at, now() - interval '1 second')
           WHERE application_id = %s AND attempt_status != 'revoked'""",
        [app_id], fetch=False,
    )

    # Reset the session so the new token's begin() creates a genuinely fresh
    # attempt instead of hitting the "already completed" guard — mirrors the
    # reset relink_appeal() already does for proctoring-terminated sessions.
    # Pre-rendered questions are intentionally left untouched so any already-
    # rendered avatar video URLs remain valid.
    query(
        """UPDATE enteri_ai_session
           SET status = 'pending', started_at = NULL,
               conversation = NULL, transcript = NULL,
               raw_score = NULL, score_detail = NULL, termination_reason = NULL,
               completed_at = NULL, email_sent = FALSE
           WHERE application_id = %s""",
        [app_id], fetch=False,
    )

    # Delegate to the main invite creator (re-triggers prerender; cache hits are instant)
    result = create_enteri_ai_invite(app_id, background_tasks, user)

    # Link the revoked token(s) to their replacement for audit/support purposes
    new_token = (result.get("invite_url") or "").rsplit("token=", 1)[-1]
    if new_token:
        query(
            """UPDATE enteri_ai_invite
               SET superseded_by_token = %s
               WHERE application_id = %s AND attempt_status = 'revoked' AND token != %s""",
            [new_token, app_id, new_token], fetch=False,
        )
    return result


@router.get("/invite/validate")
def validate_invite(token: str):
    """Public — validate a candidate interview token before showing the interview page."""
    row = query_one(
        """SELECT ni.id, ni.expires_at, ni.used_at, ni.attempt_status,
                  c.full_name, r.title AS job_title, gc.name AS company,
                  ni.application_id, ns.status AS session_status
           FROM enteri_ai_invite ni
           JOIN application  a  ON a.id  = ni.application_id
           JOIN candidate    c  ON c.id  = a.candidate_id
           JOIN requisition  r  ON r.id  = a.requisition_id
           JOIN business_unit bu ON bu.id = r.bu_id
           JOIN group_company gc ON gc.id = bu.company_id
           LEFT JOIN enteri_ai_session ns ON ns.application_id = ni.application_id
           WHERE ni.token = %s""",
        [token],
    )
    if not row:
        return {"valid": False, "reason": "This interview link is invalid."}
    # One attempt per invite: a token that has already been completed or was
    # superseded by a recruiter reissue is permanently closed — the candidate
    # must contact their recruiter for a fresh link (see reissue_invite()).
    if row["attempt_status"] in ("completed", "revoked"):
        return {"valid": False, "reason": "already_completed"}
    # Permanently closed after completion; terminated sessions show the appeal screen instead
    if row["session_status"] == "terminated_proctoring":
        return {"valid": False, "reason": "terminated_proctoring"}
    if row["session_status"] == "completed":
        return {"valid": False, "reason": "already_completed"}
    exp = row["expires_at"]
    if exp and exp.replace(tzinfo=None) < datetime.utcnow():
        return {"valid": False, "reason": "This interview link has expired."}
    # Detect campus invite so the interview page can show the resume upload widget
    _campus_c = query_one(
        "SELECT id FROM campus_candidate WHERE application_id=%s LIMIT 1",
        [str(row["application_id"])],
    )
    return {
        "valid": True,
        "candidate_name": row["full_name"],
        "job_title": row["job_title"],
        "company": row["company"],
        "application_id": str(row["application_id"]),
        "mode": _enteri_ai_mode(),
        "is_campus": _campus_c is not None,
    }


@router.post("/invite/begin")
def start_invited_session(token: str):
    """Public — candidate starts (or resumes) a Enteri AI session.

    Policy — one attempt per invite token:
    - unused: first entry, flips to in_progress and creates a fresh session.
    - in_progress: idempotent resume — the SAME session/conversation is returned
      as-is (no reset). This is what makes a page refresh / duplicate tab safe:
      it never wipes progress or re-triggers the opening greeting.
    - completed / revoked: permanently blocked. A recruiter must issue a fresh
      link via /api/enteri-ai/resend-invite/{app_id}, which creates a new token and
      resets the session for a new attempt.
    """
    invite = query_one(
        """SELECT ni.id, ni.application_id, ni.expires_at, ni.used_at, ni.attempt_status
           FROM enteri_ai_invite ni WHERE ni.token = %s""",
        [token],
    )
    if not invite:
        raise HTTPException(400, "Invalid invite token")
    if invite["attempt_status"] in ("completed", "revoked"):
        raise HTTPException(
            409,
            "This interview has already been completed. Please contact your "
            "recruiter to request a new link.",
        )
    exp = invite["expires_at"]
    if exp and exp.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(400, "This interview link has expired")

    app_row = query_one(
        """SELECT a.id, a.requisition_id, r.key_skills, r.job_description,
                  COALESCE(r.screening_questions, '{}') AS screening_questions
           FROM application a JOIN requisition r ON r.id = a.requisition_id
           WHERE a.id = %s""",
        [invite["application_id"]],
    )
    if not app_row:
        raise HTTPException(404, "Application not found")

    existing = query_one(
        "SELECT id, status, questions FROM enteri_ai_session WHERE application_id = %s",
        [invite["application_id"]],
    )
    # Belt-and-suspenders: a session can only be 'completed' if this invite's
    # attempt_status is also 'completed' (stamped together — see converse_invite
    # / terminate_invite_session / submit_invited_session), which is already
    # blocked above. Guard again in case the two ever drift out of sync.
    if existing and existing["status"] == "completed":
        raise HTTPException(409, "This interview has already been completed")

    # First entry: stamp used_at/attempt_started_at and shrink the expiry to a
    # 48-hour window. Re-entries (attempt_status already 'in_progress') skip this.
    _is_first_entry = invite["attempt_status"] == "unused"
    if _is_first_entry:
        query(
            """UPDATE enteri_ai_invite
               SET used_at = COALESCE(used_at, now()),
                   expires_at = now() + INTERVAL '48 hours',
                   attempt_status = 'in_progress',
                   attempt_started_at = now()
               WHERE id = %s""",
            [invite["id"]], fetch=False,
        )

    if existing:
        # Resume in place — do NOT touch conversation/transcript/scores, so a
        # refresh or duplicate tab never wipes progress or replays the greeting.
        query(
            """UPDATE enteri_ai_session
               SET status = 'in_progress', started_at = COALESCE(started_at, now())
               WHERE id = %s""",
            [existing["id"]], fetch=False,
        )
        questions = existing.get("questions") or []
        if not questions:
            sq = [q for q in (app_row["screening_questions"] or []) if q and q.strip()]
            questions = _generate_questions(app_row["key_skills"] or [], app_row["job_description"] or "", sq)
            query(
                "UPDATE enteri_ai_session SET questions = %s::jsonb WHERE id = %s",
                [json.dumps(questions), existing["id"]], fetch=False,
            )
        session_id = existing["id"]
    else:
        sq = [q for q in (app_row["screening_questions"] or []) if q and q.strip()]
        questions = _generate_questions(app_row["key_skills"] or [], app_row["job_description"] or "", sq)
        row = query_one(
            """INSERT INTO enteri_ai_session
               (application_id, requisition_id, questions, status, started_at)
               VALUES (%s, %s, %s::jsonb, 'in_progress', now()) RETURNING id""",
            [invite["application_id"], app_row["requisition_id"], json.dumps(questions)],
        )
        session_id = row["id"]

    if _is_first_entry:
        log_activity(
            "enteri_ai_session", "enteri_ai_session_started",
            entity_id=session_id, application_id=invite["application_id"],
            requisition_id=app_row["requisition_id"],
            actor_id=None, actor_role="candidate",
        )

    return {"session_id": session_id, "questions": questions}


@router.get("/invite/render-status")
def get_invite_render_status(token: str):
    """Public — candidate polls avatar pre-render status using their invite token."""
    inv = query_one(
        """SELECT ni.application_id
             FROM enteri_ai_invite ni
            WHERE ni.token = %s AND ni.used_at IS NOT NULL""",
        [token],
    )
    if not inv:
        raise HTTPException(404, "Session not found")
    row = query_one(
        """SELECT id, render_status, question_videos
             FROM enteri_ai_session
            WHERE application_id = %s""",
        [inv["application_id"]],
    )
    if not row:
        raise HTTPException(404, "Session not found")
    return {
        "session_id": str(row["id"]),
        "render_status": row.get("render_status") or "pending",
        "question_videos": row.get("question_videos") or [],
    }


# ── Completion email helpers ──────────────────────────────────────────────────

def _esc(s: str) -> str:
    return html.escape(str(s or ""))


def _build_completion_email_html(
    candidate_name: str,
    requisition_title: str,
    raw_score,
    score_detail: dict,
    transcript: list,
    conversation: list,
) -> str:
    sd   = score_detail or {}
    mode = "conversational" if conversation else "scripted"

    detail_html = ""
    if sd.get("strengths"):
        detail_html += (
            "<h3 style='margin:16px 0 4px;color:#1a7f37'>Strengths</h3>"
            f"<p style='margin:0 0 12px;line-height:1.5'>{_esc(sd['strengths'])}</p>"
        )
    if sd.get("concerns"):
        detail_html += (
            "<h3 style='margin:16px 0 4px;color:#b55c00'>Areas to Probe</h3>"
            f"<p style='margin:0 0 12px;line-height:1.5'>{_esc(sd['concerns'])}</p>"
        )
    if mode == "conversational" and isinstance(sd.get("per_dimension"), dict):
        pd = sd["per_dimension"]
        dim_rows = "".join(
            f"<tr><td style='padding:4px 12px 4px 0;color:#555'>{dim.title()}</td>"
            f"<td><span style='display:inline-block;width:{int(pd.get(dim, 0)) * 10}%;"
            f"max-width:120px;height:8px;background:#2d8cf0;border-radius:2px;min-width:2px'>"
            f"</span>&nbsp;<span style='font-size:12px;color:#555'>"
            f"{pd.get(dim, 0)}/10</span></td></tr>"
            for dim in ("relevance", "depth", "communication", "fit")
        )
        detail_html += (
            f"<h3 style='margin:16px 0 6px'>Dimension Scores</h3>"
            f"<table style='border-spacing:0'>{dim_rows}</table>"
        )
    elif mode == "scripted" and sd.get("questions_answered") is not None:
        detail_html += (
            f"<p style='margin:4px 0'><b>Questions answered:</b> "
            f"{sd['questions_answered']} / {sd.get('total_questions', '?')}</p>"
        )

    if mode == "conversational":
        turn_rows = ""
        for turn in (conversation or []):
            spk   = turn.get("speaker", "")
            label = "Enteri AI" if spk == "bot" else "Candidate"
            color = "#2d8cf0" if spk == "bot" else "#444"
            turn_rows += (
                f"<tr style='border-bottom:1px solid #f0f0f0'>"
                f"<td style='padding:7px 14px 7px 0;font-weight:600;color:{color};"
                f"white-space:nowrap;vertical-align:top'>{label}</td>"
                f"<td style='padding:7px 0;line-height:1.5;color:#222'>"
                f"{_esc(turn.get('text', ''))}</td></tr>"
            )
        transcript_html = (
            f"<table style='width:100%;border-collapse:collapse'>{turn_rows}</table>"
        )
    else:
        qa_blocks = ""
        for i, qa in enumerate(transcript or [], 1):
            qa_blocks += (
                f"<div style='margin-bottom:16px'>"
                f"<p style='margin:0 0 4px;font-weight:600;color:#222'>"
                f"Q{i}: {_esc(qa.get('question', ''))}</p>"
                f"<p style='margin:0;color:#444;line-height:1.5;padding-left:12px;"
                f"border-left:3px solid #ddd'>{_esc(qa.get('answer', ''))}</p></div>"
            )
        transcript_html = qa_blocks or "<p style='color:#888'>No transcript recorded.</p>"

    score_val = int(raw_score) if raw_score is not None else None
    score_str = f"{score_val}/100" if score_val is not None else "N/A"

    # detail_html/transcript_html are genuinely dynamic, variable-length rich
    # content (score bars, per-turn transcript) that don't fit the shared
    # template's fixed detail-card/steps/about-text slots -- passed through
    # as a pre-built, already-escaped raw block instead of forcing a lossy
    # redesign onto them.
    from ..services.email_layout import build_branded_email
    extra_html = f"""
      <p style="font-size:20px;font-weight:700;color:#111827;font-family:Arial,Helvetica,sans-serif;margin:0 0 20px 0">Full Interview Transcript</p>
      <div style="font-family:Arial,sans-serif;color:#222;margin-bottom:20px">{detail_html}{transcript_html}</div>"""

    return build_branded_email(
        eyebrow="Application Tracking System",
        hero_title_html="Interview<br>Completed.",
        hero_subtitle="Enteri AI has finished the AI-assisted interview — the summary and full transcript are ready for your review.",
        detail_cells=[
            ("Candidate", candidate_name), ("Role", requisition_title),
            ("AI Score", score_str),
        ],
        extra_body_html=extra_html,
        cta_label=None, cta_link=None,
        footer_note="This email was sent automatically by Enteri AI. Do not reply.",
    )


def _fire_completion_email(session_id: str) -> None:
    """Background task — resolve recruiter, guard on email_sent, send, mark sent."""
    try:
        row = query_one(
            """SELECT u.email        AS recruiter_email,
                      u.full_name    AS recruiter_name,
                      c.full_name    AS candidate_name,
                      r.title        AS requisition_title,
                      r.id           AS requisition_id,
                      r.tenant_id    AS tenant_id,
                      ns.raw_score, ns.score_detail,
                      ns.transcript, ns.conversation,
                      ns.email_sent
               FROM enteri_ai_session  ns
               JOIN application    a  ON a.id  = ns.application_id
               JOIN candidate      c  ON c.id  = a.candidate_id
               JOIN requisition    r  ON r.id  = a.requisition_id
               JOIN enteri_ai_invite   ni ON ni.application_id = ns.application_id
               JOIN app_user       u  ON u.id  = ni.created_by
               WHERE ns.id = %s
               ORDER BY ni.invited_at DESC
               LIMIT 1""",
            [session_id],
        )
        if not row or row["email_sent"]:
            return

        sd   = row["score_detail"] or {}
        conv = row["conversation"] or []
        txn  = row["transcript"]   or []

        html_body = _build_completion_email_html(
            candidate_name=row["candidate_name"],
            requisition_title=row["requisition_title"],
            raw_score=row["raw_score"],
            score_detail=sd,
            transcript=txn,
            conversation=conv,
        )
        score_display = (
            f"{int(row['raw_score'])}/100"
            if row["raw_score"] is not None
            else "N/A"
        )
        _compl_actor = {"email": row["recruiter_email"], "full_name": row["recruiter_name"]}
        _compl_req_id = str(row["requisition_id"]) if row.get("requisition_id") else None
        try:
            _et_subj, plain = _render_email_tmpl("enteri_ai_completion", {
                "candidate_name": row["candidate_name"],
                "job_title":      row["requisition_title"],
                "ai_score":       score_display,
                "strengths":      sd.get("strengths") or "—",
                "concerns":       sd.get("concerns") or "—",
            }, req_id=_compl_req_id, actor=_compl_actor)
        except ValueError as _te:
            print(f"[enteri_ai_email] template error for session {session_id}: {_te}")
            return
        send_email(
            to_email=row["recruiter_email"],
            subject=_et_subj,
            body=plain,
            html=html_body,
            reply_to=row["recruiter_email"],
            tenant_id=row.get("tenant_id"),
        )
        query(
            "UPDATE enteri_ai_session SET email_sent = TRUE WHERE id = %s",
            [session_id],
            fetch=False,
        )
    except Exception as exc:
        print(f"[enteri_ai_email] completion email failed for session {session_id}: {exc}")
        # email_sent stays FALSE forever with no retry -- log durably so it's
        # at least discoverable on the Activity Timeline instead of only stdout.
        log_activity(
            "enteri_ai_session", "enteri_ai_completion_email_failed",
            entity_id=session_id, actor_id=None, actor_role="system",
            detail={"error": str(exc)},
        )


@router.post("/invite/converse")
async def converse_invite(token: str, body: ConverseIn, background_tasks: BackgroundTasks):
    """
    Public — drive one turn of a conversational (LLM-led) Enteri AI interview.

    Call with an empty/absent candidate_text on the very first turn to get the
    bot's opening question. Subsequent calls should include the candidate's spoken
    response. The endpoint returns the bot's next reply and signals when the
    interview is complete (is_complete=true), at which point the session is scored
    and written to the database exactly as the scripted submit flow does.

    Only active when ENTERI_AI_MODE=conversational.
    """
    if _enteri_ai_mode() != "conversational":
        raise HTTPException(400, "Conversational mode is not enabled (ENTERI_AI_MODE=scripted)")

    # ── Token validation (mirrors start_invited_session) ─────────────────────
    invite = query_one(
        "SELECT id, application_id, expires_at, used_at FROM enteri_ai_invite WHERE token = %s",
        [token],
    )
    if not invite:
        raise HTTPException(400, "Invalid invite token")
    exp = invite["expires_at"]
    if exp and exp.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(400, "This interview link has expired")

    # ── Load session + role context ───────────────────────────────────────────
    sess = query_one(
        """SELECT ns.id, ns.status, ns.conversation, ns.application_id,
                  r.title, r.key_skills, r.job_description, r.is_fresher_role,
                  c.full_name AS candidate_name,
                  gc.name     AS company
           FROM enteri_ai_session ns
           JOIN application   a  ON a.id  = ns.application_id
           JOIN candidate      c  ON c.id  = a.candidate_id
           JOIN requisition    r  ON r.id  = a.requisition_id
           JOIN business_unit  bu ON bu.id = r.bu_id
           JOIN group_company  gc ON gc.id = bu.company_id
           WHERE ns.application_id = %s""",
        [invite["application_id"]],
    )
    if not sess:
        raise HTTPException(404, "Session not found — call /api/enteri-ai/invite/begin first")
    if sess["status"] in ("completed", "terminated_proctoring"):
        raise HTTPException(400, "This interview has already been completed")

    turns = list(sess["conversation"] or [])
    candidate_text = (body.candidate_text or "").strip()

    # ── Honest duration estimate spoken in the opening line ──────────────────
    _CONV_DURATION_ESTIMATE = "25 to 30 minutes"  # edit here to change the spoken estimate

    # ── First call: return hardcoded intro without hitting the LLM ───────────
    if not turns and not candidate_text:
        first_name = (sess["candidate_name"] or "").split()[0]
        greeting   = f"Hi, {first_name}!" if first_name else "Hi!"
        job_title  = sess["title"]  or "this role"
        company    = sess["company"] or "the company"
        # The "..." is deliberate -- edge-tts/gTTS/browser SpeechSynthesis all read
        # an ellipsis as a noticeably longer pause than a period (~1s), giving the
        # candidate a beat after the self-introduction before the rest of the intro.
        # None of these TTS paths accept raw SSML <break> tags through this API.
        intro = (
            f"{greeting} I'm Enteri AI, an AI interviewer from Enternly... "
            f"Thank you for applying for the {job_title} position at {company}. "
            f"I'll be conducting a brief screening interview today — it should take around "
            f"{_CONV_DURATION_ESTIMATE}, and I'll ask you a few questions about your experience and skills. "
            f"Just answer naturally, as you would with a human interviewer. "
            f"Whenever you're ready to begin, simply say yes."
        )
        turns.append({"speaker": "bot", "text": intro})
        query(
            "UPDATE enteri_ai_session SET conversation = %s::jsonb WHERE id = %s",
            [json.dumps(turns), sess["id"]],
            fetch=False,
        )
        return {"reply": intro, "is_complete": False, "audio_b64": await _synthesize_reply_audio(intro)}

    # Append candidate's reply for subsequent turns
    if candidate_text:
        turn = {"speaker": "candidate", "text": candidate_text}
        if body.possibly_truncated:
            # Silence auto-timeout may have cut the candidate off mid-thought —
            # scoring should go easy on this turn rather than marking it incomplete.
            turn["truncated"] = True
        turns.append(turn)
        # Persist BEFORE calling the LLM: if next_turn() below fails/times out,
        # the candidate's just-spoken answer must not vanish -- previously it
        # only reached the DB after a successful reply, so an LLM hiccup lost
        # the turn from the transcript/scoring forever and raised a raw 500.
        query(
            "UPDATE enteri_ai_session SET conversation = %s::jsonb WHERE id = %s",
            [json.dumps(turns), sess["id"]],
            fetch=False,
        )

    role_context = {
        "title": sess["title"] or "",
        "key_skills": sess["key_skills"] or [],
        "job_description": sess["job_description"] or "",
        "is_fresher_role": bool(sess.get("is_fresher_role")),
    }
    conversation_state = {"role_context": role_context, "turns": turns}

    # ── Get bot's next reply ──────────────────────────────────────────────────
    try:
        result = await _llm_svc.next_turn(conversation_state)
    except Exception as exc:
        # The candidate's turn is already persisted above -- degrade
        # gracefully instead of losing it behind a raw 500. Nothing is
        # appended to `turns`/DB for this failed attempt, so the candidate's
        # next submission retries next_turn() against the same saved state.
        log_activity(
            "enteri_ai_session", "enteri_ai_turn_failed",
            entity_id=str(sess["id"]), application_id=sess["application_id"],
            actor_id=None, actor_role="system",
            detail={"error": str(exc)},
        )
        retry_reply = "Sorry, I didn't quite catch that — could you say that again?"
        return {"reply": retry_reply, "is_complete": False,
                "audio_b64": await _synthesize_reply_audio(retry_reply)}
    reply       = result["reply"]
    is_complete = result["is_complete"]

    turns.append({"speaker": "bot", "text": reply})

    # ── If interview is done: score and write final results ───────────────────
    if is_complete:
        conversation_state["turns"] = turns
        score_result = await _llm_svc.score_transcript(conversation_state)
        raw_score = score_result["raw_score"]
        detail    = score_result["score_detail"]

        query(
            """UPDATE enteri_ai_session
               SET conversation = %s::jsonb,
                   raw_score = %s, score_detail = %s::jsonb,
                   status = 'completed', completed_at = now()
               WHERE id = %s""",
            [json.dumps(turns), raw_score, json.dumps(detail), sess["id"]],
            fetch=False,
        )
        _mark_invite_attempt_completed(sess["application_id"])

        # Improvement 6: use per-requisition weights for conversational mode
        app_row = query_one(
            """SELECT a.match_score, r.resume_weight, r.interview_weight
               FROM application a
               JOIN requisition r ON r.id = a.requisition_id
               WHERE a.id = %s""",
            [sess["application_id"]],
        )
        match       = float((app_row or {}).get("match_score") or 0)
        resume_w    = _weight_or_default((app_row or {}).get("resume_weight"), 0.40)
        interview_w = _weight_or_default((app_row or {}).get("interview_weight"), 0.60)
        total_w = resume_w + interview_w
        if total_w > 0:
            resume_w /= total_w
            interview_w /= total_w
        combined = round(resume_w * match + interview_w * raw_score, 1)
        # Campus fallback: if candidate skipped resume upload, combined_score = bot_score only
        _campus_no_resume_conv = query_one(
            "SELECT id FROM campus_candidate WHERE application_id=%s AND resume_uploaded=FALSE LIMIT 1",
            [sess["application_id"]],
        )
        if _campus_no_resume_conv:
            combined = raw_score
        # Scores are recorded but the stage is NOT auto-advanced — a recruiter
        # must review the result (score + proctoring) and manually move the
        # candidate to Shortlisted, same as every other pipeline transition.
        query(
            "UPDATE application SET bot_score = %s, combined_score = %s WHERE id = %s",
            [raw_score, combined, sess["application_id"]],
            fetch=False,
        )

        # A completed interview with no proctoring_session row at all is
        # indistinguishable today from "proctoring wasn't enabled for this
        # round" -- the candidate's browser may have silently failed to
        # init proctoring (ad-blocker, denied permission, script error) and
        # nobody would ever know. Log it explicitly so it's discoverable on
        # the Activity Timeline rather than invisible.
        _proc_row = query_one(
            "SELECT id, consent_granted, flag_count FROM proctoring_session WHERE application_id = %s",
            [sess["application_id"]],
        )
        if not _proc_row:
            log_activity(
                "enteri_ai_session", "enteri_ai_completed_no_proctoring",
                entity_id=str(sess["id"]), application_id=sess["application_id"],
                actor_id=None, actor_role="system",
                detail={"reason": "no proctoring_session row exists for this application"},
            )

        background_tasks.add_task(_fire_completion_email, str(sess["id"]))
    else:
        query(
            "UPDATE enteri_ai_session SET conversation = %s::jsonb WHERE id = %s",
            [json.dumps(turns), sess["id"]],
            fetch=False,
        )

    return {"reply": reply, "is_complete": is_complete, "audio_b64": await _synthesize_reply_audio(reply)}


@router.post("/invite/terminate")
async def terminate_invite_session(body: TerminateSessionIn, background_tasks: BackgroundTasks):
    """
    Public — called by the candidate's browser when 3 proctoring strikes are reached.

    Scores the partial transcript (LLM, with rule-based fallback) and writes the
    session as 'terminated_proctoring' so it cannot be resumed.  The recruiter
    dashboard will display the partial score alongside a termination indicator.
    """
    if _enteri_ai_mode() != "conversational":
        raise HTTPException(400, "Conversational mode is not enabled")

    invite = query_one(
        "SELECT id, application_id, expires_at FROM enteri_ai_invite WHERE token = %s",
        [body.token],
    )
    if not invite:
        raise HTTPException(400, "Invalid invite token")

    sess = query_one(
        """SELECT ns.id, ns.status, ns.conversation, ns.application_id,
                  r.title, r.key_skills, r.job_description, r.is_fresher_role
           FROM enteri_ai_session ns
           JOIN application  a ON a.id = ns.application_id
           JOIN requisition  r ON r.id = a.requisition_id
           WHERE ns.application_id = %s""",
        [invite["application_id"]],
    )
    if not sess:
        raise HTTPException(404, "Session not found")
    if sess["status"] in ("completed", "terminated_proctoring"):
        return {"ok": True, "already_closed": True}

    # ── Phase 3, Part E — server-side judge (gated) ───────────────────────
    # When off (default), fall straight through to the pre-existing behaviour
    # below unchanged: the browser's self-reported strike_count is trusted.
    # When on (dev/test only), re-compute via proctoring_scorer against the
    # server's own ledger and only let the termination below actually happen
    # if the server's evidence supports it.
    judge_outcome = None
    if SERVER_SIDE_PROCTORING_JUDGE:
        proc_sess = query_one(
            """SELECT id FROM proctoring_session WHERE application_id = %s
               ORDER BY created_at DESC LIMIT 1""",
            [sess["application_id"]],
        )
        if proc_sess:
            judge = _proc_scorer.judge_termination(proc_sess["id"])
            judge_outcome = judge["outcome"]
            if not judge["should_terminate"]:
                query(
                    """INSERT INTO proctoring_termination_discrepancy
                           (session_id, enteri_ai_session_id, browser_strike_count,
                            browser_reason, server_outcome, server_detail)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
                    [proc_sess["id"], sess["id"], body.strike_count, body.reason,
                     judge_outcome, json.dumps(judge["detail"], default=str)],
                    fetch=False,
                )
                # Phase 4, Part B — also surface this in the unified integrity-
                # flag inbox (kept alongside, not instead of, the row above —
                # see proctoring_scorer.record_integrity_flag's docstring and
                # the Migration 89 comment in main.py for why both exist).
                # dedupe_key='discrepancy' -- one open discrepancy per session
                # is enough; re-hitting this same unsupported state (e.g. the
                # browser retrying) doesn't pile up duplicate flags for a
                # reviewer to wade through.
                _proc_scorer.record_integrity_flag(
                    proc_sess["id"], "termination_discrepancy",
                    {
                        "browser_strike_count": body.strike_count,
                        "browser_reason": body.reason,
                        "server_outcome": judge_outcome,
                    },
                    enteri_ai_session_id=sess["id"],
                    dedupe_key="discrepancy",
                )
                # Also record any monitoring gaps found in the same pass —
                # a gap is often the actual explanation for why the ledger
                # doesn't support what the browser claimed.
                _proc_scorer.record_monitoring_gaps(proc_sess["id"], enteri_ai_session_id=sess["id"])
                # Phase 4, Part C — send the digest (no-op if nothing new).
                try:
                    from ..services import proctoring_alerts as _alerts
                    _alerts.send_integrity_digest_for_session(proc_sess["id"])
                except Exception as _digest_exc:
                    print(f"[enteri_ai] integrity digest failed for {proc_sess['id']}: {_digest_exc}")
                return {
                    "ok": True,
                    "terminated": False,
                    "outcome": judge_outcome,
                    "discrepancy_recorded": True,
                }
            # Falls through to the existing termination logic below, which
            # now also records the judge's outcome for audit (see detail[...]).

    turns = list(sess["conversation"] or [])

    # Score whatever partial transcript we have (best effort)
    role_ctx = {
        "title":           sess["title"],
        "key_skills":      sess["key_skills"] or [],
        "job_description": sess["job_description"] or "",
        "is_fresher_role": bool(sess.get("is_fresher_role")),
    }
    score_result = await _llm_svc.score_transcript({"role_context": role_ctx, "turns": turns})
    raw_score = score_result["raw_score"]
    detail    = score_result["score_detail"]
    detail["terminated_by_proctoring"] = True
    detail["strike_count"] = body.strike_count
    if judge_outcome is not None:
        detail["server_judge_outcome"] = judge_outcome  # Phase 3, Part E audit trail (only set when SERVER_SIDE_PROCTORING_JUDGE is on)

    reason_text = body.reason or f"Auto-terminated after {body.strike_count} proctoring strikes"

    query(
        """UPDATE enteri_ai_session
               SET status = 'terminated_proctoring',
                   raw_score = %s, score_detail = %s::jsonb,
                   termination_reason = %s,
                   completed_at = now()
             WHERE id = %s""",
        [raw_score, json.dumps(detail), reason_text, sess["id"]],
        fetch=False,
    )
    _mark_invite_attempt_completed(sess["application_id"])

    # Update application score (partial) — status stays at its current value
    # rather than 'screen_passed'; recruiters can filter by session status.
    app_row = query_one("SELECT match_score FROM application WHERE id = %s", [sess["application_id"]])
    match    = float((app_row or {}).get("match_score") or 0)
    combined = round(0.4 * match + 0.6 * raw_score, 1)
    query(
        "UPDATE application SET bot_score = %s, combined_score = %s WHERE id = %s",
        [raw_score, combined, sess["application_id"]],
        fetch=False,
    )

    return {"ok": True, "raw_score": raw_score}


# ── Proctoring Appeal Endpoints ───────────────────────────────────────────────

@router.post("/invite/appeal", status_code=201)
def create_appeal(token: str, body: AppealIn):
    """
    Public (token auth) — candidate submits an appeal for a proctoring-terminated session.

    Phase 7, Fix 1 — one appeal per ATTEMPT (enteri_ai_invite_id), not per session
    forever (enteri_ai_session_id): relink_appeal reuses the same enteri_ai_session
    row across every retake, so scoping uniqueness to the session used to
    permanently block a second appeal after a legitimate relink + re-
    termination. invite["id"] here is the CURRENT invite the candidate is
    actually submitting against — a fresh id every time relink_appeal issues
    a new one via _do_single_invite.
    """
    invite = query_one(
        "SELECT id, application_id FROM enteri_ai_invite WHERE token = %s",
        [token],
    )
    if not invite:
        raise HTTPException(400, "Invalid invite token")

    sess = query_one(
        "SELECT id, status FROM enteri_ai_session WHERE application_id = %s",
        [invite["application_id"]],
    )
    if not sess:
        raise HTTPException(404, "No session found for this invite")
    if sess["status"] != "terminated_proctoring":
        raise HTTPException(400, "Appeals are only available for proctoring-terminated sessions")

    existing = query_one(
        "SELECT id FROM proctoring_appeal WHERE enteri_ai_invite_id = %s",
        [invite["id"]],
    )
    if existing:
        raise HTTPException(409, "An appeal has already been submitted for this attempt")

    try:
        query(
            """INSERT INTO proctoring_appeal (application_id, enteri_ai_session_id, enteri_ai_invite_id, candidate_explanation)
               VALUES (%s, %s, %s, %s)""",
            [invite["application_id"], sess["id"], invite["id"], body.explanation.strip()],
            fetch=False,
        )
    except Exception as exc:
        # Defense-in-depth against the SELECT-then-INSERT race the check
        # above has (UNIQUE(enteri_ai_invite_id) is the real guarantee) — a
        # concurrent duplicate submission should still read as 409, not 500.
        if "enteri_ai_invite_id" in str(exc) and "unique" in str(exc).lower():
            raise HTTPException(409, "An appeal has already been submitted for this attempt")
        raise
    return {"ok": True}


@router.get("/appeals")
def list_appeals(
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """JWT — list all proctoring appeals (recruiter/TA manager/admin)."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    rows = query(
        """SELECT pa.id,
                  pa.application_id,
                  pa.enteri_ai_session_id,
                  pa.candidate_explanation,
                  pa.status,
                  pa.recruiter_notes,
                  pa.created_at,
                  pa.reviewed_at,
                  c.full_name   AS candidate_name,
                  c.email       AS candidate_email,
                  r.title       AS requisition,
                  ns.termination_reason,
                  ns.raw_score,
                  ns.score_detail,
                  ps.id         AS proctoring_session_id,
                  rev.email     AS reviewed_by_email
           FROM proctoring_appeal pa
           JOIN application   a   ON a.id  = pa.application_id
           JOIN candidate     c   ON c.id  = a.candidate_id
           JOIN requisition   r   ON r.id  = a.requisition_id
           JOIN enteri_ai_session  ns  ON ns.id = pa.enteri_ai_session_id
           LEFT JOIN proctoring_session ps ON ps.application_id = pa.application_id
           LEFT JOIN app_user  rev ON rev.id = pa.reviewed_by
           WHERE r.tenant_id = %s
           ORDER BY pa.created_at DESC
           LIMIT %s OFFSET %s""",
        [user.get("tenant_id"), limit, offset],
    )
    return rows or []


@router.patch("/appeals/{appeal_id}")
def update_appeal(appeal_id: str, body: AppealUpdateIn, user: dict = Depends(get_current_user)):
    """JWT — recruiter updates an appeal's status and/or notes."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    appeal = query_one(
        """SELECT pa.id FROM proctoring_appeal pa
           JOIN application a ON a.id = pa.application_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE pa.id = %s AND r.tenant_id = %s""",
        [appeal_id, user.get("tenant_id")],
    )
    if not appeal:
        raise HTTPException(404, "Appeal not found")

    valid_statuses = {"pending", "reviewed", "relink_sent", "rejected"}
    if body.status and body.status not in valid_statuses:
        raise HTTPException(400, f"Invalid status — must be one of: {', '.join(sorted(valid_statuses))}")

    sets, vals = [], []
    if body.status is not None:
        sets += ["status = %s", "reviewed_by = %s", "reviewed_at = now()"]
        vals += [body.status, user["sub"]]
    if body.recruiter_notes is not None:
        sets.append("recruiter_notes = %s")
        vals.append(body.recruiter_notes)

    if not sets:
        raise HTTPException(400, "No fields to update")

    vals.append(appeal_id)
    query(f"UPDATE proctoring_appeal SET {', '.join(sets)} WHERE id = %s", vals, fetch=False)
    return {"ok": True}


@router.post("/appeals/{appeal_id}/relink", status_code=201)
def relink_appeal(appeal_id: str, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    """JWT — give the candidate a fresh interview link after appeal review.

    Resets the terminated session back to pending, expires the old invite token,
    and calls the existing create_enteri_ai_invite() which issues a new token and sends
    the standard invite email — no new email infrastructure required.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    appeal = query_one(
        "SELECT id, application_id, enteri_ai_session_id, status FROM proctoring_appeal WHERE id = %s",
        [appeal_id],
    )
    if not appeal:
        raise HTTPException(404, "Appeal not found")
    if appeal["status"] == "relink_sent":
        raise HTTPException(400, "A fresh interview link has already been sent for this appeal")

    # Capture the termination reason for the TA/admin notification below,
    # BEFORE the reset wipes it off enteri_ai_session.
    _pre_reset = query_one(
        "SELECT termination_reason FROM enteri_ai_session WHERE id = %s",
        [appeal["enteri_ai_session_id"]],
    )
    _prior_termination_reason = (_pre_reset or {}).get("termination_reason")

    # Reset the session so validate + begin endpoints will accept it again
    query(
        """UPDATE enteri_ai_session
               SET status = 'pending',
                   conversation = NULL, transcript = NULL,
                   raw_score = NULL, score_detail = NULL,
                   termination_reason = NULL, completed_at = NULL
             WHERE id = %s""",
        [appeal["enteri_ai_session_id"]],
        fetch=False,
    )

    # Revoke prior invite tokens for this application (mirrors resend_enteri_ai_invite)
    query(
        """UPDATE enteri_ai_invite
           SET attempt_status = 'revoked',
               expires_at = LEAST(expires_at, now() - interval '1 second')
           WHERE application_id = %s AND attempt_status != 'revoked'""",
        [appeal["application_id"]],
        fetch=False,
    )

    # Issue fresh invite (creates token + sends standard email) via existing logic.
    # Calls _do_single_invite directly (not create_enteri_ai_invite) since appeals are
    # reviewable org-wide regardless of requisition ownership (see list_appeals).
    result = _do_single_invite(appeal["application_id"], user, background_tasks)

    # Mark appeal resolved only if the relink email actually went out — a failed
    # send must leave the appeal open (not relink_sent) so it stays in the
    # recruiter's queue and can be retried through this same endpoint. The
    # session reset + token revocation above still happen either way: on
    # failure the candidate has no working link, which matches the appeal
    # correctly staying open for a retry.
    if result.get("email_sent"):
        query(
            """UPDATE proctoring_appeal
                   SET status = 'relink_sent', reviewed_by = %s, reviewed_at = now()
                 WHERE id = %s""",
            [user["sub"], appeal_id],
            fetch=False,
        )
        # Notify TA manager/admin that a relink happened — informational only,
        # doesn't affect the relink itself, and never blocks the response
        # (reuses the same email machinery + guard as the integrity digest).
        try:
            from ..services import proctoring_alerts as _alerts
            _alerts.send_relink_notification(
                candidate_name=result.get("candidate_name"),
                job_title=result.get("job_title"),
                actor=user,
                termination_reason=_prior_termination_reason,
            )
        except Exception as _notify_exc:
            print(f"[enteri_ai] relink notification failed for appeal {appeal_id}: {_notify_exc}")

    return {**result, "ok": bool(result.get("email_sent"))}


@router.post("/invite/submit/{session_id}")
async def submit_invited_session(session_id: str, body: SubmitSessionIn, background_tasks: BackgroundTasks):
    """Public — candidate submits completed interview transcript.

    In scripted mode: scores the supplied transcript using the rule-based model.
    In conversational mode: if the converse endpoint already scored the session,
    returns that score immediately; otherwise runs LLM scoring on the stored
    conversation (edge-case safety valve).
    """
    sess = query_one(
        """SELECT id, application_id, questions, conversation,
                  raw_score, score_detail, status
           FROM enteri_ai_session WHERE id = %s""",
        [session_id],
    )
    if not sess:
        raise HTTPException(404, "Session not found")

    # ── Conversational mode ───────────────────────────────────────────────────
    if _enteri_ai_mode() == "conversational":
        # Already fully scored by the converse endpoint
        if sess["status"] == "completed" and sess["raw_score"] is not None:
            return {
                "session_id": session_id,
                "raw_score": float(sess["raw_score"]),
                "score_detail": sess["score_detail"] or {},
            }

        # Safety valve: score the stored conversation if converse didn't complete
        stored_turns = list(sess["conversation"] or [])
        app_meta = query_one(
            """SELECT a.match_score, r.title, r.key_skills, r.job_description, r.is_fresher_role
               FROM application a JOIN requisition r ON r.id = a.requisition_id
               WHERE a.id = %s""",
            [sess["application_id"]],
        )
        role_ctx = {
            "title": (app_meta or {}).get("title", ""),
            "key_skills": (app_meta or {}).get("key_skills") or [],
            "job_description": (app_meta or {}).get("job_description") or "",
            "is_fresher_role": bool((app_meta or {}).get("is_fresher_role")),
        }
        score_result = await _llm_svc.score_transcript(
            {"role_context": role_ctx, "turns": stored_turns}
        )
        raw_score = score_result["raw_score"]
        detail    = score_result["score_detail"]

        query(
            """UPDATE enteri_ai_session
               SET raw_score = %s, score_detail = %s::jsonb,
                   status = 'completed', completed_at = now()
               WHERE id = %s""",
            [raw_score, json.dumps(detail), session_id],
            fetch=False,
        )
        _mark_invite_attempt_completed(sess["application_id"])
        match    = float((app_meta or {}).get("match_score") or 0)
        combined = round(0.4 * match + 0.6 * raw_score, 1)
        # Scores are recorded but the stage is NOT auto-advanced — see comment
        # in converse_invite() above.
        query(
            "UPDATE application SET bot_score = %s, combined_score = %s WHERE id = %s",
            [raw_score, combined, sess["application_id"]], fetch=False,
        )
        background_tasks.add_task(_fire_completion_email, session_id)
        log_activity(
            "enteri_ai_session", "enteri_ai_session_completed",
            entity_id=session_id, application_id=sess["application_id"],
            requisition_id=_application_req_id(sess["application_id"]),
            actor_id=None, actor_role="candidate",
            detail={"raw_score": raw_score, "mode": "conversational"},
        )
        return {"session_id": session_id, "raw_score": raw_score, "score_detail": detail}

    # ── Scripted mode (unchanged behaviour) ──────────────────────────────────
    questions  = sess["questions"] if isinstance(sess["questions"], list) else []
    transcript = [t.dict() for t in body.transcript]
    raw_score, detail = _score_transcript(questions, transcript)

    query(
        """UPDATE enteri_ai_session
           SET transcript = %s::jsonb, raw_score = %s, score_detail = %s::jsonb,
               status = 'completed', completed_at = now()
           WHERE id = %s""",
        [json.dumps(transcript), raw_score, json.dumps(detail), session_id],
        fetch=False,
    )
    _mark_invite_attempt_completed(sess["application_id"])

    app_row = query_one(
        "SELECT match_score FROM application WHERE id = %s",
        [sess["application_id"]],
    )
    match    = float(app_row["match_score"] or 0) if app_row else 0
    combined = round(0.4 * match + 0.6 * raw_score, 1)
    # Scores are recorded but the stage is NOT auto-advanced — see comment
    # in converse_invite() above.
    query(
        "UPDATE application SET bot_score = %s, combined_score = %s WHERE id = %s",
        [raw_score, combined, sess["application_id"]], fetch=False,
    )
    background_tasks.add_task(_fire_completion_email, session_id)
    log_activity(
        "enteri_ai_session", "enteri_ai_session_completed",
        entity_id=session_id, application_id=sess["application_id"],
        requisition_id=_application_req_id(sess["application_id"]),
        actor_id=None, actor_role="candidate",
        detail={"raw_score": raw_score, "mode": "scripted"},
    )

    return {"session_id": session_id, "raw_score": raw_score, "score_detail": detail}


@router.post("/invite/transcribe")
async def transcribe_candidate_audio(file: UploadFile = File(...)):
    """
    Public — transcribe one candidate audio blob via Whisper.
    Returns {"text": "..."}. The frontend sends this text to /invite/converse.
    """
    from ..services.stt import transcribe_audio
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "Empty audio")
    try:
        text = transcribe_audio(audio, file.filename or "audio.webm")
    except Exception as exc:
        print(f"[stt] transcription failed: {exc}")
        raise HTTPException(502, "Transcription failed")
    return {"text": text}


@router.get("/health")
def enteri_ai_health(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    totals = query_one(
        """SELECT
             COUNT(*) AS total,
             COUNT(*) FILTER (WHERE status = 'completed') AS completed,
             COUNT(*) FILTER (WHERE status = 'failed')    AS failed,
             COUNT(*) FILTER (WHERE status = 'in_progress') AS in_progress,
             ROUND(AVG(raw_score) FILTER (WHERE status = 'completed')::numeric, 1) AS avg_score,
             COUNT(*) FILTER (WHERE created_at::date = CURRENT_DATE) AS today
           FROM enteri_ai_session""",
        [],
    )
    recent = query(
        """SELECT id, application_id, status, raw_score, completed_at, started_at
           FROM enteri_ai_session
           ORDER BY created_at DESC LIMIT 20""",
        [],
    )
    return {
        "bot_name": "Enteri AI",
        "version": "v1.0 — voice-first (14a)",
        "model": "Rule-based Q&A + keyword scoring",
        "status": "active",
        "avatar": _avatar_svc.get_config(),
        "total_sessions":     int(totals["total"])       if totals else 0,
        "completed_sessions": int(totals["completed"])   if totals else 0,
        "failed_sessions":    int(totals["failed"])      if totals else 0,
        "in_progress":        int(totals["in_progress"]) if totals else 0,
        "avg_score":          float(totals["avg_score"]) if totals and totals["avg_score"] else None,
        "sessions_today":     int(totals["today"])       if totals else 0,
        "recent_sessions":    recent,
    }


# ── A2: Avatar config endpoint ────────────────────────────────────────────────

@router.get("/avatar/config")
def avatar_config(_user: dict = Depends(get_current_user)):
    """Return current avatar provider config (A2 — swappable interface)."""
    return _avatar_svc.get_config()


# ── A3: Render question as speaking clip (GPU providers) ─────────────────────

class RenderQuestionIn(BaseModel):
    question_text: str
    face_id: str = "enteri-ai-female"
    session_id: Optional[str] = None


@router.post("/render-question")
async def render_question(body: RenderQuestionIn, _user: dict = Depends(get_current_user)):
    """
    STEP A3 — Generate TTS audio for a question and render a lip-sync video
    using the configured avatar provider (sadtalker / wav2lip / vendor).

    For 'orb' provider: returns {video_url: null} immediately (frontend uses orb).
    For GPU providers: generates audio via edge-tts (neural, falls back to gTTS),
    sends to GPU service, returns video_url.
    Falls back to orb cleanly if TTS or GPU service fails.
    """
    provider = _avatar_svc.PROVIDER
    if provider == "orb":
        return {"video_url": None, "provider": "orb", "fallback": False}

    # Generate TTS audio file for GPU rendering
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            audio_path = tf.name
        await _tts_svc.synthesize_speech(body.question_text, audio_path)
    except Exception as exc:
        return {"video_url": None, "provider": "orb", "fallback": True, "reason": str(exc)}

    try:
        result = _avatar_svc.render_speaking_clip(body.face_id, audio_path)
    finally:
        try:
            os.unlink(audio_path)
        except Exception:
            pass
    return result
