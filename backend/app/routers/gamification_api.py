"""
Gamification API.

GET /api/gamification/me          — caller's own points/tier/badges/rank (all authenticated roles)
GET /api/gamification/leaderboard — ta_manager/admin only; full per-persona board
GET /api/gamification/config      — ta_manager/admin only; read config
PATCH /api/gamification/config    — ta_manager/admin only; edit base_points / multipliers / thresholds
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user
from ..db import query, query_one
from ..services.gamification import get_profile, score_for, tier_for, rank_for, badge_meta

router = APIRouter(prefix="/api/gamification", tags=["gamification"])

_TA_ROLES = {"ta_manager", "admin"}


def _subject_for(user: dict) -> tuple[str, str]:
    """Map a staff JWT payload → (subject_type, subject_id) for the leaderboard."""
    role = user.get("role", "")
    uid  = user["sub"]
    if role in ("ta_manager", "recruiter", "admin"):
        return "recruiter", uid
    if role == "hiring_manager":
        return "hm", uid
    return "recruiter", uid


# ── /me — every authenticated user ────────────────────────────────────────────

@router.get("/me")
def my_profile(period: str = "all", user: dict = Depends(get_current_user)):
    """Returns the caller's own gamification profile (points, tier, badges, rank)."""
    subject_type, subject_id = _subject_for(user)
    return get_profile(subject_type, subject_id, period, user.get("tenant_id"))


# ── /leaderboard — TA managers and admins only ────────────────────────────────

@router.get("/leaderboard")
def leaderboard(
    period: str          = "all",
    subject_type: str    = "recruiter",
    user: dict           = Depends(get_current_user),
):
    """
    Full leaderboard for a persona. ta_manager/admin only.
    Any other role gets a 403 — they use /me for their own rank.
    """
    if user.get("role") not in _TA_ROLES:
        raise HTTPException(403, "Full leaderboard is visible to ta_manager / admin only. Use /me for your own rank.")

    if subject_type not in ("recruiter", "vendor", "candidate", "hm"):
        raise HTTPException(400, "subject_type must be recruiter|vendor|candidate|hm")

    period_filter = {
        "month":   "AND created_at >= date_trunc('month', now())",
        "quarter": "AND created_at >= date_trunc('quarter', now())",
        "ytd":     "AND created_at >= date_trunc('year', now())",
        "all":     "",
    }.get(period, "")

    tenant_id = user.get("tenant_id")
    rows = query(
        f"""SELECT subject_id,
                   SUM(points_awarded) AS total_points,
                   COUNT(*)            AS event_count
            FROM gamification_event
            WHERE subject_type=%s AND tenant_id=%s {period_filter}
            GROUP BY subject_id
            ORDER BY total_points DESC
            LIMIT 50""",
        [subject_type, tenant_id],
    )

    result = []
    for i, r in enumerate(rows or [], start=1):
        sid    = str(r["subject_id"])
        points = float(r["total_points"])
        # Look up display name
        name = _resolve_name(subject_type, sid, tenant_id)
        badges = query(
            "SELECT badge_key FROM gamification_badge WHERE subject_type=%s AND subject_id=%s AND tenant_id=%s",
            [subject_type, sid, tenant_id],
        )
        result.append({
            "rank":        i,
            "subject_id":  sid,
            "name":        name,
            "points":      points,
            "tier":        tier_for(points),
            "event_count": int(r["event_count"]),
            "badge_count": len(badges or []),
        })
    return result


