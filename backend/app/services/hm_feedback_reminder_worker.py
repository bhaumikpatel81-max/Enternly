"""
Hiring-manager feedback reminder -- background asyncio task (same
claim-then-send + backoff shape as linkedin_reminder_worker.py). Once an
application's current-round interview time has passed and the HM still
hasn't left hm_feedback, this emails the owning hiring manager every
_RESEND_INTERVAL, indefinitely, until they submit feedback (application.
hm_feedback IS NOT NULL) -- there is no cap on reminder count by design,
same "keep nudging until the action happens" product shape as the LinkedIn
reminder.

Because hm_feedback_reminder_sent_at starts out NULL for every application
that predates this worker, the very first pass naturally sweeps up the
whole backlog of already-overdue reviews (oldest interview first via the
ORDER BY) -- no separate backfill step needed.

Claim uses the same FOR UPDATE SKIP LOCKED + future-timestamp trick as
linkedin_reminder_worker._claim_batch: a crash mid-send can't cause a
double send, and a crash just lets the claim expire so the row becomes
pickable again.
"""
import asyncio

from ..db import query, query_one

_BATCH_SIZE          = 30
_SEND_DELAY          = 2.0     # seconds between individual sends within a batch
_BATCH_DELAY         = 30.0    # seconds pause after finishing a batch
_IDLE_SLEEP          = 1800.0  # 30 min poll when nothing is due
_CLAIM_TTL_MINUTES   = 20
_RESEND_INTERVAL     = "2 days"   # cadence between reminders for the same application
_INTERVIEW_BUFFER_MIN = 60        # assume 60 min if duration_min is missing
_MAX_ATTEMPTS        = 3
_RETRY_BACKOFF_MIN   = [30, 180]  # minutes before retrying attempt 2 and attempt 3


def _smtp_configured() -> bool:
    from .connectors import _load_email_cfg
    cfg = _load_email_cfg()
    return bool(cfg["user"] and cfg["password"])


def _claim_batch() -> list:
    rows = query(
        f"""WITH candidates_due AS (
               SELECT a.id
               FROM application a
               JOIN requisition r ON r.id = a.requisition_id
               JOIN LATERAL (
                   SELECT i.scheduled_at, i.duration_min
                   FROM interview i
                   JOIN round_config rc ON rc.id = i.round_config_id
                   WHERE i.application_id = a.id AND rc.sequence = a.current_round
                   ORDER BY i.created_at DESC LIMIT 1
               ) iv ON true
               WHERE a.status = 'interview'
                 AND (a.hm_feedback IS NULL OR a.hm_feedback = '')
                 AND r.hiring_manager_id IS NOT NULL
                 AND iv.scheduled_at + (COALESCE(iv.duration_min, {_INTERVIEW_BUFFER_MIN}) || ' minutes')::interval < now()
                 AND (a.hm_feedback_reminder_sent_at IS NULL
                      OR a.hm_feedback_reminder_sent_at < now() - interval '{_RESEND_INTERVAL}')
                 AND (a.hm_feedback_reminder_next_attempt_at IS NULL
                      OR a.hm_feedback_reminder_next_attempt_at <= now())
               ORDER BY COALESCE(a.hm_feedback_reminder_sent_at, iv.scheduled_at) ASC
               LIMIT %s
               FOR UPDATE OF a SKIP LOCKED
           )
           UPDATE application ap
           SET hm_feedback_reminder_next_attempt_at = now() + interval '{_CLAIM_TTL_MINUTES} minutes'
           FROM candidates_due
           WHERE ap.id = candidates_due.id
           RETURNING ap.id""",
        [_BATCH_SIZE],
    )
    return [str(r["id"]) for r in (rows or [])]


