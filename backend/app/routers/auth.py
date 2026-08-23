from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import create_token, get_current_user, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


def _client_ip(request: Request) -> str:
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


_RL_WINDOW_MIN       = 15
_RL_MAX_PER_IP_EMAIL = 5    # targeted: one IP brute-forcing one account
_RL_MAX_PER_IP       = 20   # spray: one IP guessing across many accounts


def _rate_limited(email: str, ip: str) -> bool:
    """True if this (ip+email) or this ip alone has too many FAILED attempts
    in the window. Only failures are counted (see login() — successes are not
    logged as failures and clear the slate)."""
    win = f"{_RL_WINDOW_MIN} minutes"
    by_pair = query_one(
        "SELECT COUNT(*) AS n FROM login_attempt "
        "WHERE success = false AND email = %s AND ip_address = %s "
        f"AND attempted_at > now() - INTERVAL '{win}'",
        [email, ip],
    )
    if by_pair and int(by_pair["n"]) >= _RL_MAX_PER_IP_EMAIL:
        return True
    by_ip = query_one(
        "SELECT COUNT(*) AS n FROM login_attempt "
        "WHERE success = false AND ip_address = %s "
        f"AND attempted_at > now() - INTERVAL '{win}'",
        [ip],
    )
    if by_ip and int(by_ip["n"]) >= _RL_MAX_PER_IP:
        return True
    return False


def _log_attempt(email: str, ip: str, success: bool) -> None:
    try:
        query("INSERT INTO login_attempt (email, ip_address, success) VALUES (%s,%s,%s)",
              [email, ip, success], fetch=False)
    except Exception:
        pass  # best-effort; never let logging break login


@router.post("/login")
def login(body: LoginIn, request: Request):
    email = body.email.lower().strip()
    ip = _client_ip(request)

    # 1. Rate-limit gate FIRST — before any DB user lookup or password check.
    if _rate_limited(email, ip):
        raise HTTPException(429, "Too many login attempts. Please wait 15 minutes and try again.")

    user = query_one(
        "SELECT id, full_name, email, role, password_hash, is_active, bu_id "
        "FROM app_user WHERE email = %s", [email],
    )

    # 2. Generic 401 for ALL failure modes (no user / inactive / not-set-up /
    #    wrong password) — identical message, so none of them are distinguishable.
    #    Every failure is logged for rate-limiting.
    def _fail() -> NoReturn:
        _log_attempt(email, ip, False)
        raise HTTPException(401, "Invalid email or password")

    if not user or not user["is_active"]:
        _fail()
    if not user["password_hash"]:
        _fail()                      # was "Account not set up — contact your admin" (enumeration leak) — now generic
    if not verify_password(body.password, user["password_hash"]):
        _fail()

    # 3. Success: log success, and CLEAR this pair's recent failures so a
    #    legit user who mistyped a couple times isn't left near lockout.
    _log_attempt(email, ip, True)
    try:
        query("DELETE FROM login_attempt WHERE success = false AND email = %s AND ip_address = %s",
              [email, ip], fetch=False)
    except Exception:
        pass

    # Existing best-effort login_log write — keep as-is
    try:
        query(
            "INSERT INTO login_log (user_id, user_role, ip_address) VALUES (%s, %s, %s)",
            [user["id"], user["role"], ip],
            fetch=False,
        )
    except Exception:
        pass
    return {
        "token": create_token(dict(user)),
        "role": user["role"],
        "name": user["full_name"],
    }


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return user


@router.post("/change-password")
def change_password(body: ChangePasswordIn, user: dict = Depends(get_current_user)):
    if len(body.new_password) < 6:
        raise HTTPException(400, "New password must be at least 6 characters")
    row = query_one(
        "SELECT password_hash FROM app_user WHERE id = %s", [user["sub"]]
    )
    if not row or not verify_password(body.old_password, row["password_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    query(
        "UPDATE app_user SET password_hash = %s WHERE id = %s",
        [hash_password(body.new_password), user["sub"]],
        fetch=False,
    )
    return {"ok": True}
