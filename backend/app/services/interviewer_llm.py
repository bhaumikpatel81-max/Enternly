"""
LLM-driven interviewer brain (NexAI conversational mode).

Uses the openai Python client pointed at Groq's OpenAI-compatible API.
Two public async functions:
  next_turn(conversation_state)        -> {"reply": str, "is_complete": bool}
  score_transcript(conversation_state) -> {"raw_score": int, "score_detail": dict}

conversation_state schema:
  {
    "role_context": {"title": str, "key_skills": list, "job_description": str},
    "turns": [{"speaker": "bot"|"candidate", "text": str}, ...]
  }

Required env vars (backend .env only — never logged, never returned):
  GROQ_API_KEY   — your Groq API key (starts with gsk_)
  GROQ_BASE_URL  — defaults to https://api.groq.com/openai/v1
  LLM_MODEL      — defaults to llama-3.3-70b-versatile
"""
import json
import os
from typing import Optional

import openai

# ── Config (read at call time so env changes in tests are picked up) ──────────

def _model() -> str:
    return os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")

# Hard cap: force is_complete=True after this many bot turns regardless of sentinel.
# Includes the hardcoded intro turn. Set high enough to allow the two-step graceful
# ending (wrap-up question + closing) without hitting the cap prematurely.
# Target interview length: 25–30 min (~22–28 substantive exchanges + 2-step close).
_MAX_BOT_TURNS = 30

# ── Prompts ───────────────────────────────────────────────────────────────────

_INTERVIEW_SYSTEM = """\
You are NexAI, a professional and warm AI screening interviewer.
Your job is to conduct a natural, spoken phone-screen for the role described below.

ROLE CONTEXT:
Title: {title}
Key Skills: {key_skills}
Job Description: {job_description}

{difficulty_guidance}

RULES:
- The candidate has already received an introduction and confirmed they are ready. \
Do NOT introduce yourself or ask if the candidate is ready — begin directly with your first question.
- Ask exactly ONE question per turn. Never stack multiple questions in one reply.
- Listen carefully to what the candidate just said and open with a brief, varied \
acknowledgement before your next question -- as a human interviewer would.
- Cover ALL of the key skills thoroughly across the conversation. For each key skill, \
ask 2–3 follow-up questions that probe real-world application, depth, and edge cases \
before moving to the next skill. Do NOT skip any key skill and do NOT move on after \
a single surface-level answer.
- Calibrate every question to the DIFFICULTY level above. Adjust smoothly as it changes \
between turns -- never comment on the difficulty level or tell the candidate it changed.
- Keep every reply short and spoken-friendly: no bullet points, no numbered lists, \
no markdown formatting -- this text will be read aloud by text-to-speech.
- Vary your acknowledgements. Do not open every turn with "Great!" or "Excellent!".
- Aim for a thorough, substantive conversation of ~22–28 exchanges. Do NOT wrap up \
early — if you have not yet deeply explored all key skills with follow-ups, keep asking.
- GRACEFUL ENDING (two steps — follow this exactly):
  Step 1: After 22 to 28 substantive exchanges (not counting the opening introduction), \
when you have thoroughly covered all key skills with multiple follow-ups each, ask a \
closing confirmation that invites both a final addition AND questions from the candidate: \
"That covers what I wanted to ask — before we wrap up, is there anything you'd like to add, \
or any questions you have for me?" \
Do NOT include [INTERVIEW_COMPLETE] on this turn.
  Step 2: On the very next turn, after the candidate has answered the closing confirmation \
(answer their question briefly and honestly if they asked one, staying in character as the \
interviewer — otherwise just acknowledge), give a brief warm closing (thank the candidate, \
mention the team will be in touch soon, wish them well) and append the exact token \
[INTERVIEW_COMPLETE] at the very end of your message (no space before it, nothing after it).
- Never reveal you are an AI or mention that the interview is being scored.\
"""

_SCORE_SYSTEM = """\
You are an expert technical recruiter evaluating a screening interview transcript.
Return STRICT JSON only -- no prose before or after, no markdown fences.

ROLE: {title}
KEY SKILLS: {key_skills}
JOB DESCRIPTION: {job_description}

This is a SPOKEN transcript captured via speech-to-text -- it may contain transcription
errors, filler words, or paraphrasing. Do NOT penalise the candidate for these. A turn
marked "(possibly cut off by silence timeout)" may have ended mid-thought because the
system stopped listening too early, not because the candidate ran out to say -- evaluate
what WAS said on its own merits rather than marking it wrong or incomplete for that reason.

{grading_guidance}

Return exactly this JSON structure (nothing else):
{{"raw_score": <integer 0-100>, "strengths": "<one concise paragraph>", \
"concerns": "<one concise paragraph>", "per_dimension": {{"relevance": <integer 0-10>, \
"depth": <integer 0-10>, "communication": <integer 0-10>, "fit": <integer 0-10>}}}}

Scoring dimensions:
  relevance     -- how closely the answers relate to the role and key skills
  depth         -- specificity and technical depth of the candidate's knowledge
  communication -- clarity, fluency, and conciseness in spoken answers
  fit           -- overall impression of culture and role fit

raw_score must be the per_dimension average scaled to 100, rounded to the nearest integer.\
"""

