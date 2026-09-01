"""
Arq worker process -- the Redis-backed alternative to main.py's
advisory-locked in-process background loops. Only relevant when REDIS_URL is
set; run as its own process, separate from the FastAPI/uvicorn web process:

    arq app.worker.WorkerSettings

This is what lets the background jobs scale independently: run more `arq`
processes to increase throughput without touching web-process concurrency,
without the old N-workers-each-starting-N-copies-of-every-loop problem
(see main.py's _try_acquire_bg_worker_lock) -- Arq's own queue/cron dedup
handles that instead of a Postgres advisory lock.

Every job here calls the SAME single-pass function the in-process fallback
loop calls (see each services/*_worker.py module) -- this file only adds
*where and how* the pass gets triggered (Arq cron, roughly matching each
loop's original sleep interval) and the same bg_task_status:<name>
crash-persistence main.py's _track_bg_task already provides for the
in-process path, so GET-endpoints/admin views that read system_status don't
care which mode is running.

Schedule notes (documented approximations -- Arq cron matches specific
clock times, not "N seconds after the previous run finished" the way the
original asyncio loops did):
  - cv_enricher: processes one CV per invocation, ticked every 5s to
    approximate the original loop's ~3s busy-pace / 30s idle-pace.
  - campus_email / enteri_ai_render: every 30s, matching their original
    ~20-45s idle/batch cadence.
  - linkedin_reminder / hm_feedback_reminder: every 30 minutes, matching
    their original 1800s idle-sleep.
  - email_ingest / recruiter_email: every 5 minutes (recruiter_email reads
    RECRUITER_EMAIL_POLL_SECONDS once at import time -- unlike the
    in-process loop, which re-reads it every cycle, so changing that env
    var in queued mode needs an `arq` process restart to take effect).
  - preboarding_proposer: once daily at a fixed hour (03:00 UTC) rather
    than "24h after the previous run" -- operationally equivalent for a
    daily batch job, called out here as a real (minor) semantic difference.
Every job also has run_at_startup=True, matching the original loops (each
of which ran its first pass immediately, then slept).
"""
import os

from arq import cron
from arq.connections import RedisSettings

from .db import query
from .services.queue import redis_url, try_acquire_job_lock, release_job_lock


def _mark_crashed(name: str, exc: Exception) -> None:
    """Same bg_task_status:<name> key main.py's _track_bg_task writes on an
    in-process task crash, so crash-visibility doesn't depend on which mode
    (in-process vs Arq) is running."""
    try:
        query(
            """INSERT INTO system_status (key, value) VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            [f"bg_task_status:{name}", f"crashed|{exc}"[:500]],
            fetch=False,
        )
    except Exception:
        pass


async def _run_locked(ctx, name: str, pass_fn) -> str:
    """
    For jobs with no row-level DB claim of their own (email_ingest,
    recruiter_email): wraps pass_fn in a Postgres advisory-lock job_lock so
    an overlapping trigger skips instead of doing concurrent duplicate work.
    This is defense-in-depth on top of Arq's own `unique=True` cron dedup
    (Arq's default -- a cron job is not re-scheduled while a previous run of
    the same job_id is still in flight); job_lock doesn't depend on trusting
    that internal Arq guarantee and is independently verifiable (see
    services/queue.py).
    """
    import asyncio
    conn = await asyncio.to_thread(try_acquire_job_lock, name)
    if conn is None:
        print(f"[worker] {name}: skipped -- another run already in progress")
        return "skipped_overlap"
    try:
        return await pass_fn()
    except Exception as exc:
        _mark_crashed(name, exc)
        raise
    finally:
        await asyncio.to_thread(release_job_lock, conn, name)


async def _run_unlocked(ctx, name: str, pass_fn) -> str:
    """For jobs that already claim their own rows safely under concurrent
    execution (FOR UPDATE SKIP LOCKED, or a status CAS) -- no job_lock
    needed, just the same crash-persistence as the in-process path."""
    try:
        return await pass_fn()
    except Exception as exc:
        _mark_crashed(name, exc)
        raise


# ── Job functions -- one per background loop ──────────────────────────────

async def job_cv_enricher(ctx):
    from .services.cv_enricher import run_one_pass
    return await _run_unlocked(ctx, "cv_enricher", run_one_pass)


async def job_email_ingest(ctx):
    from .services.email_ingest import run_one_pass
    return await _run_locked(ctx, "email_ingest", run_one_pass)


async def job_campus_email_worker(ctx):
    from .services.campus_email_worker import run_one_batch
    return await _run_unlocked(ctx, "campus_email_worker", run_one_batch)


async def job_enteri_ai_render_worker(ctx):
    from .services.enteri_ai_render_worker import run_one_batch
    return await _run_unlocked(ctx, "enteri_ai_render_worker", run_one_batch)


async def job_linkedin_reminder_worker(ctx):
    from .services.linkedin_reminder_worker import run_one_batch
    return await _run_unlocked(ctx, "linkedin_reminder_worker", run_one_batch)


async def job_recruiter_email_worker(ctx):
    from .services.recruiter_email_worker import run_one_pass
    return await _run_locked(ctx, "recruiter_email_worker", run_one_pass)


async def job_hm_feedback_reminder_worker(ctx):
    from .services.hm_feedback_reminder_worker import run_one_batch
    return await _run_unlocked(ctx, "hm_feedback_reminder_worker", run_one_batch)


async def job_preboarding_proposer_worker(ctx):
    from .services.preboarding_proposer_worker import run_one_pass
    return await _run_unlocked(ctx, "preboarding_proposer_worker", run_one_pass)


async def _on_startup(ctx):
    print("[worker] Arq worker process started")


async def _on_shutdown(ctx):
    # Arq itself already waits for in-flight jobs to finish (up to
    # job_timeout) before this fires -- see WorkerSettings below -- so this
    # is just a log line, not additional graceful-shutdown logic.
    print("[worker] Arq worker process shutting down")


_RECRUITER_EMAIL_POLL_SECONDS = int(
    os.environ.get("RECRUITER_EMAIL_POLL_SECONDS", "300") or "300"
)


class WorkerSettings:
    functions = [
        job_cv_enricher,
        job_email_ingest,
        job_campus_email_worker,
        job_enteri_ai_render_worker,
        job_linkedin_reminder_worker,
        job_recruiter_email_worker,
        job_hm_feedback_reminder_worker,
        job_preboarding_proposer_worker,
    ]
    cron_jobs = [
        cron(job_cv_enricher, second=set(range(0, 60, 5)), run_at_startup=True),
        cron(job_email_ingest, minute=set(range(0, 60, 5)), second=0, run_at_startup=True),
        cron(job_campus_email_worker, second={0, 30}, run_at_startup=True),
        cron(job_enteri_ai_render_worker, second={0, 30}, run_at_startup=True),
        cron(job_linkedin_reminder_worker, minute={0, 30}, second=0, run_at_startup=True),
        cron(
            job_recruiter_email_worker,
            minute=set(range(0, 60, max(1, _RECRUITER_EMAIL_POLL_SECONDS // 60))),
            second=0,
            run_at_startup=True,
        ),
        cron(job_hm_feedback_reminder_worker, minute={0, 30}, second=0, run_at_startup=True),
        cron(job_preboarding_proposer_worker, hour={3}, minute={0}, run_at_startup=True),
    ]
    redis_settings = RedisSettings.from_dsn(redis_url() or "redis://localhost:6379")
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    max_jobs = 10
    job_timeout = 300  # 5 minutes -- generous enough for a slow batch (e.g. campus email) or LLM call