@router.get("/history")
def leaderboard_history(
    subject_type: str = "recruiter",
    user: dict        = Depends(get_current_user),
):
    """
    Per-calendar-year leaderboard archive. ta_manager/admin only.
    Points reset each year in the live leaderboard/profile (period=ytd);
    this endpoint is what keeps prior years' totals and badges visible
    once a new year starts. Tier is derived from CURRENT config thresholds,
    same as everywhere else — never stored as a mutable value.
    """
    if user.get("role") not in _TA_ROLES:
        raise HTTPException(403, "Full leaderboard is visible to ta_manager / admin only. Use /me for your own rank.")

    if subject_type not in ("recruiter", "vendor", "candidate", "hm"):
        raise HTTPException(400, "subject_type must be recruiter|vendor|candidate|hm")

    tenant_id = user.get("tenant_id")
    rows = query(
        """SELECT EXTRACT(YEAR FROM created_at)::int AS year,
                  subject_id,
                  SUM(points_awarded) AS total_points,
                  COUNT(*)            AS event_count
           FROM gamification_event
           WHERE subject_type=%s AND tenant_id=%s
           GROUP BY year, subject_id
           ORDER BY year DESC, total_points DESC""",
        [subject_type, tenant_id],
    )

    badge_rows = query(
        """SELECT EXTRACT(YEAR FROM earned_at)::int AS year, subject_id, badge_key
           FROM gamification_badge
           WHERE subject_type=%s AND tenant_id=%s""",
        [subject_type, tenant_id],
    )
    badges_by_key: dict = {}
    for b in badge_rows or []:
        k = (int(b["year"]), str(b["subject_id"]))
        badges_by_key.setdefault(k, []).append(b["badge_key"])

    years: dict = {}
    for r in rows or []:
        year   = int(r["year"])
        sid    = str(r["subject_id"])
        points = float(r["total_points"])
        badge_keys = badges_by_key.get((year, sid), [])
        years.setdefault(year, []).append({
            "subject_id":  sid,
            "name":        _resolve_name(subject_type, sid, tenant_id),
            "points":      points,
            "tier":        tier_for(points),
            "event_count": int(r["event_count"]),
            "badges":      [{"key": bk, **badge_meta(bk)} for bk in badge_keys],
            "badge_count": len(badge_keys),
        })

    result = []
    for year in sorted(years.keys(), reverse=True):
        entries = sorted(years[year], key=lambda e: -e["points"])
        for i, e in enumerate(entries, start=1):
            e["rank"] = i
        result.append({"year": year, "entries": entries})
    return result


@router.get("/excel")
def leaderboard_excel(
    period: str       = "all",
    subject_type: str = "recruiter",
    user: dict        = Depends(get_current_user),
):
    from datetime import datetime
    import openpyxl
    from ..services import excel_export

    rows = leaderboard(period=period, subject_type=subject_type, user=user)
    sheet_rows = [
        {
            "Rank": r["rank"], "Name": r["name"], "Points": r["points"], "Tier": r["tier"],
            "Events": r["event_count"], "Badges": r["badge_count"],
        }
        for r in rows
    ]

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    excel_export.sheet_from_rows(wb, "Leaderboard", sheet_rows)
    excel_export.build_summary_sheet(
        wb,
        title=f"Leaderboard — {subject_type.title()} ({period})",
        generated_by=user.get("name") or user.get("email") or "",
        generated_at=datetime.now(),
        filters_applied=[{"key": "period", "op": "=", "value": period}, {"key": "subject_type", "op": "=", "value": subject_type}],
        rows=sheet_rows,
        measures_meta=[{"key": "Points", "label": "Points"}],
    )
    return excel_export.stream_workbook(wb, f"enternly_leaderboard_{subject_type}_{period}.xlsx")


def _resolve_name(subject_type: str, subject_id: str, tenant_id: str = None) -> str:
    if subject_type in ("recruiter", "hm"):
        row = query_one("SELECT full_name FROM app_user WHERE id=%s AND tenant_id=%s", [subject_id, tenant_id])
    elif subject_type == "vendor":
        row = query_one("SELECT full_name FROM vendor_user WHERE id=%s AND tenant_id=%s", [subject_id, tenant_id])
    elif subject_type == "candidate":
        row = query_one(
            """SELECT c.full_name FROM candidate_user cu
               JOIN candidate c ON c.id = cu.candidate_id
               WHERE cu.id=%s AND cu.tenant_id=%s""",
            [subject_id, tenant_id],
        )
    else:
        row = None
    return (row or {}).get("full_name", "Unknown")


# ── Config read/write (TA admin) ──────────────────────────────────────────────

@router.get("/config")
def get_config(user: dict = Depends(get_current_user)):
    if user.get("role") not in _TA_ROLES:
        raise HTTPException(403, "ta_manager / admin only")
    return query(
        "SELECT key, value, updated_at FROM gamification_config WHERE tenant_id=%s ORDER BY key",
        [user.get("tenant_id")],
    )


class ConfigPatchIn(BaseModel):
    key:   str
    value: str


@router.patch("/config")
def patch_config(body: ConfigPatchIn, user: dict = Depends(get_current_user)):
    if user.get("role") not in _TA_ROLES:
        raise HTTPException(403, "ta_manager / admin only")
    tenant_id = user.get("tenant_id")
    existing = query_one("SELECT key FROM gamification_config WHERE key=%s AND tenant_id=%s", [body.key, tenant_id])
    if not existing:
        raise HTTPException(404, f"Config key '{body.key}' not found")
    query(
        """UPDATE gamification_config SET value=%s, updated_at=now(), updated_by=%s
           WHERE key=%s AND tenant_id=%s""",
        [body.value, user["sub"], body.key, tenant_id], fetch=False,
    )
    return {"ok": True, "key": body.key, "value": body.value}