_GRADING_EXPERIENCED = """\
SCORING STYLE -- this is an EXPERIENCED-HIRE role. Score on CONCEPTUAL UNDERSTANDING, \
not exact definitions or textbook phrasing:
- Did the candidate convey the core idea correctly, even in their own words?
- Reward correct mental models, relevant real-world examples, and practical reasoning \
over reciting a formal definition.
- Do NOT require canonical definitions or specific keywords -- a candidate who explains \
an API as "a way for two programs to talk to each other" demonstrates understanding equal \
to one who recites "Application Programming Interface".\
"""

_GRADING_FRESHER = """\
SCORING STYLE -- this is a FRESHER / EARLY-CAREER role (0-1 years experience). Reward \
clear conceptual understanding in the candidate's own words the same as you would an \
experienced hire -- but for freshers it is also fair and expected for them to state \
textbook-style or standard definitions, since they may not yet have real-world examples \
to draw on. Do NOT penalise a fresher for giving a correct textbook definition instead of \
a practical anecdote; give full credit for accurate fundamentals either way.\
"""

# ── Adaptive difficulty ─────────────────────────────────────────────────────
# Every interview starts at LOW (safe default for freshers / unknown seniority)
# and is recomputed from scratch each turn from the transcript so far -- no new
# DB column needed. It escalates one level after _ESCALATE_STREAK consecutive
# strong answers and steps back down after the same number of consecutive weak
# ones, so a single lucky/unlucky answer can't swing the level.

_DIFFICULTY_LEVELS = ["low", "medium", "high"]
_ESCALATE_STREAK = 2

_DIFFICULTY_GUIDANCE = {
    "low": (
        "DIFFICULTY: LOW. Ask foundational, straightforward questions. Focus on core "
        "concepts, definitions, and simple real-world examples. Assume the candidate "
        "may be a fresher or early in their career -- keep questions approachable, "
        "one concept at a time, and do not stack conditions or edge cases onto them."
    ),
    "medium": (
        "DIFFICULTY: MEDIUM. Ask moderately challenging questions that require real "
        "hands-on experience, not textbook definitions. Ask the candidate to walk "
        "through how they actually solved a specific problem, including the trade-offs "
        "they weighed."
    ),
    "high": (
        "DIFFICULTY: HIGH. Ask advanced, in-depth questions. Probe edge cases, failure "
        "modes, scale, and design trade-offs. Push the candidate to justify their "
        "reasoning and to consider alternative approaches or where their solution "
        "would break down."
    ),
}


def _rate_answer(text: str, key_skills: list) -> str:
    """Cheap heuristic rating of one candidate answer: 'strong' / 'ok' / 'weak'."""
    words = text.split()
    n = len(words)
    if n < 8:
        return "weak"
    lower = text.lower()
    skill_hits = sum(1 for s in (key_skills or []) if s and s.lower() in lower)
    if n >= 40 and skill_hits > 0:
        return "strong"
    if n >= 20:
        return "strong" if skill_hits > 0 else "ok"
    return "ok"


def _current_difficulty(turns: list, key_skills: list) -> str:
    level = 0
    streak = 0
    last_dir = None
    for t in turns:
        if t.get("speaker") != "candidate":
            continue
        direction = {"strong": 1, "weak": -1, "ok": 0}[_rate_answer(t.get("text", ""), key_skills)]
        if direction == 0:
            streak, last_dir = 0, None
            continue
        streak = streak + 1 if direction == last_dir else 1
        last_dir = direction
        if streak >= _ESCALATE_STREAK:
            level = max(0, min(2, level + direction))
            streak = 0
    return _DIFFICULTY_LEVELS[level]


# ── Lazy Groq client (openai SDK, Groq base URL) ──────────────────────────────

_client: Optional[openai.AsyncOpenAI] = None


def _get_client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to .env.prod."
            )
        base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get(
            "GROQ_BASE_URL", "https://api.openai.com/v1"
        )
        _client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    return _client


# ── Helpers ───────────────────────────────────────────────────────────────────

_SENTINEL = "[INTERVIEW_COMPLETE]"


