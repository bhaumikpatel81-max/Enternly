"""Support tickets (all roles raise; admin resolves) + admin system-health."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user

router = APIRouter(prefix="/api", tags=["tickets"])


class TicketIn(BaseModel):
    category: str = "other"
    subject: str
    description: str = ""


class TicketUpdate(BaseModel):
    status: Optional[str] = None
    reply: Optional[str] = None


@router.post("/tickets")
def create_ticket(body: TicketIn, user: dict = Depends(get_current_user)):
    ticket = query_one(
        """INSERT INTO support_ticket (raised_by, category, subject, description)
           VALUES (%s, %s, %s, %s)
           RETURNING id, category, subject, status, created_at""",
        [user["sub"], body.category, body.subject, body.description],
    )
    return ticket


@router.get("/tickets")
def list_tickets(
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    if user["role"] == "admin":
        return query(
            """SELECT t.id, t.category, t.subject, t.description, t.status,
                      t.reply, t.created_at, t.resolved_at,
                      u.full_name  AS raised_by_name,
                      u.role       AS raised_by_role,
                      ru.full_name AS resolved_by_name
               FROM support_ticket t
               JOIN app_user u  ON u.id  = t.raised_by
               LEFT JOIN app_user ru ON ru.id = t.resolved_by
               ORDER BY
                 CASE t.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                 t.created_at DESC
               LIMIT %s OFFSET %s""",
            [limit, offset],
        )
    return query(
        """SELECT t.id, t.category, t.subject, t.description, t.status,
                  t.reply, t.created_at, t.resolved_at
           FROM support_ticket t
           WHERE t.raised_by = %s
           ORDER BY t.created_at DESC
           LIMIT %s OFFSET %s""",
        [user["sub"], limit, offset],
    )


@router.patch("/tickets/{ticket_id}")
def update_ticket(
    ticket_id: str, body: TicketUpdate, user: dict = Depends(get_current_user)
):
    if user["role"] != "admin":
        raise HTTPException(403, "Only TA Admin can update tickets")
    parts, params = [], []
    if body.status:
        parts.append("status = %s")
        params.append(body.status)
        if body.status == "resolved":
            parts.append("resolved_by = %s")
            params.append(user["sub"])
            parts.append("resolved_at = now()")
    if body.reply is not None:
        parts.append("reply = %s")
        params.append(body.reply)
    if not parts:
        raise HTTPException(400, "Nothing to update")
    parts.append("updated_at = now()")
    params.append(ticket_id)
    t = query_one(
        f"UPDATE support_ticket SET {', '.join(parts)} WHERE id = %s "
        "RETURNING id, status, reply",
        params,
    )
    if not t:
        raise HTTPException(404, "Ticket not found")
    return t


# ── Admin: system health ────────────────────────────────────────────────────

@router.get("/admin/system-health")
def system_health(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")

    # User counts by role
    user_counts = query(
        """SELECT role, COUNT(*) AS n
           FROM app_user WHERE is_active = true
           GROUP BY role ORDER BY role""",
        [],
    )

    # Ticket stats
    ticket_stats = query(
        "SELECT status, COUNT(*) AS n FROM support_ticket GROUP BY status",
        [],
    )
    ticket_recent = query(
        """SELECT t.id, t.category, t.subject, t.status, t.created_at,
                  u.full_name AS raised_by_name, u.role AS raised_by_role
           FROM support_ticket t
           JOIN app_user u ON u.id = t.raised_by
           ORDER BY t.created_at DESC LIMIT 10""",
        [],
    )

    # Pipeline snapshot
    snap = query_one(
        """SELECT
             (SELECT COUNT(*) FROM requisition  WHERE status='open') AS open_reqs,
             (SELECT COUNT(*) FROM application)                       AS total_apps,
             (SELECT COUNT(*) FROM application WHERE status='hired') AS total_joined,
             (SELECT COUNT(*) FROM candidate)                         AS total_candidates""",
        [],
    )

    # Recent login activity (last 50)
    recent_logins = query(
        """SELECT ll.logged_at, ll.user_role, u.full_name, u.email, ll.ip_address
           FROM login_log ll
           JOIN app_user u ON u.id = ll.user_id
           ORDER BY ll.logged_at DESC LIMIT 50""",
        [],
    )

    # Logins today vs yesterday
    login_trend = query(
        """SELECT
             COUNT(*) FILTER (WHERE logged_at >= CURRENT_DATE)                  AS today,
             COUNT(*) FILTER (WHERE logged_at >= CURRENT_DATE - INTERVAL '1 day'
                               AND  logged_at <  CURRENT_DATE)                  AS yesterday
           FROM login_log""",
        [],
    )

    return {
        "user_counts":   user_counts,
        "ticket_stats":  ticket_stats,
        "ticket_recent": ticket_recent,
        "pipeline":      dict(snap) if snap else {},
        "recent_logins": recent_logins,
        "login_trend":   dict(login_trend[0]) if login_trend else {"today": 0, "yesterday": 0},
    }