def _fetch_batch(ids: list) -> list:
    if not ids:
        return []
    return query(
        """SELECT a.id AS app_id, a.hm_feedback_reminder_count, a.hm_feedback_reminder_attempts,
                  c.full_name AS candidate_name,
                  r.id AS req_id, r.title AS req_title, r.req_code,
                  hm.id AS hm_id, hm.full_name AS hm_name, hm.email AS hm_email,
                  iv.scheduled_at AS interview_scheduled_at, rc.name AS round_name
           FROM application a
           JOIN candidate c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           JOIN app_user hm ON hm.id = r.hiring_manager_id
           JOIN LATERAL (
               SELECT i.scheduled_at, i.round_config_id
               FROM interview i
               JOIN round_config rc2 ON rc2.id = i.round_config_id
               WHERE i.application_id = a.id AND rc2.sequence = a.current_round
               ORDER BY i.created_at DESC LIMIT 1
           ) iv ON true
           LEFT JOIN round_config rc ON rc.id = iv.round_config_id
           WHERE a.id = ANY(%s::uuid[])""",
        [ids],
    )


def _fmt_ist(dt) -> str:
    if not dt:
        return "—"
    try:
        from . import connectors
        return connectors.to_ist(dt).strftime("%A, %d %B %Y at %I:%M %p IST")
    except Exception:
        return str(dt)


def _build_email(row: dict, base_url: str) -> tuple:
    """Returns (subject, plain_text, html)."""
    from .email_layout import build_branded_email

    hm_first = (row.get("hm_name") or "there").split(" ")[0]
    candidate_name = row.get("candidate_name") or "the candidate"
    req_title = row.get("req_title") or "the role"
    round_name = row.get("round_name") or "Interview"
    review_url = f"{base_url}/#profiles"
    reminder_count = (row.get("hm_feedback_reminder_count") or 0) + 1

    subject = f"Reminder: feedback still pending for {candidate_name} — {req_title}"
    plain = (
        f"Hi {hm_first},\n\n"
        f"{candidate_name}'s {round_name} for {req_title} has been completed, "
        "but your feedback hasn't been submitted yet. The recruiting team is "
        "waiting on your review to move this candidate forward.\n\n"
        f"Give feedback now: {review_url}\n\n"
        "You'll keep getting this reminder every couple of days until feedback is submitted.\n\n"
        "— EnternsTech Talent Acquisition"
    )

    html_body = build_branded_email(
        eyebrow="Action needed · Interview feedback",
        hero_title_html="Your feedback is<br>still pending.",
        hero_subtitle=f"{candidate_name} is waiting on your review to move forward.",
        detail_cells=[
            ("Candidate", candidate_name),
            ("Requisition", f"{row.get('req_code') or ''} {req_title}".strip()),
            ("Round", round_name),
            ("Interviewed On", _fmt_ist(row.get("interview_scheduled_at"))),
        ],
        about_text=(
            "The recruiting team can't move this candidate to the next stage "
            "until your feedback is on file — a quick Approve/Not approved "
            "plus a short comment is all that's needed."
        ),
        about_heading="Why you're getting this",
        cta_label="Give Feedback Now",
        cta_link=review_url,
        footer_note=(
            f"This is reminder #{reminder_count} — it'll keep repeating every "
            "couple of days until feedback is submitted. Questions? Simply "
            "reply to this email."
        ),
    )
    return subject, plain, html_body


def _send_one(row: dict) -> None:
    """Blocking -- build + send one reminder, then mark it sent. Raises on failure."""
    from .connectors import send_email, _load_email_cfg
    from .notifications import notify

    if not row.get("hm_email"):
        raise RuntimeError("no_email_on_file")

    base_url = (_load_email_cfg().get("base_url") or "http://localhost:8000").strip().rstrip("/")
    subject, plain, html_body = _build_email(row, base_url)
    send_email(row["hm_email"], subject, plain, html=html_body)

    query(
        """UPDATE application
           SET hm_feedback_reminder_sent_at = now(),
               hm_feedback_reminder_count = hm_feedback_reminder_count + 1,
               hm_feedback_reminder_next_attempt_at = NULL,
               hm_feedback_reminder_attempts = 0,
               hm_feedback_reminder_last_error = NULL
           WHERE id=%s""",
        [row["app_id"]], fetch=False,
    )

    notify(
        row["hm_id"], "hm_feedback_reminder",
        f"Feedback pending for {row.get('candidate_name')}",
        body=f"{row.get('req_title')} — {row.get('round_name') or 'interview'} feedback is still needed.",
        action_url="/#profiles", is_actionable=True,
        requisition_id=row.get("req_id"), application_id=row.get("app_id"),
    )


