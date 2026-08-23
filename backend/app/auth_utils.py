"""
JWT + bcrypt utilities shared across auth and admin routers.
"""
import os
from datetime import datetime, timedelta

import bcrypt as _bcrypt

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

_bearer = HTTPBearer(auto_error=False)

SECRET_KEY = os.environ.get("JWT_SECRET", "").strip()
if not SECRET_KEY:
    # Allow a dev default ONLY when not in production.
    if os.environ.get("ENV", "").lower() in ("prod", "production"):
        raise RuntimeError(
            "JWT_SECRET is not set. Add a long random JWT_SECRET to .env.prod "
            "before starting in production."
        )
    SECRET_KEY = "enternly-ats-dev-secret-change-in-prod"
ALGORITHM = "HS256"
TOKEN_HOURS = 8

# Signed "aud" claims so a mismatched token fails at the signature-verification
# layer (jwt.decode(..., audience=...)), not just a hand-written payload check.
# One shared SECRET_KEY still signs all three — this only adds audience
# separation, not a new key.
AUD_STAFF = "enternly-staff"
AUD_VENDOR = "enternly-vendor"
AUD_CANDIDATE = "enternly-candidate"


def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt(rounds=10)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_token(user: dict) -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_HOURS)
    return jwt.encode(
        {
            "sub": str(user["id"]),
            "email": user["email"],
            "role": user["role"],
            "name": user["full_name"],
            "aud": AUD_STAFF,
            "exp": expire,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


_KNOWN_AUDIENCES = (AUD_STAFF, AUD_VENDOR, AUD_CANDIDATE)


def _decode(token: str) -> dict:
    """Generic gate for main.py's global auth_middleware, which runs before
    any surface-specific check and must accept a token signed for ANY of
    our three audiences (staff/vendor/candidate) -- each surface's own
    dependency (get_current_user/get_current_vendor/get_current_candidate)
    enforces its specific audience afterwards. Genuinely verifies the aud
    claim against each known audience in turn -- never disables
    verification -- falling through to a no-aud decode only for tokens
    issued before the aud claim existed at all (the same bounded legacy
    grace window used by decode_staff_token/get_current_vendor/
    get_current_candidate)."""
    last_err = None
    for aud in _KNOWN_AUDIENCES:
        try:
            return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], audience=aud)
        except JWTError as exc:
            last_err = exc
            continue
    # No known audience matched. A token with no aud claim at all predates
    # this claim -- accept it via the same legacy grace window used
    # elsewhere. One that HAS an aud but none of ours is genuinely invalid.
    legacy = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_aud": False})
    if legacy.get("aud"):
        raise last_err or JWTError("Invalid audience")
    return legacy


def _decode_aud(token: str, audience: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], audience=audience)


def is_staff_payload(payload: dict) -> bool:
    """Staff JWTs (create_token above) carry a role and no account_type.
    Candidate/vendor portal JWTs carry account_type='candidate'|'vendor' and
    no role. Both are signed with the same SECRET_KEY, so a portal token
    decodes successfully here too -- this is what actually distinguishes them."""
    return not payload.get("account_type") and bool(payload.get("role"))


def assert_staff(payload: dict) -> None:
    """Raise 403 unless the decoded token is a staff token. Use this directly
    on endpoints that read request.state.user instead of Depends(get_current_user)
    (get_current_user already enforces this for every dependency-based caller)."""
    if not is_staff_payload(payload):
        raise HTTPException(403, "Staff access only")


def decode_staff_token(token: str) -> dict:
    """Decode a Bearer token as a staff-audience JWT, with a legacy grace
    window for tokens issued before the aud claim existed. Shared by
    get_current_user (router-level Depends) and main.py's global
    auth_middleware (request.state.user) so both apply the identical rule --
    the middleware previously used the aud-blind _decode() and rejected every
    freshly-issued (aud-tagged) token, while only this function's callers saw
    the grace path. Raises HTTPException(401/403) on any rejection."""
    # Primary path: verify the signed staff audience.
    try:
        payload = _decode_aud(token, AUD_STAFF)
        # aud verified cryptographically; still confirm it's staff-shaped.
        assert_staff(payload)
        return payload
    except JWTError:
        pass  # fall through to grace check
    # Grace window (until all legacy tokens expire, ~8h): accept a token
    # with NO aud claim ONLY if it is staff-shaped the old way.
    try:
        legacy = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM],
                            options={"verify_aud": False})
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")
    if legacy.get("aud"):
        # It HAS an aud but it wasn't AUD_STAFF (first decode failed) →
        # this is a vendor/candidate token hitting a staff endpoint. Reject.
        raise HTTPException(403, "Staff access only")
    # No aud at all = legacy token. Narrow rule: staff-shaped only.
    if not is_staff_payload(legacy):
        raise HTTPException(403, "Staff access only")
    return legacy


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    if not creds:
        raise HTTPException(401, "Not authenticated")
    return decode_staff_token(creds.credentials)


# Explicit alias for call sites that want the staff requirement to read
# obviously in the endpoint signature -- functionally identical to
# get_current_user, which is staff-only unconditionally.
require_staff = get_current_user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin access required")
    return user


def require_admin_or_manager(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in ("admin", "ta_manager"):
        raise HTTPException(403, "Admin or TA Manager access required")
    return user
