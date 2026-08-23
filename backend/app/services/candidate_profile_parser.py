"""
Resume -> structured candidate profile parsing, using the same Groq LLM
already used by cv_enricher.py (skills/experience_years extraction) --
this extends that idea to the richer shape the candidate portal's My
Profile tab needs: given name, phone, up to 5 work experiences, and
education entries. Regex (resume_parser.extract_contact_info) can reliably
get a single name/email/phone, but multi-entry work history and education
are not reliably extractable without an LLM.

Called synchronously (not a background queue like cv_enricher) from two
places, both of which treat a None return as "nothing to prefill" and must
never fail the caller's own action:
  1. main.py._maybe_issue_candidate_invite -- first time a candidate_user
     account is created, to prefill their brand-new profile.
  2. candidate_portal_api.portal_update_resume -- whenever a candidate
     re-uploads their resume from the portal.

Merge policy (deliberate, not a placeholder): parsed results only fill
fields that are currently EMPTY. A candidate's own manual edits are never
silently overwritten by a later resume upload -- see _fill_empty_only in
candidate_portal_api.py. If a candidate wants a full refresh from a new
resume, they clear/delete the entries themselves first.
"""
import json
import os
import re
from typing import Optional


def _make_client():
    import openai
    return openai.OpenAI(
        api_key=os.environ.get("GROQ_API_KEY"),
        base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
    )


def _model() -> str:
    return os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")


_SYSTEM = """\
You are a resume parsing assistant. Extract structured data from the resume text below.
Return ONLY a valid JSON object -- no markdown fences, no prose before or after.

Required fields:
{
  "given_name": "<the candidate's first/given name only, or null>",
  "phone": "<phone number as written, or null>",
  "skills": ["array", "of", "normalized", "lowercase", "technical", "skills"],
  "work_experience": [
    {
      "company": "<employer name>",
      "title": "<job title>",
      "start_month": <1-12 or null>,
      "start_year": <4-digit year or null>,
      "end_month": <1-12 or null, null if current>,
      "end_year": <4-digit year or null, null if current>,
      "is_current": <true if this is their present role, else false>,
      "description": "<1-2 sentence summary of responsibilities, or null>"
    }
  ],
  "education": [
    {
      "institution": "<school/university name>",
      "degree": "<degree name, e.g. B.Tech, MBA>",
      "field_of_study": "<major/field, or null>",
      "start_year": <4-digit year or null>,
      "end_year": <4-digit year or null>
    }
  ]
}

List work_experience in reverse-chronological order (most recent first), and
return AT MOST 5 entries. List education entries similarly, most recent first."""

_MAX_TOKENS  = 1400
_MAX_RETRIES = 2


def _strip_fences(s: str) -> str:
    s = re.sub(r'^```(?:json)?\s*', '', s.strip(), flags=re.IGNORECASE)
    s = re.sub(r'\s*```$', '', s.strip())
    return s.strip()


def _clean_work_experience(raw: list) -> list:
    cleaned = []
    for item in (raw or [])[:5]:
        if not isinstance(item, dict) or not item.get("company") or not item.get("title"):
            continue
        cleaned.append({
            "company":     str(item["company"])[:200],
            "title":       str(item["title"])[:200],
            "start_month": _safe_int(item.get("start_month"), 1, 12),
            "start_year":  _safe_int(item.get("start_year"), 1950, 2100),
            "end_month":   _safe_int(item.get("end_month"), 1, 12),
            "end_year":    _safe_int(item.get("end_year"), 1950, 2100),
            "is_current":  bool(item.get("is_current")),
            "description": (str(item["description"])[:1000] if item.get("description") else None),
        })
    return cleaned


def _clean_education(raw: list) -> list:
    cleaned = []
    for item in (raw or [])[:8]:
        if not isinstance(item, dict) or not item.get("institution"):
            continue
        cleaned.append({
            "institution":    str(item["institution"])[:200],
            "degree":         (str(item["degree"])[:150] if item.get("degree") else None),
            "field_of_study": (str(item["field_of_study"])[:150] if item.get("field_of_study") else None),
            "start_year":     _safe_int(item.get("start_year"), 1950, 2100),
            "end_year":       _safe_int(item.get("end_year"), 1950, 2100),
        })
    return cleaned