def _fetch_ready_context(interview_id: str):
    return query_one(
        """SELECT a.id AS app_id, a.hm_feedback,
                  c.full_name AS candidate_name,
                  r.id AS req_id, r.title AS req_title, r.req_code, r.hiring_manager_id,
                  hm.id AS hm_id, hm.full_name AS hm_name, hm.email AS hm_email,
                  i.scheduled_at AS interview_scheduled_at, rc.name AS round_name
           FROM interview i
           JOIN application a  ON a.id = i.application_id
           JOIN candidate   c  ON c.id = a.candidate_id
           JOIN requisition r  ON r.id = a.requisition_id
           JOIN round_config rc ON rc.id = i.round_config_id
           LEFT JOIN app_user hm ON hm.id = r.hiring_manager_id
           WHERE i.id = %s""",
        [interview_id],
    )


def send_feedback_ready_email(interview_id: str) -> None:
    """
    Fired once, right when the LAST expected panelist on a round submits
    their scorecard (see scorecard_api.save_scorecard's panel-completeness
    check) -- an immediate "feedback is ready" ping to the hiring manager,
    distinct from and well ahead of the periodic "still pending" nag
    start_hm_feedback_reminder_worker sends starting ~1h after the
    interview if the HM still hasn't recorded a decision. Every panelist
    can only submit once (scorecard locks on submit), so "the panel just
    became fully submitted" is itself a one-time transition -- no
    duplicate-send guard needed here.

    Best-effort: any failure here must never fail the scorecard save.
    """
    try:
        row = _fetch_ready_context(interview_id)
        if not row or not row.get("hiring_manager_id") or not row.get("hm_email"):
            return
        if row.get("hm_feedback"):
            return  # HM already recorded a decision -- nothing to ping

        from .connectors import send_email, _load_email_cfg
        from .notifications import notify
        from .email_layout import build_branded_email

        base_url = (_load_email_cfg().get("base_url") or "http://localhost:8000").strip().rstrip("/")
        review_url = f"{base_url}/#profiles"
        hm_first = (row.get("hm_name") or "there").split(" ")[0]
        candidate_name = row.get("candidate_name") or "the candidate"
        req_title = row.get("req_title") or "the role"
        round_name = row.get("round_name") or "Interview"

        subject = f"Panel feedback ready for {candidate_name} — {req_title}"
        plain = (
            f"Hi {hm_first},\n\n"
            f"The panel has finished submitting feedback for {candidate_name}'s "
            f"{round_name} for {req_title}. It's ready for your review.\n\n"
            f"Review now: {review_url}\n\n"
            "— EnternsTech Talent Acquisition"
        )
        html_body = build_branded_email(
            eyebrow="Panel feedback ready",
            hero_title_html="Panel feedback is<br>ready for review.",
            hero_subtitle=f"Every panelist has submitted their feedback on {candidate_name}.",
            detail_cells=[
                ("Candidate", candidate_name),
                ("Requisition", f"{row.get('req_code') or ''} {req_title}".strip()),
                ("Round", round_name),
                ("Interviewed On", _fmt_ist(row.get("interview_scheduled_at"))),
            ],
            about_text=(
                "All panelists on this round have submitted their scorecards. "
                "Take a look and record your decision (Approve/Not approved) so "
                "the recruiting team can move this candidate forward."
            ),
            about_heading="Why you're getting this",
            cta_label="Review Feedback Now",
            cta_link=review_url,
        )
        send_email(row["hm_email"], subject, plain, html=html_body)

        notify(
            row["hm_id"], "hm_feedback_ready",
            f"Panel feedback ready for {candidate_name}",
            body=f"{req_title} — {round_name} feedback is ready for your review.",
            action_url="/#profiles", is_actionable=True,
            requisition_id=row.get("req_id"), application_id=row.get("app_id"),
        )
    except Exception as exc:
        print(f"[hm-feedback-ready] failed to notify for interview {interview_id}: {exc}")


