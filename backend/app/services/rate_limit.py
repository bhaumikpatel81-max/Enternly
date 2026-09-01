"""
Lightweight in-process rate limiter for abuse/cost protection on expensive or
public endpoints (AI calls, uploads, password reset) not already covered by
the DB-backed login limiter (login_rate_limit.py).

Deliberately simple and in-memory: exact cross-process enforcement matters
less here than bounding the worst case cheaply, and WEB_CONCURRENCY defaults
small (see Dockerfile) so each worker process independently capping abuse is
good enough for cost/DoS protection, without adding a DB round-trip to every
AI/upload request.
"""
import threading
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import HTTPException, Request

from ..login_rate_limit import client_ip

_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)


def check_rate_limit(bucket: str, identity: str, max_calls: int, window_seconds: int) -> None:
    """Raise 429 if `identity` has made >= max_calls calls to `bucket` within
    the last window_seconds."""
    key = f"{bucket}:{identity}"
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= max_calls:
            raise HTTPException(429, "Too many requests — please slow down and try again shortly.")
        q.append(now)


def rate_limit_dep(bucket: str, max_calls: int, window_seconds: int):
    """FastAPI dependency factory, e.g. Depends(rate_limit_dep("ai_turn", 30, 60))."""

    def _dep(request: Request) -> None:
        # Prefer the authenticated identity (staff/vendor/candidate JWT `sub`)
        # when available; fall back to client IP for public/token-auth routes.
        user: Optional[dict] = getattr(request.state, "user", None)
        identity = (user or {}).get("sub") if user else None
        check_rate_limit(bucket, identity or client_ip(request), max_calls, window_seconds)

    return _dep
