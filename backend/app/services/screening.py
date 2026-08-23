"""
Screening engine.

Scores a candidate against a requisition using four dimensions:

  Keyword match   — rule-based skill/JD overlap (alias-aware)
  Experience      — years vs. minimum required
  AI holistic fit — Groq LLM reads resume + JD and returns 0-100
  Stability       — average role tenure (experienced candidates only)

FRESHERS get a different model:
  Education fit   — degree/branch alignment (25%)
  Project relevance — JD keywords in Projects section (25%)
  Keyword match   — same alias-aware match (20%)
  AI fresher fit  — fresher-tuned Groq prompt (30%)

WEIGHTS (editable constants below):
  Experienced candidates: keyword 0.30 + experience 0.20 + AI 0.40 + stability 0.10
  Freshers: education 0.25 + project 0.25 + keyword 0.20 + AI 0.30

STABILITY:
  Parsed from resume date ranges. If not parseable: status = 'pending_manual'
  and recruiter can enter it via the manual-tenure endpoint.

AI FALLBACK:
  On any Groq failure the AI component returns score 50 for composite math
  but sets ai_fit_score = None and ai_score_status = "unavailable" in the
  breakdown so the UI can warn recruiters instead of silently using 50.
"""
import json
import os
import re
from datetime import date
from typing import Optional

import openai

# ── Keyword alias map (Improvement 3) ────────────────────────────────────────
# For each JD skill key, list all resume spellings that count as a match.
# Matching is case-insensitive substring. Any alias hit = full match credit.

KEYWORD_ALIASES: dict[str, list[str]] = {
    "javascript":      ["javascript", "js", "node.js", "nodejs"],
    "typescript":      ["typescript", "ts"],
    "python":          ["python", "py"],
    "react":           ["react", "react.js", "reactjs"],
    "angular":         ["angular", "angularjs", "angular.js"],
    "vue":             ["vue", "vue.js", "vuejs"],
    "dotnet":          [".net", "dotnet", "asp.net", "c#", "csharp"],
    "java":            ["java", "spring", "springboot", "spring boot"],
    "sql":             ["sql", "mysql", "postgresql", "postgres", "mssql", "oracle db"],
    "nosql":           ["nosql", "mongodb", "mongo", "redis", "cassandra"],
    "aws":             ["aws", "amazon web services", "ec2", "s3", "lambda"],
    "azure":           ["azure", "microsoft azure"],
    "gcp":             ["gcp", "google cloud", "bigquery"],
    "docker":          ["docker", "containerization", "containers"],
    "kubernetes":      ["kubernetes", "k8s"],
    "machine learning":["machine learning", "ml", "sklearn", "scikit-learn",
                        "tensorflow", "keras", "pytorch"],
    "ai":              ["artificial intelligence", "ai", "llm", "generative ai", "gen ai"],
    "git":             ["git", "github", "gitlab", "version control"],
    "api":             ["api", "rest api", "restful", "graphql", "fastapi", "flask", "django"],
}

# ── Editable scoring constants ────────────────────────────────────────────────

EXPERIENCED_MIN_YEARS  = 4.0
EXPERIENCED_MIN_ROLES  = 2

SCORE_WEIGHT_KEYWORD    = 0.30
SCORE_WEIGHT_EXPERIENCE = 0.20
SCORE_WEIGHT_AI         = 0.40
SCORE_WEIGHT_STABILITY  = 0.10

STABILITY_FULL_MONTHS = 24
STABILITY_ZERO_MONTHS = 3

# ── Education fit constants (Improvement 2) ───────────────────────────────────

_EDU_BRANCHES_HIGH = {
    "computer science", "computer engineering", "information technology",
    "cs", "it", "ai", "ml", "data science", "artificial intelligence",
    "machine learning", "software engineering",
}
_EDU_BRANCHES_MED = {
    "electronics", "ece", "electronics and communication",
    "information systems",
}
_PROJECT_HEADINGS = {
    "project", "projects", "academic project", "academic projects",
    "capstone", "final year project", "personal projects",
}

# ── Groq sync client ──────────────────────────────────────────────────────────

_sync_client: Optional[openai.OpenAI] = None


def _get_client() -> openai.OpenAI:
    global _sync_client
    if _sync_client is None:
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not set — add it to .env.prod")
        base_url = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
        _sync_client = openai.OpenAI(api_key=api_key, base_url=base_url)
    return _sync_client


def _llm_model() -> str:
    return os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")


