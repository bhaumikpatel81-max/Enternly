from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import create_token, get_current_user, hash_password, verify_password
from ..login_rate_limit import client_ip as _client_ip, log_attempt as _log_attempt, rate_limited as _rate_limited

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/login")
def login(body: LoginIn, request: Request):
    email = body.email.lower().strip()
    ip = _client_ip(request)

    # 1. Rate-limit gate FIRST — before any DB user lookup or password check.
    if _rate_limited(email, ip):
        raise HTTPException(429, "Too many login attempts. Please wait 15 minutes and try again.")

    user = query_one(
        "SELECT id, full_name, email, role, password_hash, is_active, bu_id, "
        "tenant_id, token_version, is_platform_superadmin, is_company_admin "
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
