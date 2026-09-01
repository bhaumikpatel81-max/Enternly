"""
Rate limiter for abuse/cost protection on expensive or public endpoints (AI
calls, uploads, password reset) not already covered by the DB-backed login
limiter (login_rate_limit.py).

Dual-mode, same pattern as services/queue.py: REDIS_URL unset (the default)
uses an in-process in-memory counter -- exact cross-process enforcement
doesn't matter for a single small deployment, and WEB_CONCURRENCY defaults
small (see Dockerfile) so each process independently capping abuse is good
enough. REDIS_URL set enforces the same limits in Redis (a sorted-set sliding
window) so they hold across every process/replica, not just the one that
happened to see a given request.
"""
import threading
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import HTTPException, Request

from ..login_rate_limit import client_ip
from .queue import queue_enabled, get_redis_client

_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)


def _check_in_memory(bucket: str, identity: str, max_calls: int, window_seconds: int) -> None:
    key = f"{bucket}:{identity}"
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= max_calls:
            raise HTTPException(429, "Too many requests — please slow down and try again shortly.")
        q.append(now)


def _check_redis(bucket: str, identity: str, max_calls: int, window_seconds: int) -> None:
    """
    Sorted-set sliding window: each call adds a (now, unique-member) entry
    scored by timestamp, expires anything older than the window, then counts
    what's left. ZADD+ZREMRANGEBYSCORE+ZCARD+EXPIRE run individually (not a
    Lua script/MULTI) -- a small race between two concurrent requests can at
    worst let one or two extra calls through right at the boundary, which is
    an acceptable trade for staying simple; this is abuse/cost protection,
    not a hard security boundary. Falls back to the in-memory limiter (fails
    open on the Redis side, not on the limit) if Redis itself is unreachable,
    so a Redis blip never takes down the endpoints it's protecting.
    """
    try:
        r = get_redis_client()
        key = f"ratelimit:{bucket}:{identity}"
        now = time.time()
        r.zremrangebyscore(key, 0, now - window_seconds)
        count = r.zcard(key)
        if count >= max_calls:
            raise HTTPException(429, "Too many requests — please slow down and try again shortly.")
        r.zadd(key, {f"{now}:{id(object())}": now})
        r.expire(key, window_seconds)
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[rate_limit] Redis unavailable ({exc}), falling back to in-memory for this call")
        _check_in_memory(bucket, identity, max_calls, window_seconds)


def check_rate_limit(bucket: str, identity: str, max_calls: int, window_seconds: int) -> None:
    """Raise 429 if `identity` has made >= max_calls calls to `bucket` within
    the last window_seconds."""
    if queue_enabled():
        _check_redis(bucket, identity, max_calls, window_seconds)
    else:
        _check_in_memory(bucket, identity, max_calls, window_seconds)


def rate_limit_dep(bucket: str, max_calls: int, window_seconds: int):
    """FastAPI dependency factory, e.g. Depends(rate_limit_dep("ai_turn", 30, 60))."""

    def _dep(request: Request) -> None:
        # Prefer the authenticated identity (staff/vendor/candidate JWT `sub`)
        # when available; fall back to client IP for public/token-auth routes.
        user: Optional[dict] = getattr(request.state, "user", None)
        identity = (user or {}).get("sub") if user else None
        check_rate_limit(bucket, identity or client_ip(request), max_calls, window_seconds)

    return _dep