# ── AI screen prompts ─────────────────────────────────────────────────────────

_AI_SCREEN_SYSTEM = """\
You are an expert recruiter evaluating a candidate resume against a job opening.
Return STRICT JSON only -- no prose before or after, no markdown fences.

Return exactly this JSON structure (nothing else):
{"ai_fit_score":<integer 0-100>,"strengths":"<one concise paragraph>",\
"concerns":"<one concise paragraph>","rationale":"<one concise paragraph>"}

Scoring guide: 0-30 poor fit, 31-50 below average, 51-70 average,\
 71-85 good fit, 86-100 excellent fit.
Base ai_fit_score on: relevance of skills to role requirements, quality and\
 depth of experience, seniority alignment with band, overall career trajectory.\
"""

# Improvement 2 — fresher-specific Groq prompt
_AI_FRESHER_SCREEN_SYSTEM = """\
You are an expert campus recruiter evaluating a fresher candidate (0–1 years experience) \
against a job opening.
Return STRICT JSON only — no prose, no markdown fences.

Return exactly this JSON structure (nothing else):
{"ai_fit_score":<integer 0-100>,"strengths":"<one concise paragraph>",\
"concerns":"<one concise paragraph>","rationale":"<one concise paragraph>",\
"learning_signals":"<one concise paragraph>"}

Scoring guide: 0-30 poor, 31-50 below avg, 51-70 avg, 71-85 good, 86-100 excellent.
Evaluate on: alignment of academic branch with role, quality and relevance of projects, \
certifications or self-learning signals (courses, hackathons, open source, internships), \
communication clarity in resume, and overall potential. \
Do NOT penalize for lack of work experience — this is expected.\
"""

# ── Tenure extraction ─────────────────────────────────────────────────────────

_MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

_MON = (
    r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
)
_YR      = r'(\d{4})'
_SEP     = r'(?:\s*[-–—/|]\s*|\s+to\s+)'
_PRESENT = r'(present|current|now|till\s+date|today|to\s+date)'

_TENURE_RE = re.compile(
    _MON + r'\s+' + _YR + _SEP
    + r'(?:' + _PRESENT + r'|' + _MON + r'\s+' + _YR + r')',
    re.IGNORECASE,
)


def _parse_avg_tenure(resume_text: str) -> tuple[Optional[float], dict]:
    today = date.today()
    durations: list[int] = []

    for m in _TENURE_RE.finditer(resume_text or ""):
        s_mon    = m.group(1)
        s_yr     = int(m.group(2))
        present  = m.group(3)
        e_mon    = m.group(4)
        e_yr_str = m.group(5)

        s_m = _MONTHS.get(s_mon[:3].lower(), 1)

        if present:
            e_m, e_yr = today.month, today.year
        elif e_mon and e_yr_str:
            e_m  = _MONTHS.get(e_mon[:3].lower(), 1)
            e_yr = int(e_yr_str)
        else:
            continue

        months = (e_yr - s_yr) * 12 + (e_m - s_m)
        if 1 <= months <= 480:
            durations.append(months)

    if not durations:
        return None, {"tenure_parse": "no_date_ranges_found"}

    avg = sum(durations) / len(durations)
    return avg, {
        "tenure_parse":   "computed",
        "roles_parsed":   len(durations),
        "tenures_months": durations,
    }


# ── Stability score ───────────────────────────────────────────────────────────

def compute_stability_score(avg_tenure_months: float) -> float:
    if avg_tenure_months >= STABILITY_FULL_MONTHS:
        return 100.0
    if avg_tenure_months <= STABILITY_ZERO_MONTHS:
        return 0.0
    span = max(1.0, STABILITY_FULL_MONTHS - STABILITY_ZERO_MONTHS)
    return 100.0 * (avg_tenure_months - STABILITY_ZERO_MONTHS) / span


# ── Education fit score (Improvement 2) ──────────────────────────────────────

