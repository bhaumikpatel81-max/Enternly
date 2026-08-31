"""
Platform-admin login. Separate endpoint from /api/auth/login (not just a
role check bolted onto the same route) so Enternstech staff sign in through
a dedicated page (platform-login.html) and get a token carrying platform=True
-- but it's still a normal aud=AUD_STAFF staff token under the hood, built by
the same create_platform_token()/_build_token() machinery as every other
staff login, and it reuses auth.py's exact rate-limit/anti-enumeration
shape via login_rate_limit.py so this surface isn't a weaker brute-force
target than the ordinary login.
"""
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import create_platform_token, verify_password
from ..login_rate_limit import client_ip, log_attempt, rate_limited

router = APIRouter(prefix="/api/platform", tags=["platform-auth"])


class PlatformLoginIn(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
def platform_login(body: PlatformLoginIn, request: Request):
    email = body.email.lower().strip()
    ip = client_ip(request)

    if rate_limited(email, ip):
        raise HTTPException(429, "Too many login attempts. Please wait 15 minutes and try again.")

    user = query_one(
        "SELECT id, full_name, email, role, password_hash, is_active, "
        "tenant_id, token_version, is_platform_superadmin, is_company_admin "
        "FROM app_user WHERE email = %s", [email],
    )

    # Generic 401 for every failure mode -- no such user, inactive, no
    # password set, wrong password, OR not flagged as a platform superadmin
    # -- all indistinguishable, same anti-enumeration shape as auth.py::login.
    def _fail() -> NoReturn:
        log_attempt(email, ip, False)
        raise HTTPException(401, "Invalid email or password")

    if not user or not user["is_active"]:
        _fail()
    if not user["password_hash"]:
        _fail()
    if not verify_password(body.password, user["password_hash"]):
        _fail()
    if not user["is_platform_superadmin"]:
        _fail()

    log_attempt(email, ip, True)
    try:
        query("DELETE FROM login_attempt WHERE success = false AND email = %s AND ip_address = %s",
              [email, ip], fetch=False)
    except Exception:
        pass
    try:
        query(
            "INSERT INTO login_log (user_id, user_role, ip_address) VALUES (%s, %s, %s)",
            [user["id"], user["role"], ip],
            fetch=False,
        )
    except Exception:
        pass
    try:
        query("UPDATE app_user SET last_login_at = now() WHERE id = %s", [user["id"]], fetch=False)
    except Exception:
        pass

    return {
        "token": create_platform_token(dict(user)),
        "role": user["role"],
        "name": user["full_name"],
    }
