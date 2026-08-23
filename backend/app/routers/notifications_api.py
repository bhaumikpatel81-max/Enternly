"""
Notification center -- bell/dropdown + Action Queue backing store.

Every endpoint is scoped to recipient_user_id = current user; there is no
role check because "own notifications" is itself the authorization rule.
Any id not owned by the caller 404s (never 403 -- matches the project-wide
convention of not confirming a resource's existence to a non-owner).
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import query, query_one
from ..auth_utils import get_current_user

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
def list_notifications(
    unread_only: bool = Query(False),
    limit: int = Query(30, le=100),
    user: dict = Depends(get_current_user),
):
    where = "WHERE recipient_user_id = %s"
    params = [user["sub"]]
    if unread_only:
        where += " AND is_read = false"
    return query(
        f"""SELECT id, type, title, body, action_url, is_actionable, is_read,
                   requisition_id, application_id, interview_request_id,
                   created_at, read_at
            FROM notification
            {where}
            ORDER BY created_at DESC
            LIMIT %s""",
        params + [limit],
    )


@router.get("/unread-count")
def unread_count(user: dict = Depends(get_current_user)):
    row = query_one(
        "SELECT COUNT(*) AS n FROM notification WHERE recipient_user_id = %s AND is_read = false",
        [user["sub"]],
    )
    return {"count": int(row["n"]) if row else 0}


@router.post("/read-all")
def mark_all_read(user: dict = Depends(get_current_user)):
    query(
        "UPDATE notification SET is_read = true, read_at = now() WHERE recipient_user_id = %s AND is_read = false",
        [user["sub"]], fetch=False,
    )
    return {"ok": True}


@router.post("/{notif_id}/read")
def mark_read(notif_id: str, user: dict = Depends(get_current_user)):
    row = query_one(
        "SELECT id FROM notification WHERE id = %s AND recipient_user_id = %s",
        [notif_id, user["sub"]],
    )
    if not row:
        raise HTTPException(404, "Notification not found")
    query(
        "UPDATE notification SET is_read = true, read_at = now() WHERE id = %s",
        [notif_id], fetch=False,
    )
    return {"ok": True}
