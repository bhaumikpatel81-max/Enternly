"""
LinkedIn profile refresh reminder -- background asyncio task (same pattern
as campus_email_worker.py: claim-then-send, retry with backoff, never
crashes the app). Sends every 6 months, indefinitely, to any candidate with
a linkedin_url on file who hasn't opted out -- there is deliberately no
auto-stop on hired/rejected/stale candidates; the unsubscribe link is the
only way this stops for a given candidate (per product decision).

Claim uses the same FOR UPDATE SKIP LOCKED + future-timestamp trick as
campus_email_worker._claim_batch: a crash mid-send can't cause a double
send, and a crash just lets the claim expire so the row becomes pickable
again.

Bounce/delivery visibility: an SMTP failure is retried a couple of times,
then logged via log_activity so it's at least visible in the admin activity
timeline -- the mailer has no bounce webhook, so this is the only trace a
dead candidate email leaves. See _mark_failure.
"""
import asyncio
import secrets

from ..db import query, query_one

_BATCH_SIZE        = 20
_SEND_DELAY        = 2.0     # seconds between individual sends within a batch
_BATCH_DELAY       = 45.0    # seconds pause after finishing a batch
_IDLE_SLEEP        = 1800.0  # 30 min -- this runs on a 6-month cadence, no need to poll often
_CLAIM_TTL_MINUTES = 30
_MAX_ATTEMPTS      = 3
_RETRY_BACKOFF_MIN = [30, 180]  # minutes before retrying attempt 2 and attempt 3


def _smtp_configured() -> bool:
    """Cheap idle-gate: true if ANY tenant (or the env fallback) has SMTP
    configured. Each row still sends through its own tenant's config."""
    import os
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        return True
    row = query_one(
        "SELECT 1 FROM system_settings WHERE key='smtp_user' AND COALESCE(value, '') <> '' LIMIT 1"
    )
    return bool(row)


def _claim_batch() -> list:
    rows = query(
        f"""WITH claimed AS (
               SELECT id FROM candidate
               WHERE linkedin_url IS NOT NULL
                 AND NOT linkedin_reminders_opt_out
                 AND (linkedin_reminder_sent_at IS NULL
                      OR linkedin_reminder_sent_at < now() - interval '6 months')
                 AND (linkedin_reminder_next_attempt_at IS NULL
                      OR linkedin_reminder_next_attempt_at <= now())
               ORDER BY COALESCE(linkedin_reminder_sent_at, 'epoch'::timestamptz) ASC
               LIMIT %s
               FOR UPDATE SKIP LOCKED
           )
           UPDATE candidate c
           SET linkedin_reminder_next_attempt_at = now() + interval '{_CLAIM_TTL_MINUTES} minutes'
           FROM claimed
           WHERE c.id = claimed.id
           RETURNING c.id""",
        [_BATCH_SIZE],
    )
    return [str(r["id"]) for r in (rows or [])]


def _fetch_batch(ids: list) -> list:
    if not ids:
        return []
    return query(
        """SELECT id, full_name, given_name, email, linkedin_url, tenant_id,
                  linkedin_connected_at, linkedin_last_synced_at,
                  linkedin_reminder_attempts, linkedin_unsub_token
           FROM candidate
           WHERE id = ANY(%s::uuid[])""",
        [ids],
    )


def _ensure_unsub_token(row: dict) -> str:
    if row.get("linkedin_unsub_token"):
        return row["linkedin_unsub_token"]
    token = secrets.token_urlsafe(24)
    query(
        "UPDATE candidate SET linkedin_unsub_token=%s WHERE id=%s AND linkedin_unsub_token IS NULL",
        [token, str(row["id"])], fetch=False,
    )
    return token


def _fmt_date(dt) -> str:
    if not dt:
        return "Not yet"
    try:
        return dt.strftime("%d %b %Y")
    except Exception:
        return str(dt)