def education_fit_score(resume_text: str) -> tuple[float, dict]:
    """Score a fresher's academic background against target degree/branch."""
    text_lc = (resume_text or "").lower()

    # Detect degree presence (BE/BTech/BSc/MCA/MSc etc.)
    high_degree_patterns = [
        r'\bb\.?tech\b', r'\bb\.?e\.?\b', r'\bb\.?sc\b',
        r'\bmca\b', r'\bm\.?sc\b', r'\bm\.?tech\b', r'\bm\.?e\.?\b',
    ]
    has_high_degree = any(re.search(p, text_lc) for p in high_degree_patterns)

    # Branch alignment
    if any(b in text_lc for b in _EDU_BRANCHES_HIGH):
        branch_score = 100.0
        branch_category = "high"
    elif any(b in text_lc for b in _EDU_BRANCHES_MED):
        branch_score = 60.0
        branch_category = "medium"
    elif any(t in text_lc for t in ["engineering", "technology"]):
        branch_score = 30.0
        branch_category = "technical"
    else:
        branch_score = 10.0
        branch_category = "non_technical"

    # Degree modifier — non-technical degrees discounted
    degree_modifier = 1.0 if has_high_degree else 0.7

    # CGPA / percentage boost (+10 if >= 7.0 CGPA or >= 70%)
    cgpa_boost = 0.0
    for val in re.findall(r'cgpa[:\s]*([0-9]+(?:\.[0-9]+)?)', text_lc):
        try:
            if float(val) >= 7.0:
                cgpa_boost = 10.0
                break
        except ValueError:
            pass
    if not cgpa_boost:
        for val in re.findall(r'([0-9]+(?:\.[0-9]+)?)\s*%', text_lc):
            try:
                if float(val) >= 70.0:
                    cgpa_boost = 10.0
                    break
            except ValueError:
                pass

    final = min(100.0, round(branch_score * degree_modifier + cgpa_boost, 1))
    return final, {
        "education_fit_score": final,
        "has_high_degree":     has_high_degree,
        "branch_category":     branch_category,
        "cgpa_boost":          cgpa_boost,
    }


# ── Project relevance score (Improvement 2) ──────────────────────────────────

def project_relevance_score(resume_text: str, key_skills: list[str]) -> tuple[float, dict]:
    """Match JD keywords against the Projects section of a fresher resume."""
    if not key_skills:
        return 0.0, {"projects_section_found": False, "project_relevance_score": 0.0}

    text_lc = (resume_text or "").lower()

    # Locate Projects heading
    project_start = -1
    for heading in _PROJECT_HEADINGS:
        idx = text_lc.find(heading)
        if idx != -1 and (project_start == -1 or idx < project_start):
            project_start = idx

    if project_start == -1:
        return 0.0, {
            "projects_section_found":  False,
            "project_relevance_score": 0.0,
            "note": "no projects section found",
        }

    project_text = text_lc[project_start: project_start + 500]

    matched_in_projects: list[str] = []
    for s in key_skills:
        s_lc    = s.lower()
        aliases = KEYWORD_ALIASES.get(s_lc, [s_lc])
        if any(alias in project_text for alias in aliases):
            matched_in_projects.append(s)

    score = round(100.0 * len(matched_in_projects) / len(key_skills), 1)
    return score, {
        "projects_section_found":  True,
        "project_relevance_score": score,
        "project_matched_skills":  matched_in_projects,
        "project_missing_skills":  [s for s in key_skills if s not in matched_in_projects],
    }


# ── Fresher detection (Improvement 2) ────────────────────────────────────────

_COMPANY_INDICATORS = [
    "pvt", "ltd", "private limited", "inc.", "corporation",
    "company", "technologies", "solutions", "services",
    "internship",
]


def _detect_fresher(
    candidate_years: Optional[float],
    resume_text: str,
    roles_parsed: int,
) -> bool:
    """Return True if the candidate is a fresher (0–1 years experience)."""
    if candidate_years is not None and float(candidate_years) < 1.0:
        return True
    text_lc = (resume_text or "").lower()
    # Recent grad year 2023-2027 with no detectable company mentions
    if re.search(r'\b202[3-7]\b', text_lc):
        has_company = any(ind in text_lc for ind in _COMPANY_INDICATORS)
        if not has_company and roles_parsed == 0:
            return True
    return False


# ── Keyword match (alias-aware, Improvement 3) ───────────────────────────────

def keyword_match_score(resume_text: str, key_skills: list[str]) -> tuple[float, dict]:
    """Fraction of required skills found in resume text (alias-expanded), 0-100."""
    if not key_skills:
        return 50.0, {"matched_skills": [], "note": "no key skills defined"}
    resume_lc = (resume_text or "").lower()
    matched: list[str] = []
    matched_via_alias: list[str] = []
    for s in key_skills:
        s_lc    = s.lower()
        aliases = KEYWORD_ALIASES.get(s_lc, [s_lc])
        if any(alias in resume_lc for alias in aliases):
            matched.append(s)
            # Note alias hit only when the canonical form itself isn't in text
            if s_lc not in resume_lc:
                matched_via_alias.append(s)
    score = 100.0 * len(matched) / len(key_skills)
    return score, {
        "matched_skills":       matched,
        "missing_skills":       [s for s in key_skills if s not in matched],
        "skills_matched_count": len(matched),
        "skills_total":         len(key_skills),
        "matched_via_alias":    matched_via_alias,
    }


