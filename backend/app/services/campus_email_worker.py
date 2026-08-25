"""
Campus bulk-invite email sender.

Background asyncio task started at app startup (same pattern as
cv_enricher.py / email_ingest.py). Picks up to 20 campus_candidate rows
with email_status='queued' at a time, sends the NexAI invite email to
each with a short delay between sends, then pauses before picking up the
next batch of 20 — this keeps a 1000+ row campus drive from firing all
its emails at once and getting the sending domain flagged/throttled.

Failed sends are retried with backoff (up to 3 attempts total) before
being marked 'failed'; campus_bulk_api.py's resend-queued endpoint resets
failed rows back to 'queued' so this loop picks them up again.

Never crashes the app — all exceptions are caught and logged.
"""
import asyncio

from ..db import query, query_one

_BATCH_SIZE           = 20     # max emails picked up per cycle
_SEND_DELAY           = 2.0    # seconds between individual sends within a batch
_BATCH_DELAY          = 45.0   # seconds pause after finishing a batch, before the next
_IDLE_SLEEP           = 20.0   # seconds to sleep when there's nothing queued
_NOT_CONFIGURED_SLEEP = 60.0   # seconds to sleep when SMTP isn't configured
_MAX_ATTEMPTS         = 3
_RETRY_BACKOFF_MIN    = [5, 15]  # minutes before retrying attempt 2 and attempt 3


def _smtp_configured() -> bool:
    """
    Cheap idle-vs-active gate for the loop -- true if ANY tenant (or the env
    var fallback) has SMTP configured. Each row is still sent through its own
    tenant's config in _send_one(); this only avoids spinning the claim/fetch
    loop hot when nothing at all is configured anywhere.
    """
    import os
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        return True
    row = query_one(
        "SELECT 1 FROM system_settings WHERE key='smtp_user' AND COALESCE(value, '') <> '' LIMIT 1"
    )
    return bool(row)


def _claim_batch() -> list:
    """
    Atomically claim up to _BATCH_SIZE queued rows by pushing their
    email_next_attempt_at into the near future, in a single UPDATE ... FROM
    (SELECT ... FOR UPDATE SKIP LOCKED) statement. This closes a race where a
    crash/restart between "email sent" and "status set to sent" would let
    the next fetch re-pick (and re-email) the same row: while claimed, the
    row falls outside _fetch_batch's own eligibility window, and if the
    process dies before resolving it, the claim simply expires and the row
    becomes pickable again — no permanently-stuck "in-flight" state.
    """
    rows = query(
        """WITH claimed AS (
               SELECT id FROM campus_candidate
               WHERE email_status = 'queued'
                 AND (email_next_attempt_at IS NULL OR email_next_attempt_at <= now())
               ORDER BY created_at ASC
               LIMIT %s
               FOR UPDATE SKIP LOCKED
           )
           UPDATE campus_candidate cc
           SET email_next_attempt_at = now() + interval '10 minutes'
           FROM claimed
           WHERE cc.id = claimed.id
           RETURNING cc.id""",
        [_BATCH_SIZE],
    )
    return [str(r["id"]) for r in (rows or [])]


def _fetch_batch(ids: list) -> list:
    if not ids:
        return []
    return query(
        """SELECT cc.id, cc.name, cc.email, cc.application_id, cc.email_attempts,
                  r.id AS req_id, r.title, r.tenant_id, gc.name AS company
           FROM campus_candidate cc
           JOIN requisition r ON r.id = cc.requisition_id
           JOIN business_unit bu ON bu.id = r.bu_id
           JOIN group_company gc ON gc.id = bu.company_id
           WHERE cc.id = ANY(%s::uuid[])
           ORDER BY cc.created_at ASC""",
        [ids],
    )