def _build_email(row: dict, base_url: str, unsub_token: str) -> tuple:
    """Returns (subject, plain_text, html)."""
    import html as _html
    from .email_layout import build_branded_email

    first_name = row.get("given_name") or (row.get("full_name") or "").split(" ")[0] or "there"
    profile_url = f"{base_url}/candidate-portal?next=profile&section=basic-info"
    linkedin_url = f"{base_url}/candidate-portal?next=profile&section=linkedin"
    unsub_url = f"{base_url}/api/candidate/portal/linkedin/unsubscribe?token={unsub_token}"

    subject = f"Your LinkedIn profile is due for a refresh, {first_name}"
    plain = (
        f"Hi {first_name},\n\n"
        "It's been 6 months since we last synced your LinkedIn profile. "
        "Recruiters at EnternsTech use the details on your profile to match you with new openings.\n\n"
        f"Update LinkedIn: {linkedin_url}\n"
        f"Update Profile:  {profile_url}\n\n"
        f"Don't want these reminders? Unsubscribe: {unsub_url}\n\n"
        "— EnternsTech Talent Acquisition"
    )

    two_button_html = f"""
          <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:4px">
            <tr><td align="center">
              <table cellpadding="0" cellspacing="0" border="0"><tr>
                <td style="padding:0 6px">
                  <a href="{linkedin_url}" style="display:block;color:#ffffff;background-color:#1e63f2;padding:13px 22px;border-radius:6px;text-decoration:none;font-size:13.5px;font-weight:700;font-family:Arial,Helvetica,sans-serif">Update LinkedIn</a>
                </td>
                <td style="padding:0 6px">
                  <a href="{profile_url}" style="display:block;color:#1e63f2;background-color:#ffffff;border:2px solid #1e63f2;padding:11px 20px;border-radius:6px;text-decoration:none;font-size:13.5px;font-weight:700;font-family:Arial,Helvetica,sans-serif">Update Profile</a>
                </td>
              </tr></table>
            </td></tr>
            <tr><td align="center" style="padding-top:10px">
              <span style="font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#9aa3b8">"Update LinkedIn" reconnects via LinkedIn &#183; "Update Profile" edits your details directly</span>
            </td></tr>
          </table>
          <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f8faff;border-radius:12px;margin:22px 0 4px">
            <tr><td style="padding:16px;font-size:12.5px;line-height:1.6;color:#4b5563;font-family:Arial,Helvetica,sans-serif">
              <b style="color:#333d4c">Why we ask</b> &#8212; when you reconnect, EnternsTech only reads your name and photo from LinkedIn to verify it's really you. We never post on your behalf, don't store your LinkedIn password, and your clickable profile link is only ever what you type in yourself.
            </td></tr>
          </table>
          <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:18px">
            <tr><td align="center" style="font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#9aa3b8;padding-top:10px;border-top:1px dashed #e2e6f0">
              Getting this every 6 months and don't want to? <a href="{unsub_url}" style="color:#9aa3b8;text-decoration:underline">Manage your reminder preferences</a>
            </td></tr>
          </table>"""

    html = build_branded_email(
        eyebrow="Profile refresh &#183; every 6 months",
        hero_title_html="Let's keep your<br>profile updated.",
        hero_subtitle="It's been 6 months since we last synced your LinkedIn profile.",
        detail_cells=[
            ("LinkedIn on file", row["linkedin_url"]),
            ("Connected", _fmt_date(row.get("linkedin_connected_at"))),
            ("Last synced", _fmt_date(row.get("linkedin_last_synced_at"))),
            ("Next check-in", "In 6 months"),
        ],
        about_text=None,
        about_heading=None,
        extra_body_html=two_button_html,
        cta_label=None, cta_link=None,
    )
    return subject, plain, html


def _send_one(row: dict) -> None:
    """Blocking -- build + send one reminder. Raises on failure."""
    from .connectors import send_email, _load_email_cfg

    if not row.get("email"):
        raise RuntimeError("no_email_on_file")

    tenant_id = row.get("tenant_id")
    base_url = (_load_email_cfg(tenant_id).get("base_url") or "http://localhost:8000").strip().rstrip("/")
    unsub_token = _ensure_unsub_token(row)
    subject, plain, html_body = _build_email(row, base_url, unsub_token)
    send_email(row["email"], subject, plain, html=html_body, tenant_id=tenant_id)

    query(
        """UPDATE candidate
           SET linkedin_reminder_sent_at = now(),
               linkedin_reminder_next_attempt_at = NULL,
               linkedin_reminder_attempts = 0,
               linkedin_reminder_last_error = NULL
           WHERE id=%s""",
        [str(row["id"])], fetch=False,
    )


