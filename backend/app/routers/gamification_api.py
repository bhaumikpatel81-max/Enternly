"""
Gamification API.

GET /api/gamification/me          — caller's own points/tier/badges/rank (all authenticated roles)
GET /api/gamification/leaderboard — ta_manager/admin only; full per-persona board
GET /api/gamification/config      — ta_manager/admin only; read config
PATCH /api/gamification/config    — ta_manager/admin only; edit base_points / multipliers / thresholds

GET  /api/gamification/daily-question        — today's HR trivia question (all authenticated roles)
POST /api/gamification/daily-question/answer — submit today's answer, awards points via the ledger
POST /api/gamification/daily-question/skip   — skip today's question without breaking the streak
"""
import hashlib
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user
from ..db import query, query_one
from ..services.gamification import get_profile, score_for, tier_for, rank_for, badge_meta, award as gam_award
from ..services.report_scope import scope_for

from ..module_access import require_tenant_module

router = APIRouter(prefix="/api/gamification", tags=["gamification"],
                    dependencies=[Depends(require_tenant_module("gamification"))])

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


# ── Daily HR trivia question ───────────────────────────────────────────────────

def _pick_daily_question(tenant_id: str, subject_type: str, subject_id: str, today: date) -> dict | None:
    """
    Deterministic per (subject, day) pick — refreshing the page must never
    reroll the question. Uses an unsalted hash (never Python's builtin
    hash(), which is randomized per process) over subject_id+date, then
    probes forward through the active bank to skip whichever questions this
    subject answered most recently, so the same question doesn't repeat
    back-to-back once the bank is larger than a couple of entries.
    """
    questions = query(
        """SELECT id, question_text, option_a, option_b, option_c, correct_option, explanation_text
           FROM hr_question WHERE tenant_id=%s AND active=true ORDER BY created_at""",
        [tenant_id],
    ) or []
    if not questions:
        return None

    recent = query(
        """SELECT question_id FROM user_question_answer
           WHERE subject_type=%s AND subject_id=%s
           ORDER BY answer_date DESC LIMIT %s""",
        [subject_type, str(subject_id), max(len(questions) - 1, 1)],
    ) or []
    recent_ids = {str(r["question_id"]) for r in recent}

    digest = hashlib.md5(f"{subject_id}:{today.isoformat()}".encode()).hexdigest()
    idx = int(digest, 16) % len(questions)
    for step in range(len(questions)):
        candidate = questions[(idx + step) % len(questions)]
        if str(candidate["id"]) not in recent_ids:
            return candidate
    return questions[idx]


def _has_urgent_requisitions(user: dict) -> bool:
    """Drives the frontend's auto-pause rule: hide the daily question while
    the caller has any open High/Critical requisition. Reuses the same
    role-scoping helper every other report/dashboard query uses, so this
    stays role-correct (a recruiter only sees their own reqs, a hiring
    manager only theirs, ta_manager/admin see the whole tenant) for free."""
    role = user.get("role", "")
    uid = user["sub"]
    tenant_id = user.get("tenant_id")
    join_sql, where_sql, join_params, where_params = scope_for(role, uid, tenant_id)
    row = query_one(
        f"""SELECT 1 FROM requisition r
            {join_sql}
            WHERE r.status='open' AND r.criticality IN ('High','Critical') {where_sql}
            LIMIT 1""",
        join_params + where_params,
    )
    return bool(row)


