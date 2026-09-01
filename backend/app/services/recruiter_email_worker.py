"""
Per-recruiter mailbox poller — background asyncio task (same pattern as
cv_enricher.py / campus_email_worker.py). Every recruiter can already set
their own Gmail address + App Password (Users & Access → gmail_app_password
on app_user, wired up for the on-demand "Scan My Email" button in
routers/cv_api.py). This loop is the automatic counterpart: it periodically
scans EVERY recruiter's inbox that has an App Password configured, so CVs
land in the CV Repository without anyone having to click the button.

Reuses services/cv_email_scan.py for the actual IMAP scan / per-attachment
resume classification / ingest — identical behaviour to a manual scan,
just run on a timer across every configured mailbox instead of one.

Never crashes the app — all exceptions are caught and logged, and a bad
App Password on one recruiter's mailbox doesn't stop the others from being
scanned.
"""
import asyncio
import os

from ..db import query
from .cv_email_scan import scan_gmail_inbox
from .cv_ingest import is_scan_paused

_POLL_SECONDS_ENV = "RECRUITER_EMAIL_POLL_SECONDS"
_DEFAULT_POLL_SECONDS = 300  # 5 minutes


def _set_status(status: str, detail: str = "") -> None:
    """Persists poller status to system_settings, same pattern as
    email_ingest.py's email_ingest_status — visible via a DB query even
    though there's no dedicated admin UI surfacing it yet."""
    try:
        query(
            """INSERT INTO system_status (key, value) VALUES ('recruiter_email_scan_status', %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            [f"{status}|{detail}"[:500]], fetch=False,
        )
    except Exception as exc:
        print(f"[recruiter-email] could not persist status: {exc}")


def _fetch_configured_recruiters() -> list:
    return query(
        """SELECT id, gmail_address, gmail_app_password, gmail_last_scan_at, tenant_id FROM app_user
           WHERE gmail_address IS NOT NULL AND gmail_address <> ''
             AND gmail_app_password IS NOT NULL AND gmail_app_password <> ''"""
    ) or []


def _update_checkpoint(user_id) -> None:
    query("UPDATE app_user SET gmail_last_scan_at = now() WHERE id = %s", [user_id], fetch=False)


def poll_interval_seconds() -> int:
    """Read once per call rather than cached, so worker.py's Arq cron
    schedule and the fallback loop always agree with the current env var."""
    return int(os.environ.get(_POLL_SECONDS_ENV, str(_DEFAULT_POLL_SECONDS)) or _DEFAULT_POLL_SECONDS)


async def run_one_pass() -> str:
    """
    One scan cycle across every recruiter mailbox with an App Password
    configured. Returns "paused" | "idle_no_recruiters" | "ok" | "error".
    Public entrypoint for the Arq queued job (worker.py) -- idempotency is
    via the per-mailbox gmail_last_scan_at checkpoint plus whatever
    dedup scan_gmail_inbox already does internally (same as a manual
    "Scan My Email" click); worker.py additionally wraps this in a job_lock
    so an overlapping tick skips instead of concurrently re-scanning the
    same mailboxes. Extracted out of start_recruiter_email_worker's
    while-loop body so both that loop (Redis-less fallback mode) and this
    function call the exact same logic.
    """
    if await asyncio.to_thread(is_scan_paused):
        # Master stop is active — this is the loop that kept running
        # after a per-job Stop click, since it has no job_id of its
        # own to cancel. Skip the cycle entirely until resumed.
        await asyncio.to_thread(_set_status, "paused_by_master_stop")
        return "paused"

    recruiters = await asyncio.to_thread(_fetch_configured_recruiters)
    if not recruiters:
        await asyncio.to_thread(_set_status, "idle_no_recruiters_configured")
        return "idle_no_recruiters"

    totals = {"processed": 0, "mapped": 0, "pooled": 0,
              "duplicates": 0, "skipped": 0, "errors": 0}
    failed_mailboxes = 0
    stopped_early = False
    for r in recruiters:
        if await asyncio.to_thread(is_scan_paused):
            stopped_early = True
            break
        try:
            res = await asyncio.to_thread(
                scan_gmail_inbox, r["gmail_address"], r["gmail_app_password"], str(r["id"]),
                r.get("gmail_last_scan_at"), is_scan_paused, r.get("tenant_id"),
            )
            for k in ("processed", "mapped", "pooled", "duplicates", "skipped"):
                totals[k] += res.get(k, 0)
            totals["errors"] += len(res.get("errors") or [])
            if not res.get("cancelled"):
                await asyncio.to_thread(_update_checkpoint, r["id"])
        except Exception as exc:
            failed_mailboxes += 1
            print(f"[recruiter-email] scan failed for {r['gmail_address']}: {exc}")

    detail = (
        f"{len(recruiters)} mailbox(es), {failed_mailboxes} failed, "
        f"{totals['processed']} ingested ({totals['mapped']} mapped/"
        f"{totals['pooled']} pooled), {totals['skipped']} skipped, "
        f"{totals['duplicates']} duplicates, {totals['errors']} errors"
        + (" — stopped early by master stop" if stopped_early else "")
    )
    print(f"[recruiter-email] cycle done: {detail}")
    await asyncio.to_thread(_set_status, "running", detail)
    return "ok"


async def start_recruiter_email_worker():
    """Infinite background loop — scans every configured recruiter mailbox
    on a fixed interval and ingests any resume-like attachment found."""
    interval = poll_interval_seconds()
    print(f"[recruiter-email] background worker starting (interval={interval}s)")

    while True:
        try:
            await run_one_pass()
        except asyncio.CancelledError:
            print("[recruiter-email] task cancelled, shutting down")
            return
        except Exception as exc:
            print(f"[recruiter-email] poll cycle error: {exc}")
            await asyncio.to_thread(_set_status, "poll_error", str(exc)[:500])

        await asyncio.sleep(poll_interval_seconds())
