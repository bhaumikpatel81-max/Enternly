"""
CV Repository AI Screening Scorecard.

On-demand resume-vs-JD scoring for any CV Repository entry against any
requisition, independent of whether a real application exists yet. Reuses
services/screening.py's score_application() -- the same scoring engine
already used automatically when a candidate applies -- so scoring logic
lives in exactly one place.

Roles: ta_manager, recruiter, admin only (matches CV Repository access).
Results are cached per (cv, requisition) pair in cv_scorecard so re-opening
the same pairing doesn't re-call the Groq LLM; a "force" flag re-scores.
"""
import json
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user
from ..db import query, query_one
from ..services import screening

router = APIRouter(prefix="/api/cv", tags=["cv-ai-scorecard"])

_ALLOWED = {"ta_manager", "recruiter", "admin"}


def _require(user: dict):
    if user.get("role") not in _ALLOWED:
        raise HTTPException(403, "AI Screening Scorecard: ta_manager / recruiter / admin only")


def _json_safe(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"not serializable: {type(obj)}")


class ScorecardIn(BaseModel):
    requisition_id: str
    force: bool = False


def _serialize(row: dict, cv: dict, req: dict, cached: bool) -> dict:
    return {
        "id":               str(row["id"]),
        "cv_id":            str(row["cv_repository_id"]),
        "requisition_id":   str(row["requisition_id"]),
        "match_score":      float(row["match_score"]) if row["match_score"] is not None else None,
        "score_breakdown":  row["score_breakdown"] or {},
        "candidate_name":   cv.get("candidate_name") or cv.get("file_name"),
        "requisition_title": req.get("title"),
        "cached":           cached,
        "created_at":       row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at":       row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


@router.post("/{cv_id}/scorecard")
def generate_scorecard(cv_id: str, body: ScorecardIn, user: dict = Depends(get_current_user)):
    _require(user)

    cv = query_one(
        """SELECT id, raw_text, experience_years, candidate_name, file_name, candidate_id
           FROM cv_repository WHERE id=%s""",
        [cv_id],
    )
    if not cv:
        raise HTTPException(404, "CV not found")
    if not (cv["raw_text"] or "").strip():
        raise HTTPException(
            422,
            "This CV has no extracted resume text yet, so an AI scorecard can't be "
            "generated. Wait for AI enrichment to finish, or re-upload the file.",
        )

    req = query_one(
        """SELECT r.id, r.title, r.status, r.job_description, r.key_skills,
                  r.min_experience, r.is_fresher_role, b.code AS band_code
           FROM requisition r
           JOIN band b ON b.id = r.band_id
           WHERE r.id=%s""",
        [body.requisition_id],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    if not body.force:
        existing = query_one(
            """SELECT * FROM cv_scorecard
               WHERE cv_repository_id=%s AND requisition_id=%s""",
            [cv_id, body.requisition_id],
        )
        if existing:
            return _serialize(existing, cv, req, cached=True)

    years = float(cv["experience_years"]) if cv["experience_years"] is not None else None
    score, breakdown = screening.score_application(cv["raw_text"], years, dict(req))

    row = query_one(
        """INSERT INTO cv_scorecard
               (cv_repository_id, requisition_id, match_score, score_breakdown, scored_by)
           VALUES (%s, %s, %s, %s::jsonb, %s)
           ON CONFLICT (cv_repository_id, requisition_id)
           DO UPDATE SET match_score     = EXCLUDED.match_score,
                         score_breakdown = EXCLUDED.score_breakdown,
                         scored_by       = EXCLUDED.scored_by,
                         updated_at      = now()
           RETURNING *""",
        [cv_id, body.requisition_id, score, json.dumps(breakdown, default=_json_safe), user["sub"]],
    )
    return _serialize(row, cv, req, cached=False)


@router.get("/{cv_id}/scorecard/{requisition_id}")
def get_scorecard(cv_id: str, requisition_id: str, user: dict = Depends(get_current_user)):
    """Fetch a previously generated scorecard without triggering a (re)score."""
    _require(user)
    row = query_one(
        """SELECT * FROM cv_scorecard
           WHERE cv_repository_id=%s AND requisition_id=%s""",
        [cv_id, requisition_id],
    )
    if not row:
        raise HTTPException(404, "No scorecard generated yet for this candidate/requisition pair")
    cv = query_one(
        "SELECT candidate_name, file_name FROM cv_repository WHERE id=%s", [cv_id]
    ) or {}
    req = query_one("SELECT title FROM requisition WHERE id=%s", [requisition_id]) or {}
    return _serialize(row, cv, req, cached=True)
