"""
Login rate-limiting + client-IP helpers shared by every staff-facing login
endpoint (routers/auth.py's /api/auth/login and routers/platform_auth_api.py's
/api/platform/auth/login). Extracted verbatim out of routers/auth.py so the
platform login doesn't duplicate this logic -- both endpoints share the same
login_attempt table and the same brute-force/spray thresholds.
"""
from fastapi import Request

from .db import query, query_one


def client_ip(request: Request) -> str:
    # Enternly runs behind a reverse proxy. The proxy MUST be configured to
    # OVERWRITE (not append) X-Real-IP / X-Forwarded-For, otherwise these
    # are client-spoofable. Prefer X-Real-IP (single value the proxy sets),
    # then the left-most X-Forwarded-For entry, then the direct peer.
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return (request.client.host if request and request.client else "unknown")


RL_WINDOW_MIN       = 15
RL_MAX_PER_IP_EMAIL = 5    # targeted: one IP brute-forcing one account
RL_MAX_PER_IP       = 20   # spray: one IP guessing across many accounts


def rate_limited(email: str, ip: str) -> bool:
    """True if this (ip+email) or this ip alone has too many FAILED attempts
    in the window. Only failures are counted (see login() — successes are not
    logged as failures and clear the slate)."""
    win = f"{RL_WINDOW_MIN} minutes"
    by_pair = query_one(
        "SELECT COUNT(*) AS n FROM login_attempt "
        "WHERE success = false AND email = %s AND ip_address = %s "
        f"AND attempted_at > now() - INTERVAL '{win}'",
        [email, ip],
    )
    if by_pair and int(by_pair["n"]) >= RL_MAX_PER_IP_EMAIL:
        return True
    by_ip = query_one(
        "SELECT COUNT(*) AS n FROM login_attempt "
        "WHERE success = false AND ip_address = %s "
        f"AND attempted_at > now() - INTERVAL '{win}'",
        [ip],
    )
    if by_ip and int(by_ip["n"]) >= RL_MAX_PER_IP:
        return True
    return False


def log_attempt(email: str, ip: str, success: bool) -> None:
    try:
        query("INSERT INTO login_attempt (email, ip_address, success) VALUES (%s,%s,%s)",
              [email, ip, success], fetch=False)
    except Exception:
        pass  # best-effort; never let logging break login