def _bump_streak(subject_type: str, subject_id: str, tenant_id: str, today: date, grow: bool) -> dict:
    """
    Update (or create) this subject's streak row. `grow=True` (a submitted
    answer, correct or not) increments the streak when yesterday was the
    last activity day, or resets it to 1 on a gap. `grow=False` (a skip)
    only stamps last_activity_date=today so tomorrow's gap check treats
    today as covered — it never increments or resets current_streak,
    which is the only reading consistent with "skipping does not break a
    streak" that doesn't also let a user farm an unlimited streak by
    skipping forever.
    """
    row = query_one(
        """SELECT current_streak, longest_streak, last_activity_date
           FROM user_gamification_streak WHERE subject_type=%s AND subject_id=%s""",
        [subject_type, str(subject_id)],
    )

    if not row:
        current = 1 if grow else 0
        longest = current
        query(
            """INSERT INTO user_gamification_streak
                   (subject_type, subject_id, tenant_id, current_streak, longest_streak, last_activity_date)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            [subject_type, str(subject_id), tenant_id, current, longest, today],
            fetch=False,
        )
        return {"current": current, "longest": longest}

    current, longest, last = row["current_streak"], row["longest_streak"], row["last_activity_date"]
    if grow:
        if last == today - timedelta(days=1):
            current += 1
        elif last != today:
            current = 1
        longest = max(longest, current)

    query(
        """UPDATE user_gamification_streak
               SET current_streak=%s, longest_streak=%s, last_activity_date=%s, updated_at=now()
           WHERE subject_type=%s AND subject_id=%s""",
        [current, longest, today, subject_type, str(subject_id)],
        fetch=False,
    )
    return {"current": current, "longest": longest}


@router.get("/daily-question")
def daily_question(user: dict = Depends(get_current_user)):
    """Today's HR trivia question for the caller, plus streak + whether the
    daily-question card should be auto-paused for urgent requisitions."""
    subject_type, subject_id = _subject_for(user)
    tenant_id = user.get("tenant_id")
    today = date.today()

    streak_row = query_one(
        """SELECT current_streak, longest_streak FROM user_gamification_streak
           WHERE subject_type=%s AND subject_id=%s""",
        [subject_type, str(subject_id)],
    ) or {"current_streak": 0, "longest_streak": 0}
    streak = {"current": streak_row["current_streak"], "longest": streak_row["longest_streak"]}
    has_urgent = _has_urgent_requisitions(user)

    existing = query_one(
        """SELECT uqa.question_id, uqa.was_skipped, uqa.is_correct,
                  hq.question_text, hq.option_a, hq.option_b, hq.option_c,
                  hq.correct_option, hq.explanation_text
           FROM user_question_answer uqa
           JOIN hr_question hq ON hq.id = uqa.question_id
           WHERE uqa.subject_type=%s AND uqa.subject_id=%s AND uqa.answer_date=%s""",
        [subject_type, str(subject_id), today],
    )
    if existing:
        return {
            "question": {
                "id": str(existing["question_id"]),
                "question_text": existing["question_text"],
                "option_a": existing["option_a"], "option_b": existing["option_b"], "option_c": existing["option_c"],
            },
            "already_answered": True,
            "was_skipped": existing["was_skipped"],
            "is_correct": existing["is_correct"],
            "correct_option": None if existing["was_skipped"] else existing["correct_option"],
            "explanation_text": None if existing["was_skipped"] else existing["explanation_text"],
            "streak": streak,
            "hasUrgentReqs": has_urgent,
        }

    q = _pick_daily_question(tenant_id, subject_type, subject_id, today)
    if not q:
        return {"question": None, "already_answered": False, "streak": streak, "hasUrgentReqs": has_urgent}

    return {
        "question": {
            "id": str(q["id"]), "question_text": q["question_text"],
            "option_a": q["option_a"], "option_b": q["option_b"], "option_c": q["option_c"],
        },
        "already_answered": False,
        "was_skipped": False,
        "is_correct": None,
        "correct_option": None,
        "explanation_text": None,
        "streak": streak,
        "hasUrgentReqs": has_urgent,
    }


class DailyAnswerIn(BaseModel):
    question_id: str
    selected_option: str


@router.post("/daily-question/answer")
def answer_daily_question(body: DailyAnswerIn, user: dict = Depends(get_current_user)):
    if body.selected_option not in ("a", "b", "c"):
        raise HTTPException(400, "selected_option must be 'a', 'b', or 'c'")

    subject_type, subject_id = _subject_for(user)
    tenant_id = user.get("tenant_id")
    today = date.today()

    if query_one(
        "SELECT id FROM user_question_answer WHERE subject_type=%s AND subject_id=%s AND answer_date=%s",
        [subject_type, str(subject_id), today],
    ):
        raise HTTPException(409, "Today's question has already been answered")

    todays_q = _pick_daily_question(tenant_id, subject_type, subject_id, today)
    if not todays_q or str(todays_q["id"]) != body.question_id:
        raise HTTPException(400, "question_id does not match today's question — refresh and try again")

    is_correct = body.selected_option == todays_q["correct_option"]
    points_awarded = 0.0
    if is_correct:
        result = gam_award(subject_type, subject_id, "daily_question_correct")
        if result:
            points_awarded = float(result["points_awarded"])

    query(
        """INSERT INTO user_question_answer
               (tenant_id, subject_type, subject_id, question_id, answer_date,
                selected_option, is_correct, was_skipped, points_awarded)
           VALUES (%s,%s,%s,%s,%s,%s,%s,false,%s)""",
        [tenant_id, subject_type, str(subject_id), todays_q["id"], today,
         body.selected_option, is_correct, points_awarded],
        fetch=False,
    )

    streak = _bump_streak(subject_type, subject_id, tenant_id, today, grow=True)

    return {
        "is_correct": is_correct,
        "correct_option": todays_q["correct_option"],
        "explanation_text": todays_q["explanation_text"],
        "points_awarded": points_awarded,
        "streak": streak,
    }


class DailySkipIn(BaseModel):
    question_id: Optional[str] = None


@router.post("/daily-question/skip")
def skip_daily_question(body: DailySkipIn, user: dict = Depends(get_current_user)):
    subject_type, subject_id = _subject_for(user)
    tenant_id = user.get("tenant_id")
    today = date.today()

    if query_one(
        "SELECT id FROM user_question_answer WHERE subject_type=%s AND subject_id=%s AND answer_date=%s",
        [subject_type, str(subject_id), today],
    ):
        raise HTTPException(409, "Today's question has already been answered or skipped")

    todays_q = _pick_daily_question(tenant_id, subject_type, subject_id, today)
    if not todays_q:
        raise HTTPException(404, "No active question to skip")
    if body.question_id and str(todays_q["id"]) != body.question_id:
        raise HTTPException(400, "question_id does not match today's question — refresh and try again")

    query(
        """INSERT INTO user_question_answer
               (tenant_id, subject_type, subject_id, question_id, answer_date, was_skipped)
           VALUES (%s,%s,%s,%s,%s,true)""",
        [tenant_id, subject_type, str(subject_id), todays_q["id"], today],
        fetch=False,
    )

    streak = _bump_streak(subject_type, subject_id, tenant_id, today, grow=False)
    return {"streak": streak}


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