def _build_messages(system_prompt: str, turns: list) -> list:
    """
    Build the OpenAI-format messages list from stored turns.
    A synthetic 'please begin' user message is always prepended so the model's
    first reply is the bot's opening question and role alternation stays valid.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "The candidate has confirmed they are ready. Please begin with your first interview question."},
    ]
    for turn in turns:
        role = "assistant" if turn["speaker"] == "bot" else "user"
        messages.append({"role": role, "content": turn["text"]})
    return messages


def _count_bot_turns(turns: list) -> int:
    return sum(1 for t in turns if t.get("speaker") == "bot")


# ── Public API ────────────────────────────────────────────────────────────────

async def next_turn(conversation_state: dict) -> dict:
    """
    Generate the bot's next spoken reply.

    Returns {"reply": str, "is_complete": bool}.
    is_complete becomes True when the model appends [INTERVIEW_COMPLETE] to its
    reply, or when the hard cap of 10 bot turns is reached.
    """
    role_ctx = conversation_state["role_context"]
    turns = conversation_state.get("turns", [])

    # Hard cap: if we've already reached the max, close regardless
    if _count_bot_turns(turns) >= _MAX_BOT_TURNS:
        return {
            "reply": (
                "Thank you so much for your time today. "
                "The team will carefully review your responses and be in touch soon. "
                "Have a wonderful day!"
            ),
            "is_complete": True,
        }

    difficulty = _current_difficulty(turns, role_ctx.get("key_skills") or [])
    system_prompt = _INTERVIEW_SYSTEM.format(
        title=role_ctx.get("title", "this role"),
        key_skills=", ".join(role_ctx.get("key_skills") or []) or "general professional skills",
        job_description=(role_ctx.get("job_description") or "")[:800],
        difficulty_guidance=_DIFFICULTY_GUIDANCE[difficulty],
    )

    messages = _build_messages(system_prompt, turns)

    response = await _get_client().chat.completions.create(
        model=_model(),
        max_tokens=400,
        messages=messages,
    )

    reply = (response.choices[0].message.content or "").strip()

    # Detect sentinel and strip it from the spoken reply
    if _SENTINEL in reply:
        reply = reply.replace(_SENTINEL, "").strip()
        return {"reply": reply, "is_complete": True}

    return {"reply": reply, "is_complete": False}


async def score_transcript(conversation_state: dict) -> dict:
    """
    Score the full interview conversation using the LLM.

    Returns {"raw_score": int 0-100, "score_detail": dict}.
    Falls back to rule-based scoring on any API or parse failure so a score
    is always produced.
    """
    role_ctx = conversation_state["role_context"]
    turns = conversation_state.get("turns", [])

    transcript_text = "\n".join(
        f"{t['speaker'].upper()}"
        f"{' (possibly cut off by silence timeout)' if t.get('truncated') else ''}"
        f": {t['text']}"
        for t in turns
    )
    if not transcript_text.strip():
        return _rule_based_fallback(turns)

    try:
        grading_guidance = _GRADING_FRESHER if role_ctx.get("is_fresher_role") else _GRADING_EXPERIENCED
        system_prompt = _SCORE_SYSTEM.format(
            title=role_ctx.get("title", "this role"),
            key_skills=", ".join(role_ctx.get("key_skills") or []) or "general professional skills",
            job_description=(role_ctx.get("job_description") or "")[:800],
            grading_guidance=grading_guidance,
        )

        response = await _get_client().chat.completions.create(
            model=_model(),
            max_tokens=600,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"INTERVIEW TRANSCRIPT:\n\n{transcript_text}"
                        "\n\nEvaluate this candidate now. Return JSON only."
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )

        raw_text = (response.choices[0].message.content or "").strip()

        # Defensive fence stripping (belt-and-suspenders)
        if raw_text.startswith("```"):
            parts = raw_text.split("```")
            raw_text = parts[1].lstrip("json").strip() if len(parts) > 1 else raw_text

        parsed = json.loads(raw_text)
        raw_score = max(0, min(100, int(round(float(parsed["raw_score"])))))
        per_dim = parsed.get("per_dimension", {})
        score_detail = {
            "strengths": parsed.get("strengths", ""),
            "concerns": parsed.get("concerns", ""),
            "per_dimension": {
                "relevance":     int(per_dim.get("relevance", 0)),
                "depth":         int(per_dim.get("depth", 0)),
                "communication": int(per_dim.get("communication", 0)),
                "fit":           int(per_dim.get("fit", 0)),
            },
            "scored_by": "llm",
        }
        return {"raw_score": raw_score, "score_detail": score_detail}

    except Exception as exc:
        print(f"[interviewer_llm] LLM scoring failed, falling back to rule-based: {exc}")
        return _rule_based_fallback(turns)


# ── Rule-based fallback ───────────────────────────────────────────────────────

def _rule_based_fallback(turns: list) -> dict:
    """Simple word-count heuristic used when LLM scoring is unavailable."""
    candidate_turns = [t for t in turns if t.get("speaker") == "candidate"]
    if not candidate_turns:
        return {
            "raw_score": 0,
            "score_detail": {
                "scored_by": "rule_based_fallback",
                "reason": "no_candidate_answers",
            },
        }

    total_words = sum(len(t["text"].split()) for t in candidate_turns)
    avg_words = total_words / len(candidate_turns)
    depth = min(avg_words / 60.0, 1.0)
    communication = 1.0 if avg_words >= 15 else (avg_words / 15.0)
    raw_score = min(round((depth * 0.6 + communication * 0.4) * 70, 1), 70.0)

    return {
        "raw_score": int(round(raw_score)),
        "score_detail": {
            "scored_by": "rule_based_fallback",
            "turns_answered": len(candidate_turns),
            "avg_words_per_turn": round(avg_words, 1),
        },
    }
