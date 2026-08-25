"""
Gamification service — append-only ledger of points events.

Mirrors the simplicity of pipeline.log_event: one function writes one row.
Scores, tiers, and badges are DERIVED from the ledger — never stored as mutable totals.

award(subject_type, subject_id, event_type, requisition_id, application_id)
  → looks up base_points + criticality multiplier from gamification_config
  → writes ONE ledger row
  → checks and grants any newly-earned badges

score_for(subject_type, subject_id, period)  → total points for a period
tier_for(points)                             → tier name
check_and_grant_badges(subject_type, subject_id)  → grant any newly-earned badges
"""
from ..db import query, query_one


def _load_config(tenant_id: str = None) -> dict:
    """gamification_config is tenant-scoped (Migration 96) -- without a
    tenant_id filter, rows from every tenant would merge into one dict with
    undefined last-write-wins behaviour per key once a second tenant exists."""
    if tenant_id:
        rows = query("SELECT key, value FROM gamification_config WHERE tenant_id = %s", [tenant_id])
    else:
        rows = query("SELECT key, value FROM gamification_config")
    return {r["key"]: r["value"] for r in rows} if rows else {}


def award(
    subject_type: str,
    subject_id: str,
    event_type: str,
    requisition_id: str | None = None,
    application_id: str | None = None,
) -> dict | None:
    """
    Write one gamification ledger row for the given event.
    Returns the ledger row dict, or None if config is missing.
    Does NOT raise — gamification must never break the calling flow.
    """
    try:
        # Resolve the tenant this event belongs to -- every real call site
        # passes requisition_id, which is the authoritative signal (same
        # source already used for criticality below). Falls back to the
        # acting subject's own account when no requisition is given, so a
        # future award() with no requisition still lands in the right
        # tenant rather than silently defaulting to the seed tenant.
        tenant_id = None
        req = None
        if requisition_id:
            req = query_one(
                "SELECT criticality, tenant_id FROM requisition WHERE id=%s", [requisition_id]
            )
            if req:
                tenant_id = req.get("tenant_id")
        if not tenant_id:
            subject_table = {
                "recruiter": "app_user", "hm": "app_user",
                "vendor": "vendor_user", "candidate": "candidate_user",
            }.get(subject_type)
            if subject_table:
                srow = query_one(f"SELECT tenant_id FROM {subject_table} WHERE id=%s", [str(subject_id)])
                tenant_id = (srow or {}).get("tenant_id")
        if not tenant_id:
            # Same seed tenant every tenant-owned table defaults to (Migration
            # 94-96) -- gamification_event.tenant_id is NOT NULL, so this is
            # what an omitted column would resolve to anyway; inlined so the
            # INSERT below can stay one fixed-shape statement.
            tenant_id = "00000000-0000-0000-0000-000000000001"

        cfg = _load_config(tenant_id)

        base_points_str = cfg.get(f"points.{event_type}")
        if base_points_str is None:
            return None
        base_points = float(base_points_str)

        criticality = (req or {}).get("criticality") or "Medium"

        multiplier = float(cfg.get(f"multiplier.{criticality}", "1.0"))
        points_awarded = round(base_points * multiplier, 2)

        row = query_one(
            """INSERT INTO gamification_event
                   (subject_type, subject_id, event_type,
                    base_points, criticality, multiplier, points_awarded,
                    requisition_id, application_id, tenant_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id, points_awarded""",
            [
                subject_type, str(subject_id), event_type,
                base_points, criticality, multiplier, points_awarded,
                requisition_id, application_id, tenant_id,
            ],
        )

        check_and_grant_badges(subject_type, str(subject_id), tenant_id)
        return dict(row) if row else None

    except Exception as exc:
        print(f"[gamification] award failed ({event_type}/{subject_type}/{subject_id}): {exc}")
        try:
            from .activity_log import log_activity
            log_activity(
                "gamification", "award_failed",
                entity_id=str(subject_id), requisition_id=requisition_id, application_id=application_id,
                actor_id=None, actor_role="system",
                detail={"event_type": event_type, "subject_type": subject_type, "error": str(exc)},
            )
        except Exception:
            pass
        return None


