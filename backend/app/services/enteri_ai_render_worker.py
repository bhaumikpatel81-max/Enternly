"""
Enteri AI avatar pre-render — durable retry backstop.

enteri_ai_api._do_single_invite() fires prerender_interview_videos() as a
FastAPI BackgroundTask: fast in the common case (video is usually ready
before the candidate opens their link), but if the worker process restarts
between the invite response being sent and that task actually executing,
the job is silently dropped -- render_status sits at its default 'pending'
forever, indistinguishable from "just created", with no retry and nothing
to point at.

This background asyncio task (same pattern as campus_email_worker.py) is a
periodic sweep, not a replacement for the fast path: it picks up any
enteri_ai_session stuck at render_status='pending' past a grace window (long
enough that a normal in-flight render isn't mistaken for a dropped one), or
'failed' with retries left, and calls the same prerender_interview_videos()
function directly. Sessions with SADTALKER_SERVICE_URL not configured at
all fail fast and immediately (see prerender.py) -- this loop checks that
once per cycle so it doesn't burn cycles re-claiming every pending session
when the GPU simply isn't deployed yet (orb takes over either way).

Never crashes the app -- all exceptions are caught and logged.
"""
import asyncio
import os

from ..db import query

_BATCH_SIZE           = 10    # sessions claimed per cycle
_GRACE_SECONDS        = 90    # how long a 'pending' session gets before being considered stuck
_RETRY_BACKOFF_MIN    = 10    # minutes to wait before re-claiming an already-attempted session
_IDLE_SLEEP           = 30.0  # seconds to sleep when nothing to claim
_NOT_CONFIGURED_SLEEP = 120.0  # seconds to sleep when no GPU is deployed
_MAX_ATTEMPTS         = 3


def _gpu_configured() -> bool:
    return bool(os.environ.get("SADTALKER_SERVICE_URL", "").strip())


def _claim_batch() -> list:
    """
    Atomically claim up to _BATCH_SIZE stuck/retryable sessions, same
    FOR UPDATE SKIP LOCKED + claim-timestamp pattern as
    campus_email_worker._claim_batch -- a crash mid-render just lets the
    claim expire and the row becomes pickable again. render_claimed_at
    doubles as both "already claimed" (never set on a fresh 'pending' row)
    and the retry-backoff clock for a 'failed' row that's been tried before.
    """
    rows = query(
        """WITH claimed AS (
               SELECT id FROM enteri_ai_session
               WHERE (
                   (render_status = 'pending'
                    AND created_at < now() - (%s || ' seconds')::interval)
                   OR
                   (render_status = 'failed' AND render_attempts < %s)
               )
               AND (render_claimed_at IS NULL
                    OR render_claimed_at < now() - (%s || ' minutes')::interval)
               ORDER BY created_at ASC
               LIMIT %s
               FOR UPDATE SKIP LOCKED
           )
           UPDATE enteri_ai_session ns
           SET render_claimed_at = now(), render_attempts = ns.render_attempts + 1
           FROM claimed
           WHERE ns.id = claimed.id
           RETURNING ns.id""",
        [_GRACE_SECONDS, _MAX_ATTEMPTS, _RETRY_BACKOFF_MIN, _BATCH_SIZE],
    )
    return [str(r["id"]) for r in (rows or [])]


async def run_one_batch() -> str:
    """
    One claim-and-retry cycle: up to _BATCH_SIZE stuck/failed sessions.
    Returns "not_configured" | "idle" | "retried" so the fallback loop's
    sleep choice matches exactly what it did before this was extracted.
    Extracted so both start_enteri_ai_render_worker (Redis-less fallback
    mode) and the Arq queued job (worker.py, Redis mode) call the same
    logic -- _claim_batch's FOR UPDATE SKIP LOCKED + render_claimed_at
    already makes this safe to invoke concurrently.
    """
    from .prerender import prerender_interview_videos

    if not await asyncio.to_thread(_gpu_configured):
        return "not_configured"
    claimed_ids = await asyncio.to_thread(_claim_batch)
    if not claimed_ids:
        return "idle"

    print(f"[enteri-ai-render] retrying {len(claimed_ids)} stuck/failed session(s)")
    for session_id in claimed_ids:
        try:
            await prerender_interview_videos(session_id)
        except Exception as exc:
            print(f"[enteri-ai-render] session={session_id} retry raised: {exc}")
    return "retried"


async def start_enteri_ai_render_worker():
    """Infinite background loop -- retries dropped/failed avatar pre-renders."""
    print("[enteri-ai-render] background worker started")
    while True:
        try:
            outcome = await run_one_batch()
            if outcome == "not_configured":
                await asyncio.sleep(_NOT_CONFIGURED_SLEEP)
            elif outcome == "idle":
                await asyncio.sleep(_IDLE_SLEEP)

        except asyncio.CancelledError:
            print("[enteri-ai-render] task cancelled, shutting down")
            return
        except Exception as exc:
            print(f"[enteri-ai-render] unexpected error: {exc}")
            await asyncio.sleep(10)
