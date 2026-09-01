"""
Redis/Arq dual-mode plumbing shared by worker.py (queued background jobs)
and rate_limit.py (cross-process rate limiting).

REDIS_URL unset (the default) = every consumer of this module falls back to
its pre-existing behavior unchanged: main.py's advisory-locked in-process
background loops, and rate_limit.py's in-memory counters. Nothing here is a
hard dependency for local/free-tier dev.

REDIS_URL set = main.py skips starting the in-process loops (see
main.py's _start_background_services) and a separate `arq app.worker.WorkerSettings`
process (see worker.py) runs the same job logic as Arq cron jobs instead;
rate_limit.py enforces limits in Redis so they hold across every process.
"""
import hashlib
import os
from typing import Optional


def redis_url() -> Optional[str]:
    url = os.environ.get("REDIS_URL", "").strip()
    return url or None


def queue_enabled() -> bool:
    return redis_url() is not None


_redis_client = None


def get_redis_client():
    """Lazy singleton sync redis-py client, used by rate_limit.py. Only ever
    constructed when REDIS_URL is set -- callers must check queue_enabled()
    (or redis_url()) first; this raises if called without one."""
    global _redis_client
    if _redis_client is None:
        import redis  # local import: only required when REDIS_URL is set
        _redis_client = redis.Redis.from_url(redis_url(), decode_responses=True)
    return _redis_client


def _job_lock_key(name: str) -> int:
    """Deterministic 64-bit signed int from a job name, for pg_advisory_lock
    (which takes a bigint key, not an arbitrary string)."""
    digest = hashlib.sha256(f"job_lock:{name}".encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def try_acquire_job_lock(name: str):
    """
    Best-effort mutual exclusion for one named job's single pass, so an
    overlapping trigger (a slow pass still running when the next Arq cron
    tick fires, or -- in principle -- two worker processes) skips instead of
    doing genuinely concurrent duplicate work. Only needed for the handful
    of jobs (email_ingest, recruiter_email) that have no row-level DB claim
    of their own; every job that already uses FOR UPDATE SKIP LOCKED
    (campus_email, enteri_ai_render, linkedin_reminder, hm_feedback_reminder,
    cv_enricher) is already safe to run concurrently without this.

    Returns the open psycopg2 connection holding the lock (pass it to
    release_job_lock when done), or None if another run already holds it.
    Uses a dedicated connection (not the pool) + a session-level advisory
    lock, same mechanism and same reasoning as main.py's
    _try_acquire_bg_worker_lock: tied to the connection's session, so it's
    always released (even on a hard crash) the moment that connection closes
    -- no separate cleanup/expiry logic needed.
    """
    import psycopg2

    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME", "oneclickhire"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", [_job_lock_key(name)])
        acquired = cur.fetchone()[0]
        cur.close()
        if acquired:
            return conn
        conn.close()
        return None
    except Exception as exc:
        print(f"[queue] job_lock({name!r}) acquire failed: {exc}")
        return None


def release_job_lock(conn, name: str) -> None:
    if conn is None:
        return
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_unlock(%s)", [_job_lock_key(name)])
        cur.close()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