def score_for(
    subject_type: str,
    subject_id: str,
    period: str = "all",
) -> float:
    """
    Total points earned by a subject for a given period.
    period: 'month' | 'quarter' | 'ytd' | 'all'
    """
    period_filter = {
        "month":   "AND created_at >= date_trunc('month', now())",
        "quarter": "AND created_at >= date_trunc('quarter', now())",
        "ytd":     "AND created_at >= date_trunc('year', now())",
        "all":     "",
    }.get(period, "")

    row = query_one(
        f"""SELECT COALESCE(SUM(points_awarded), 0) AS total
            FROM gamification_event
            WHERE subject_type=%s AND subject_id=%s {period_filter}""",
        [subject_type, str(subject_id)],
    )
    return float(row["total"]) if row else 0.0


def rank_for(subject_type: str, subject_id: str, period: str = "all", tenant_id: str = None) -> int:
    """Return the rank of this subject among all subjects of the same type
    (within the same tenant, once tenant_id is passed -- otherwise this
    would rank a subject against every other tenant's subjects too)."""
    period_filter = {
        "month":   "AND created_at >= date_trunc('month', now())",
        "quarter": "AND created_at >= date_trunc('quarter', now())",
        "ytd":     "AND created_at >= date_trunc('year', now())",
        "all":     "",
    }.get(period, "")

    tenant_filter = "AND tenant_id = %s" if tenant_id else ""
    params = [subject_type] + ([tenant_id] if tenant_id else [])
    rows = query(
        f"""SELECT subject_id, SUM(points_awarded) AS total
            FROM gamification_event
            WHERE subject_type=%s {tenant_filter} {period_filter}
            GROUP BY subject_id
            ORDER BY total DESC""",
        params,
    )
    for i, r in enumerate(rows or [], start=1):
        if str(r["subject_id"]) == str(subject_id):
            return i
    return len(rows or []) + 1


def tier_for(points: float, tenant_id: str = None) -> str:
    """Derive tier name from cumulative points using gamification_config thresholds."""
    try:
        cfg = _load_config(tenant_id)
        thresholds = {
            "platinum": float(cfg.get("tier.platinum", "1500")),
            "gold":     float(cfg.get("tier.gold",     "600")),
            "silver":   float(cfg.get("tier.silver",   "200")),
            "bronze":   float(cfg.get("tier.bronze",   "0")),
        }
        if points >= thresholds["platinum"]:
            return "platinum"
        if points >= thresholds["gold"]:
            return "gold"
        if points >= thresholds["silver"]:
            return "silver"
        return "bronze"
    except Exception:
        return "bronze"


def next_tier_info(points: float, tenant_id: str = None) -> dict:
    """Return the next tier name and points needed to reach it."""
    try:
        cfg = _load_config(tenant_id)
        tiers = [
            ("silver",   float(cfg.get("tier.silver",   "200"))),
            ("gold",     float(cfg.get("tier.gold",     "600"))),
            ("platinum", float(cfg.get("tier.platinum", "1500"))),
        ]
        for name, threshold in tiers:
            if points < threshold:
                return {"next_tier": name, "points_needed": round(threshold - points, 2)}
        return {"next_tier": None, "points_needed": 0}
    except Exception:
        return {"next_tier": None, "points_needed": 0}


# Badge definitions — key → human name + rule
_BADGES = {
    "critical_closer":  {"name": "Critical Closer",  "desc": "Filled a Critical requisition"},
    "speed_demon":      {"name": "Speed Demon",       "desc": "10 SLA-beating screen completions"},
    "quality_streak":   {"name": "Quality Streak",    "desc": "5 consecutive offer acceptances"},
    "first_hire":       {"name": "First Hire",        "desc": "First candidate hired"},
    "super_sourcer":    {"name": "Super Sourcer",     "desc": "50 submissions made"},
}


def badge_meta(badge_key: str) -> dict:
    """Human-readable name/desc for a badge key."""
    return _BADGES.get(badge_key, {"name": badge_key, "desc": ""})


