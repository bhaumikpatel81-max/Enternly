"""
Recipient-scoped, best-effort notification writes for the bell/Action Queue.

Deliberately separate from activity_log (services/activity_log.py) -- that
table is audit-only, never read live, and has no recipient/read-state
concept. This one is read live by GET /api/notifications* and by
hm_api.hm_dashboard()'s action_queue.

Two entry points, matching activity_log's split:
- notify(): autocommitting, try/except-swallow (mirrors log_activity /
  auth.py's login_log idiom) -- use when there's no already-open transaction.
- notify_tx(): executes on an existing transaction cursor via tx_exec() so
  the notification commits atomically with the row that triggered it -- use
  inside a `with transaction() as cur:` block.
"""
from ..db import query, tx_exec


def notify(
    recipient_user_id,
    type: str,
    title: str,
    *,
    body: str | None = None,
    action_url: str | None = None,
    is_actionable: bool = False,
    requisition_id=None,
    application_id=None,
    interview_request_id=None,
) -> None:
    if not recipient_user_id:
        return
    try:
        query(
            """INSERT INTO notification
                 (recipient_user_id, type, title, body, action_url, is_actionable,
                  requisition_id, application_id, interview_request_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [
                recipient_user_id, type, title, body, action_url, is_actionable,
                requisition_id, application_id, interview_request_id,
            ],
            fetch=False,
        )
    except Exception as exc:
        print(f"[notifications] failed to write {type} for {recipient_user_id}: {exc}")


def notify_tx(
    cur,
    recipient_user_id,
    type: str,
    title: str,
    *,
    body: str | None = None,
    action_url: str | None = None,
    is_actionable: bool = False,
    requisition_id=None,
    application_id=None,
    interview_request_id=None,
) -> None:
    if not recipient_user_id:
        return
    tx_exec(
        cur,
        """INSERT INTO notification
             (recipient_user_id, type, title, body, action_url, is_actionable,
              requisition_id, application_id, interview_request_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        [
            recipient_user_id, type, title, body, action_url, is_actionable,
            requisition_id, application_id, interview_request_id,
        ],
    )