def _send_one(row: dict) -> None:
    """Blocking — build + send one invite email. Raises on failure."""
    from .connectors import send_email, resolve_global_placeholders as _rgp, _load_email_cfg
    from .email_templates import render_template as _render_email_tmpl
    from ..routers.nexai_api import _build_invite_html

    if not row["application_id"]:
        raise RuntimeError("no_application")

    invite_row = query_one(
        """SELECT token FROM nexai_invite
           WHERE application_id=%s AND used_at IS NULL AND expires_at > now()
           ORDER BY invited_at DESC LIMIT 1""",
        [str(row["application_id"])],
    )
    if not invite_row:
        raise RuntimeError("no_valid_token")

    tenant_id = row.get("tenant_id")
    base_url = (_load_email_cfg(tenant_id).get("base_url") or "http://localhost:8000").strip().rstrip("/")
    invite_url = f"{base_url}/nexai-interview?token={invite_row['token']}"

    globals_ = _rgp(req_id=str(row["req_id"]))
    reply_to = globals_.get("recruiter_email") or None

    subject, plain = _render_email_tmpl("nexai_invite", {
        "candidate_name": row["name"] or "Candidate",
        "job_title":      row["title"],
        "company_name":   row["company"],
        "interview_link": invite_url,
    }, req_id=str(row["req_id"]))

    html_body = _build_invite_html(
        name=row["name"] or "Candidate",
        job=row["title"],
        company=row["company"],
        invite_url=invite_url,
    )
    send_email(row["email"], subject, plain, html=html_body, reply_to=reply_to, tenant_id=tenant_id)

    query(
        """UPDATE campus_candidate
           SET invite_status='invited', email_status='sent',
               email_error=NULL, nexai_session_id=%s, invite_sent_at=now()
           WHERE id=%s""",
        [invite_row["token"], str(row["id"])],
        fetch=False,
    )
    query(
        """UPDATE campus_upload_batch SET invited_count = invited_count + 1
           WHERE id = (SELECT batch_id FROM campus_candidate WHERE id=%s)""",
        [str(row["id"])],
        fetch=False,
    )


def _mark_failure(row: dict, exc: Exception) -> None:
    attempts = (row.get("email_attempts") or 0) + 1
    if attempts >= _MAX_ATTEMPTS:
        query(
            """UPDATE campus_candidate
               SET email_status='failed', email_attempts=%s,
                   email_error=%s, email_next_attempt_at=NULL
               WHERE id=%s""",
            [attempts, str(exc)[:500], str(row["id"])],
            fetch=False,
        )
        print(f"[campus-email] {row['email']} permanently failed after {attempts} attempts: {exc}")
        try:
            from .activity_log import log_activity
            log_activity(
                "campus_candidate", "invite_email_permanently_failed",
                entity_id=str(row["id"]), actor_id=None, actor_role="system",
                detail={"email": row["email"], "attempts": attempts, "error": str(exc)[:500]},
            )
        except Exception:
            pass
    else:
        backoff_min = _RETRY_BACKOFF_MIN[min(attempts - 1, len(_RETRY_BACKOFF_MIN) - 1)]
        query(
            """UPDATE campus_candidate
               SET email_attempts=%s, email_error=%s,
                   email_next_attempt_at = now() + (%s || ' minutes')::interval
               WHERE id=%s""",
            [attempts, str(exc)[:500], backoff_min, str(row["id"])],
            fetch=False,
        )
        print(f"[campus-email] {row['email']} attempt {attempts} failed, retrying in {backoff_min}m: {exc}")


async def start_campus_email_worker():
    """Infinite background loop — sends queued campus invite emails in throttled batches of 20."""
    print("[campus-email] background worker started")
    while True:
        try:
            if not await asyncio.to_thread(_smtp_configured):
                await asyncio.sleep(_NOT_CONFIGURED_SLEEP)
                continue

            claimed_ids = await asyncio.to_thread(_claim_batch)
            if not claimed_ids:
                await asyncio.sleep(_IDLE_SLEEP)
                continue
            batch = await asyncio.to_thread(_fetch_batch, claimed_ids)

            print(f"[campus-email] sending batch of {len(batch)}")
            for row in batch:
                try:
                    await asyncio.to_thread(_send_one, row)
                    print(f"[campus-email] sent to {row['email']}")
                except Exception as exc:
                    await asyncio.to_thread(_mark_failure, row, exc)
                await asyncio.sleep(_SEND_DELAY)

            await asyncio.sleep(_BATCH_DELAY)

        except asyncio.CancelledError:
            print("[campus-email] task cancelled, shutting down")
            return
        except Exception as exc:
            print(f"[campus-email] unexpected error: {exc}")
            await asyncio.sleep(10)