def check_and_grant_badges(subject_type: str, subject_id: str, tenant_id: str = None):
    """Check all badge conditions and grant any not yet earned. Every COUNT
    query below is already scoped to one specific subject_id (a globally
    unique account id, never shared across tenants), so they don't need a
    tenant filter themselves -- only the INSERT needs tenant_id stamped,
    same as award()'s gamification_event insert."""
    try:
        existing_keys = {
            r["badge_key"]
            for r in (query(
                "SELECT badge_key FROM gamification_badge WHERE subject_type=%s AND subject_id=%s",
                [subject_type, str(subject_id)],
            ) or [])
        }

        def grant(badge_key: str):
            if badge_key not in existing_keys:
                if tenant_id:
                    query(
                        """INSERT INTO gamification_badge (subject_type, subject_id, badge_key, tenant_id)
                           VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                        [subject_type, str(subject_id), badge_key, tenant_id],
                        fetch=False,
                    )
                else:
                    # No tenant resolved -- omit the column so the DB default
                    # (the seed tenant) applies, rather than inserting a
                    # literal NULL into a NOT NULL column.
                    query(
                        """INSERT INTO gamification_badge (subject_type, subject_id, badge_key)
                           VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                        [subject_type, str(subject_id), badge_key],
                        fetch=False,
                    )

        # First hire
        hire_count = query_one(
            """SELECT COUNT(*) AS n FROM gamification_event
               WHERE subject_type=%s AND subject_id=%s AND event_type='offer_joined'""",
            [subject_type, str(subject_id)],
        )
        if hire_count and hire_count["n"] >= 1:
            grant("first_hire")

        # Critical Closer: filled at least one Critical req
        critical_count = query_one(
            """SELECT COUNT(*) AS n FROM gamification_event
               WHERE subject_type=%s AND subject_id=%s
                 AND event_type IN ('offer_accepted','offer_joined')
                 AND criticality='Critical'""",
            [subject_type, str(subject_id)],
        )
        if critical_count and critical_count["n"] >= 1:
            grant("critical_closer")

        # Speed Demon: 10 fast_screen events
        speed_count = query_one(
            """SELECT COUNT(*) AS n FROM gamification_event
               WHERE subject_type=%s AND subject_id=%s AND event_type='fast_screen'""",
            [subject_type, str(subject_id)],
        )
        if speed_count and speed_count["n"] >= 10:
            grant("speed_demon")

        # Quality Streak: 5 offer_accepted events
        qa_count = query_one(
            """SELECT COUNT(*) AS n FROM gamification_event
               WHERE subject_type=%s AND subject_id=%s AND event_type='offer_accepted'""",
            [subject_type, str(subject_id)],
        )
        if qa_count and qa_count["n"] >= 5:
            grant("quality_streak")

        # Super Sourcer: 50 submission events
        sub_count = query_one(
            """SELECT COUNT(*) AS n FROM gamification_event
               WHERE subject_type=%s AND subject_id=%s AND event_type='submission'""",
            [subject_type, str(subject_id)],
        )
        if sub_count and sub_count["n"] >= 50:
            grant("super_sourcer")

    except Exception as exc:
        print(f"[gamification] badge check failed: {exc}")


def get_profile(
    subject_type: str,
    subject_id: str,
    period: str = "all",
    tenant_id: str = None,
) -> dict:
    """Full gamification profile for a subject: points, tier, badges, rank."""
    points = score_for(subject_type, subject_id, period)
    badges_rows = query(
        "SELECT badge_key, earned_at FROM gamification_badge WHERE subject_type=%s AND subject_id=%s",
        [subject_type, str(subject_id)],
    ) or []
    badges = [
        {
            "key":       r["badge_key"],
            "name":      _BADGES.get(r["badge_key"], {}).get("name", r["badge_key"]),
            "desc":      _BADGES.get(r["badge_key"], {}).get("desc", ""),
            "earned_at": r["earned_at"],
        }
        for r in badges_rows
    ]
    return {
        "subject_type":  subject_type,
        "subject_id":    str(subject_id),
        "points":        points,
        "tier":          tier_for(points, tenant_id),
        "badges":        badges,
        "rank":          rank_for(subject_type, subject_id, period, tenant_id),
        "period":        period,
        **next_tier_info(points, tenant_id),
    }