def _mark_failure(row: dict, exc: Exception) -> None:
    attempts = (row.get("linkedin_reminder_attempts") or 0) + 1
    if attempts >= _MAX_ATTEMPTS:
        # Don't hammer a dead address every _CLAIM_TTL_MINUTES forever --
        # push the next attempt out a full cycle and reset the counter, but
        # leave linkedin_reminder_sent_at untouched so this candidate is
        # still due (not silently marked "sent" when it never was).
        query(
            """UPDATE candidate
               SET linkedin_reminder_attempts = 0,
                   linkedin_reminder_last_error = %s,
                   linkedin_reminder_next_attempt_at = now() + interval '6 months'
               WHERE id=%s""",
            [str(exc)[:500], str(row["id"])], fetch=False,
        )
        print(f"[linkedin-reminder] {row.get('email')} permanently failed after {attempts} attempts: {exc}")
        try:
            from .activity_log import log_activity
            log_activity(
                "candidate", "linkedin_reminder_email_failed",
                entity_id=str(row["id"]), actor_id=None, actor_role="system",
                detail={"email": row.get("email"), "attempts": attempts, "error": str(exc)[:500]},
            )
        except Exception:
            pass
    else:
        backoff_min = _RETRY_BACKOFF_MIN[min(attempts - 1, len(_RETRY_BACKOFF_MIN) - 1)]
        query(
            """UPDATE candidate
               SET linkedin_reminder_attempts = %s,
                   linkedin_reminder_last_error = %s,
                   linkedin_reminder_next_attempt_at = now() + (%s || ' minutes')::interval
               WHERE id=%s""",
            [attempts, str(exc)[:500], backoff_min, str(row["id"])], fetch=False,
        )
        print(f"[linkedin-reminder] {row.get('email')} attempt {attempts} failed, retrying in {backoff_min}m: {exc}")


async def run_one_batch() -> str:
    """
    One claim-and-send cycle: up to _BATCH_SIZE due reminders. Returns
    "not_configured" | "idle" | "sent" so the fallback loop's sleep choice
    matches exactly what it did before this was extracted. Extracted so
    both start_linkedin_reminder_worker (Redis-less fallback mode) and the
    Arq queued job (worker.py, Redis mode) call the same logic -- the
    FOR UPDATE SKIP LOCKED claim in _claim_batch already makes this safe to
    invoke concurrently.
    """
    if not await asyncio.to_thread(_smtp_configured):
        return "not_configured"
    claimed_ids = await asyncio.to_thread(_claim_batch)
    if not claimed_ids:
        return "idle"
    batch = await asyncio.to_thread(_fetch_batch, claimed_ids)

    print(f"[linkedin-reminder] sending batch of {len(batch)}")
    for row in batch:
        try:
            await asyncio.to_thread(_send_one, row)
            print(f"[linkedin-reminder] sent to {row['email']}")
        except Exception as exc:
            await asyncio.to_thread(_mark_failure, row, exc)
        await asyncio.sleep(_SEND_DELAY)
    return "sent"


async def start_linkedin_reminder_worker():
    """Infinite background loop -- sends due 6-month LinkedIn refresh reminders."""
    print("[linkedin-reminder] background worker started")
    while True:
        try:
            outcome = await run_one_batch()
            if outcome in ("not_configured", "idle"):
                await asyncio.sleep(_IDLE_SLEEP)
            else:
                await asyncio.sleep(_BATCH_DELAY)

        except asyncio.CancelledError:
            print("[linkedin-reminder] task cancelled, shutting down")
            return
        except Exception as exc:
            print(f"[linkedin-reminder] unexpected error: {exc}")
            await asyncio.sleep(10)