# ── Experience score ──────────────────────────────────────────────────────────

def experience_score(years: Optional[float], min_required: Optional[float]) -> tuple[float, dict]:
    """100 if meets/exceeds requirement, partial credit below."""
    if min_required is None or years is None:
        return 50.0, {"experience_note": "experience not evaluated"}
    years        = float(years)
    min_required = float(min_required)
    if years >= min_required:
        return 100.0, {"experience_met": True, "years": years, "required": min_required}
    ratio = max(0.0, years / min_required) if min_required else 0.0
    return 100.0 * ratio, {"experience_met": False, "years": years, "required": min_required}


# ── AI holistic screen (Groq, Improvements 1 & 2) ────────────────────────────

def ai_screen(
    resume_text: str,
    job_description: str,
    key_skills: Optional[list[str]] = None,
    title: str = "",
    band_code: str = "",
    is_fresher: bool = False,
) -> tuple[float, dict]:
    """
    Groq LLM holistic resume-vs-JD evaluation.
    Returns (score_0-100_used_in_composite, detail_dict).

    On any failure: composite score component = 50 (unchanged formula),
    but ai_fit_score = None and ai_score_status = "unavailable" in the
    breakdown so the UI can surface a ⚠️ warning to recruiters.
    """
    system_prompt = _AI_FRESHER_SCREEN_SYSTEM if is_fresher else _AI_SCREEN_SYSTEM
    try:
        skills_str   = ", ".join(key_skills or []) or "not specified"
        user_content = (
            f"ROLE: {title or 'not specified'}\n"
            f"BAND/SENIORITY: {band_code or 'not specified'}\n"
            f"KEY SKILLS: {skills_str}\n"
            f"JOB DESCRIPTION:\n{(job_description or '')[:1200]}\n\n"
            f"RESUME:\n{(resume_text or '')[:3000]}\n\n"
            "Evaluate this candidate. Return JSON only."
        )
        response = _get_client().chat.completions.create(
            model=_llm_model(),
            max_tokens=500,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
        parsed = json.loads(raw)
        score  = max(0, min(100, int(round(float(parsed["ai_fit_score"])))))
        return float(score), {
            "ai_fit_score":    score,
            "ai_score_status": "scored",
            "strengths":       parsed.get("strengths", ""),
            "concerns":        parsed.get("concerns",  ""),
            "rationale":       parsed.get("rationale", ""),
            "learning_signals":parsed.get("learning_signals", "") if is_fresher else None,
            "scored_by":       "groq",
        }
    except Exception as exc:
        print(f"[screening] Groq AI screen failed — neutral fallback (50): {exc}")
        # Improvement 1: expose failure clearly; composite still uses 50 so
        # match_score is not zeroed, but the breakdown flags it for the UI.
        return 50.0, {
            "ai_fit_score":    None,           # NULL stored in DB column
            "ai_score_status": "unavailable",  # triggers ⚠️ badge in UI
            "ai_score_fallback": True,
            "strengths":       "",
            "concerns":        "",
            "rationale":       "",
            "scored_by":       "fallback",
            "fallback_reason": str(exc)[:200],
        }


# ── Combined scorer ───────────────────────────────────────────────────────────

def score_application(
    resume_text: str,
    candidate_years: Optional[float],
    requisition: dict,
    file_size_bytes: int = 0,
) -> tuple[float, dict]:
    """
    Blend keyword, experience, AI-fit, and (for experienced) stability into
    one 0-100 match score.  Freshers use the education+project model instead.

    Returns (final_score, breakdown_dict).

    breakdown_dict includes all sub-scores, weights, AI reasoning, stability
    status, parse quality flags, and fresher signals — ready to store in
    score_breakdown JSONB and display in the 'Why this score?' UI.
    """
    from .cv_parser import assess_parse_quality  # lightweight, avoids circular import at module level

    key_skills = requisition.get("key_skills") or []
    jd         = requisition.get("job_description") or ""
    min_exp    = requisition.get("min_experience")
    title      = requisition.get("title", "")
    band_code  = requisition.get("band_code", "")
    is_fresher_role = bool(requisition.get("is_fresher_role", False))

    # Parse quality (Improvement 4)
    parse_quality_info = assess_parse_quality(resume_text, file_size_bytes)

    # Always compute keyword and experience scores
    skills_s, skills_b = keyword_match_score(resume_text, key_skills)
    exp_s,    exp_b    = experience_score(candidate_years, min_exp)

    avg_months, tenure_b = _parse_avg_tenure(resume_text)
    roles_parsed = tenure_b.get("roles_parsed", 0)

    # Fresher detection (Improvement 2)
    is_fresher = is_fresher_role or _detect_fresher(candidate_years, resume_text, roles_parsed)

    if is_fresher:
        # ── Fresher scoring model ─────────────────────────────────────────────
        ai_s, ai_b = ai_screen(resume_text, jd, key_skills, title, band_code, is_fresher=True)
        edu_s, edu_b   = education_fit_score(resume_text)
        proj_s, proj_b = project_relevance_score(resume_text, key_skills)

        w_edu  = 0.25
        w_proj = 0.25
        w_kw   = 0.20
        w_ai   = 0.30
        final  = edu_s * w_edu + proj_s * w_proj + skills_s * w_kw + float(ai_s) * w_ai

        weights_used = {
            "education": w_edu, "project": w_proj,
            "keyword":   w_kw,  "ai":      w_ai,
            "stability": 0,
        }
        breakdown = {
            "is_fresher":       True,
            "is_experienced":   False,
            "education_score":  round(edu_s, 1),
            "project_score":    round(proj_s, 1),
            "skills_score":     round(skills_s, 1),
            "ai_score":         round(float(ai_s), 1),
            "stability_score":  None,
            "stability_status": "not_applicable",
            "weights":          weights_used,
            "avg_tenure_months": None,
            **tenure_b,
            **edu_b,
            **proj_b,
            **skills_b,
            **exp_b,
            **ai_b,
            **parse_quality_info,
        }
        return round(final, 1), breakdown

    # ── Experienced scoring model ─────────────────────────────────────────────
    is_experienced = (
        (candidate_years is not None and float(candidate_years) >= EXPERIENCED_MIN_YEARS)
        or roles_parsed >= EXPERIENCED_MIN_ROLES
    )

    ai_s, ai_b = ai_screen(resume_text, jd, key_skills, title, band_code, is_fresher=False)

    stability_s:      Optional[float]
    stability_status: str

    if not is_experienced:
        stability_s      = None
        stability_status = "not_applicable"
    elif avg_months is not None:
        stability_s      = compute_stability_score(avg_months)
        stability_status = "computed"
    else:
        stability_s      = None
        stability_status = "pending_manual"

    if is_experienced and stability_status == "computed" and stability_s is not None:
        w_kw  = SCORE_WEIGHT_KEYWORD
        w_exp = SCORE_WEIGHT_EXPERIENCE
        w_ai  = SCORE_WEIGHT_AI
        w_st  = SCORE_WEIGHT_STABILITY
        final = (skills_s * w_kw + exp_s * w_exp
                 + float(ai_s) * w_ai + stability_s * w_st)
        weights_used = {
            "keyword": w_kw, "experience": w_exp, "ai": w_ai, "stability": w_st,
        }
    else:
        base  = SCORE_WEIGHT_KEYWORD + SCORE_WEIGHT_EXPERIENCE + SCORE_WEIGHT_AI
        w_kw  = SCORE_WEIGHT_KEYWORD    / base
        w_exp = SCORE_WEIGHT_EXPERIENCE / base
        w_ai  = SCORE_WEIGHT_AI         / base
        final = skills_s * w_kw + exp_s * w_exp + float(ai_s) * w_ai
        weights_used = {
            "keyword":    round(w_kw,  4),
            "experience": round(w_exp, 4),
            "ai":         round(w_ai,  4),
            "stability":  0,
        }

    breakdown = {
        "is_fresher":       False,
        "is_experienced":   is_experienced,
        "skills_score":     round(skills_s, 1),
        "experience_score": round(exp_s, 1),
        "ai_score":         round(float(ai_s), 1),
        "stability_score":  round(stability_s, 1) if stability_s is not None else None,
        "weights":          weights_used,
        "stability_status": stability_status,
        "avg_tenure_months": round(avg_months, 1) if avg_months is not None else None,
        **tenure_b,
        **skills_b,
        **exp_b,
        **ai_b,
        **parse_quality_info,
    }
    return round(final, 1), breakdown