def _safe_int(val, lo: int, hi: int) -> Optional[int]:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def parse_resume_to_profile(resume_text: str) -> Optional[dict]:
    """
    Synchronous Groq call. Returns a dict with given_name/phone/skills/
    work_experience/education on success, or None on any failure (bad
    response, network error, exhausted retries) -- callers must treat None
    as "nothing to prefill," never as fatal to whatever they were doing
    (account creation, resume re-upload).
    """
    if not resume_text or not (os.environ.get("GROQ_API_KEY") or "").strip():
        return None

    client = _make_client()
    text_truncated = resume_text[:8000]

    for attempt in range(_MAX_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=_model(),
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": f"Resume text:\n\n{text_truncated}"},
                ],
                temperature=0,
                max_tokens=_MAX_TOKENS,
            )
            raw = resp.choices[0].message.content or ""
            data = json.loads(_strip_fences(raw))
            return {
                "given_name":      (str(data.get("given_name") or "").strip() or None),
                "phone":           (str(data.get("phone") or "").strip() or None),
                "skills":          [str(s).lower() for s in (data.get("skills") or []) if s],
                "work_experience": _clean_work_experience(data.get("work_experience")),
                "education":       _clean_education(data.get("education")),
            }
        except Exception as exc:
            print(f"[candidate-profile-parser] attempt {attempt + 1} failed: {exc}")

    return None


def apply_parsed_profile(candidate_id: str, parsed: dict) -> None:
    """
    Fill-empty-only merge of a parse_resume_to_profile() result into the
    candidate's profile -- called from both first-invite (main.py) and
    resume re-upload (candidate_portal_api.py). Never overwrites a field or
    a whole collection (work experience / education) that already has data;
    a candidate's own edits always win over a later resume upload. Non-
    fatal by contract: any DB error here must not fail the caller's own
    action (account creation, resume upload), so exceptions are caught and
    logged, never raised.
    """
    from ..db import query, query_one
    try:
        cand = query_one(
            "SELECT given_name, phone, skills FROM candidate WHERE id=%s", [candidate_id]
        )
        if not cand:
            return

        if not cand.get("given_name") and parsed.get("given_name"):
            query("UPDATE candidate SET given_name=%s WHERE id=%s",
                  [parsed["given_name"], candidate_id], fetch=False)
        if not cand.get("phone") and parsed.get("phone"):
            query("UPDATE candidate SET phone=%s WHERE id=%s",
                  [parsed["phone"], candidate_id], fetch=False)

        if parsed.get("skills"):
            existing_skills = set(cand.get("skills") or [])
            merged = sorted(existing_skills | set(parsed["skills"]))
            if merged != sorted(existing_skills):
                query("UPDATE candidate SET skills=%s WHERE id=%s",
                      [merged, candidate_id], fetch=False)

        if parsed.get("work_experience"):
            has_existing = query_one(
                "SELECT 1 FROM candidate_work_experience WHERE candidate_id=%s LIMIT 1",
                [candidate_id],
            )
            if not has_existing:
                for i, exp in enumerate(parsed["work_experience"][:5]):
                    query(
                        """INSERT INTO candidate_work_experience
                             (candidate_id, company, title, start_month, start_year,
                              end_month, end_year, is_current, description, source, sort_order)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'resume_parse',%s)""",
                        [candidate_id, exp["company"], exp["title"], exp["start_month"],
                         exp["start_year"], exp["end_month"], exp["end_year"],
                         exp["is_current"], exp["description"], i],
                        fetch=False,
                    )

        if parsed.get("education"):
            has_existing = query_one(
                "SELECT 1 FROM candidate_education WHERE candidate_id=%s LIMIT 1",
                [candidate_id],
            )
            if not has_existing:
                for i, edu in enumerate(parsed["education"][:8]):
                    query(
                        """INSERT INTO candidate_education
                             (candidate_id, institution, degree, field_of_study,
                              start_year, end_year, source, sort_order)
                           VALUES (%s,%s,%s,%s,%s,%s,'resume_parse',%s)""",
                        [candidate_id, edu["institution"], edu["degree"], edu["field_of_study"],
                         edu["start_year"], edu["end_year"], i],
                        fetch=False,
                    )
    except Exception as exc:
        print(f"[candidate-profile-parser] apply_parsed_profile failed for {candidate_id} (non-fatal): {exc}")