def _mark_failure(row: dict, exc: Exception) -> None:
    attempts = (row.get("hm_feedback_reminder_attempts") or 0) + 1
    app_id = row["app_id"]
    if attempts >= _MAX_ATTEMPTS:
        # Don't hammer a broken mailbox every _CLAIM_TTL_MINUTES forever --
        # push the next attempt out a full resend cycle and reset the
        # counter, but leave hm_feedback_reminder_sent_at untouched so this
        # application is still considered "due" (not silently marked "sent"
        # when it never was).
        query(
            f"""UPDATE application
               SET hm_feedback_reminder_attempts = 0,
                   hm_feedback_reminder_last_error = %s,
                   hm_feedback_reminder_next_attempt_at = now() + interval '{_RESEND_INTERVAL}'
               WHERE id=%s""",
            [str(exc)[:500], app_id], fetch=False,
        )
        print(f"[hm-feedback-reminder] application {app_id} permanently failed after {attempts} attempts: {exc}")
        try:
            from .activity_log import log_activity
            log_activity(
                "application", "hm_feedback_reminder_email_failed",
                entity_id=app_id, actor_id=None, actor_role="system",
                detail={"attempts": attempts, "error": str(exc)[:500]},
            )
        except Exception:
            pass
    else:
        backoff_min = _RETRY_BACKOFF_MIN[min(attempts - 1, len(_RETRY_BACKOFF_MIN) - 1)]
        query(
            """UPDATE application
               SET hm_feedback_reminder_attempts = %s,
                   hm_feedback_reminder_last_error = %s,
                   hm_feedback_reminder_next_attempt_at = now() + (%s || ' minutes')::interval
               WHERE id=%s""",
            [attempts, str(exc)[:500], backoff_min, app_id], fetch=False,
        )
        print(f"[hm-feedback-reminder] application {app_id} attempt {attempts} failed, retrying in {backoff_min}m: {exc}")


async def start_hm_feedback_reminder_worker():
    """Infinite background loop -- sends due HM-feedback-pending reminders."""
    print("[hm-feedback-reminder] background worker started")
    while True:
        try:
            if not await asyncio.to_thread(_smtp_configured):
                await asyncio.sleep(_IDLE_SLEEP)
                continue

            claimed_ids = await asyncio.to_thread(_claim_batch)
            if not claimed_ids:
                await asyncio.sleep(_IDLE_SLEEP)
                continue
            batch = await asyncio.to_thread(_fetch_batch, claimed_ids)

            print(f"[hm-feedback-reminder] sending batch of {len(batch)}")
            for row in batch:
                try:
                    await asyncio.to_thread(_send_one, row)
                    print(f"[hm-feedback-reminder] sent to {row['hm_email']} re: application {row['app_id']}")
                except Exception as exc:
                    await asyncio.to_thread(_mark_failure, row, exc)
                await asyncio.sleep(_SEND_DELAY)

            await asyncio.sleep(_BATCH_DELAY)

        except asyncio.CancelledError:
            print("[hm-feedback-reminder] task cancelled, shutting down")
            return
        except Exception as exc:
            print(f"[hm-feedback-reminder] unexpected error: {exc}")
            await asyncio.sleep(10)
