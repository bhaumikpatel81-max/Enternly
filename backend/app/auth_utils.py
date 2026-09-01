"""
JWT + bcrypt utilities shared across auth and admin routers.
"""
import os
from datetime import datetime, timedelta

import bcrypt as _bcrypt

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from .db import query_one

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


def _build_token(user: dict, *, extra_claims: dict | None = None, hours: float = TOKEN_HOURS) -> str:
    """Shared staff-JWT builder behind create_token/create_platform_token/
    create_impersonation_token -- one place assembling the base claim set so
    the three call sites can't drift. `extra_claims` is merged in last (so it
    can only add claims, e.g. platform=True or isImpersonation=True, never
    silently override sub/tenant_id/tver/etc)."""
    expire = datetime.utcnow() + timedelta(hours=hours)
    claims = {
        "sub": str(user["id"]),
        "email": user["email"],
        "role": user["role"],
        "name": user["full_name"],
        "tenant_id": str(user["tenant_id"]) if user.get("tenant_id") else None,
        "is_platform_superadmin": bool(user.get("is_platform_superadmin")),
        "is_company_admin": bool(user.get("is_company_admin")),
        # Snapshot of app_user.token_version at login time -- compared
        # against the live value on every request by _refresh_staff_claims
        # so a role change or admin-forced logout doesn't have to wait
        # out the token's remaining TOKEN_HOURS expiry.
        "tver": user.get("token_version") or 0,
        "aud": AUD_STAFF,
        "exp": expire,
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)


def create_token(user: dict) -> str:
    return _build_token(user)


def create_download_token(user: dict) -> str:
    """Very short-lived (60s) staff token for one-off file-download links
    (opening a CV/JD/resume in a new browser tab, which can't attach a
    custom Authorization header, so the token has to travel in the URL).
    A leaked URL, access log line, or Referer header now only exposes a
    session valid for a minute instead of the full TOKEN_HOURS session."""
    return _build_token(user, hours=1 / 60)


def create_platform_token(user: dict) -> str:
    """Same staff token shape as create_token, plus platform=True so the
    platform console's own auth guard can tell a platform login apart from
    an ordinary staff login -- still aud=AUD_STAFF, so it's accepted by every
    existing staff-only dependency (require_company_admin etc), it just also
    satisfies require_platform_admin's is_platform_superadmin check."""
    return _build_token(user, extra_claims={"platform": True})


def create_impersonation_token(user: dict, impersonated_by: str) -> str:
    """A platform superadmin briefly acting AS the target user (Feature F).
    Short-lived (15 min, not TOKEN_HOURS) and carries isImpersonation=True +
    impersonatedBy so: (1) require_platform_admin above always rejects it --
    an impersonation session can never reach the platform console itself;
    (2) index.html can show a "you are impersonating" banner and audit the
    session; still aud=AUD_STAFF so every ordinary staff-only dependency
    (require_company_admin etc) accepts it exactly like a real session for
    that user."""
    return _build_token(
        user,
        extra_claims={"isImpersonation": True, "impersonatedBy": str(impersonated_by)},
        hours=0.25,
    )


def _refresh_staff_claims(payload: dict) -> dict:
    """A staff JWT is valid for up to TOKEN_HOURS, but role/tenant_id are
    read live here on every request rather than trusted from the (possibly
    hours-stale) token -- this is what actually fixes the long-standing bug
    where an admin reassigning someone's role/company didn't take effect
    until they happened to log out and back in (see hrbp_api.py's own
    ad hoc version of this same fix for BU scoping). token_version is the
    explicit escape hatch on top of that: bumping it invalidates every
    outstanding token for that user immediately, for cases with no live
    column to re-read (e.g. an admin-forced logout with no other state
    change). Vendor/candidate payloads have no role claim -- pass through
    untouched, there is nothing of theirs to refresh here."""
    if not is_staff_payload(payload):
        return payload
    row = query_one(
        """SELECT u.role, u.tenant_id, u.token_version, u.is_active, u.full_name,
                  u.is_platform_superadmin, u.is_company_admin,
                  t.status AS tenant_status, t.is_deleted AS tenant_deleted,
                  t.subscription_end_date, t.grace_period_days
           FROM app_user u
           LEFT JOIN tenant t ON t.id = u.tenant_id
           WHERE u.id = %s""",
        [payload.get("sub")],
    )
    if not row or not row.get("is_active"):
        raise HTTPException(401, "Account is inactive or no longer exists")
    if int(row.get("token_version") or 0) != int(payload.get("tver") or 0):
        raise HTTPException(401, "Session expired — please log in again")
    # Tenant lifecycle -- checked live on every request (not just at login)
    # so suspending/deleting a tenant, or letting its subscription run out
    # past its grace period, ends every open session immediately rather than
    # waiting for token expiry. A NULL subscription_end_date never blocks
    # (no subscription configured yet = unrestricted, per design decision).
    #
    # EXEMPT platform superadmins from this check entirely. They belong to
    # the Enternstech/seed tenant, which this same check would otherwise
    # apply to like any other -- meaning a superadmin who suspends (or lets
    # expire) their OWN tenant would lock out every platform superadmin,
    # including from the only account that could undo it via the API. A
    # platform superadmin's access is never meant to depend on their home
    # tenant's commercial status.
    if not row.get("is_platform_superadmin"):
        if row.get("tenant_deleted") or row.get("tenant_status") == "suspended":
            raise HTTPException(401, "This company's account is no longer active")
        end_date = row.get("subscription_end_date")
        if end_date is not None:
            from datetime import date as _date, timedelta as _timedelta
            if _date.today() > end_date + _timedelta(days=row.get("grace_period_days") or 0):
                raise HTTPException(401, "This company's subscription has expired")
    payload["role"] = row["role"]
    payload["tenant_id"] = str(row["tenant_id"]) if row.get("tenant_id") else None
    payload["name"] = row["full_name"]
    payload["is_platform_superadmin"] = bool(row.get("is_platform_superadmin"))
    payload["is_company_admin"] = bool(row.get("is_company_admin"))
    return payload


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
            return _refresh_staff_claims(jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], audience=aud))
        except JWTError as exc:
            last_err = exc
            continue
    # No known audience matched. A token with no aud claim at all predates
    # this claim -- accept it via the same legacy grace window used
    # elsewhere. One that HAS an aud but none of ours is genuinely invalid.
    legacy = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_aud": False})
    if legacy.get("aud"):
        raise last_err or JWTError("Invalid audience")
    return _refresh_staff_claims(legacy)


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
        return _refresh_staff_claims(payload)
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
    return _refresh_staff_claims(legacy)


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


def is_platform_tier(user: dict) -> bool:
    """True if `user` qualifies as platform-superadmin tier: the
    is_platform_superadmin flag ONLY -- deliberately NO 'admin'-role-string
    fallback. Unlike company-tier below, a role fallback here would be
    actively unsafe: platform_admin_api.py::create_tenant/add_tenant_admin
    both create ordinary company admins with role='admin' (kept for
    NAV_DEF/index.html nav compatibility, see PLATFORM_ADMIN_MAPPING.md
    §5b) + is_company_admin=TRUE, is_platform_superadmin=FALSE -- so
    'role == admin' is NOT a reliable platform-tier signal once this
    project's own tenant-creation flow exists; it would let any ordinary
    company admin reach cross-tenant /api/platform/* endpoints. Migration
    100's backfill already sets is_platform_superadmin=TRUE for every
    genuine legacy admin/platform_admin account on the seed tenant, so no
    role-string fallback is actually needed for correctness here -- confirmed
    live: an earlier version of this function WITH the fallback let a
    freshly-created company admin (role=admin, is_company_admin=TRUE,
    is_platform_superadmin=FALSE) successfully call GET /api/platform/stats;
    caught and fixed during Fix #3's own live verification before it was
    ever committed. Shared by require_platform_admin below and by every
    router-local platform-only gate migrated off the retired role-string
    pattern (Step 1 full audit, finding #3)."""
    return bool(user.get("is_platform_superadmin"))


def is_company_tier(user: dict) -> bool:
    """True if `user` qualifies as company-admin tier: is_company_admin,
    is_platform_superadmin (platform staff can still reach into a tenant's
    own admin surface), or the legacy 'admin' role fallback. The 'admin'
    fallback IS safe here (unlike is_platform_tier above) -- every
    'admin'-role account is guaranteed at least one of the two flags by
    Migration 100's backfill (is_platform_superadmin on the seed tenant,
    is_company_admin everywhere else), so the fallback is redundant for any
    real account and only matters as defense-in-depth; it can never grant
    a company admin anything beyond company-tier access, which they'd
    already have via is_company_admin regardless. Shared by
    require_company_admin below and by every router-local company-admin
    gate migrated off the retired role-string pattern (Step 1 full audit,
    finding #3) -- accepts any dict carrying these keys, not just a
    decoded JWT payload, so it also works directly against a plain
    app_user row fetched via query_one() (see password_api.py)."""
    return bool(user.get("is_company_admin") or user.get("is_platform_superadmin") or user.get("role") == "admin")


def require_platform_admin(user: dict = Depends(get_current_user)) -> dict:
    """Enternstech-only tier: manages the tenant/company roster itself. An
    impersonation token can never satisfy this, regardless of the
    impersonated user's own flags -- the platform console must always be
    operated from a real platform session."""
    if user.get("isImpersonation"):
        raise HTTPException(403, "Platform Admin access required")
    if not is_platform_tier(user):
        raise HTTPException(403, "Platform Admin access required")
    return user


def require_company_admin(user: dict = Depends(get_current_user)) -> dict:
    """A customer's own super admin: user management, org settings,
    SMTP/calendar integrations -- scoped to their company. ta_manager is
    deliberately excluded -- it's restricted to team management + reports,
    see require_ta_manager below."""
    if is_company_tier(user):
        return user
    raise HTTPException(403, "Company Admin access required")


def require_ta_manager(user: dict = Depends(get_current_user)) -> dict:
    """Team management + reporting only -- anyone who can already act as a
    Company Admin passes too, since that role is a superset.

    Confirmed zero call sites anywhere in the codebase as of the Platform
    Admin closeout pass (2026-08-31) -- intentionally retained rather than
    removed, since the ta_manager tier boundary it documents is real and
    actively checked ad hoc (role == "ta_manager") across many routers
    (pipeline_api.py, offers_api.py, scheduling_api.py, reports_api.py,
    etc.); this is the one place that boundary is expressed as a reusable
    FastAPI dependency, for whichever future endpoint wants it instead of
    repeating the role check inline. Still string-based rather than
    flag-based (see is_company_tier above) since ta_manager has no
    dedicated boolean flag today -- not part of the is_company_tier/
    is_platform_tier flag migration (Step 1 full audit, finding #3), which
    only covered company-admin/platform-admin tier, not this one."""
    if user.get("role") not in ("admin", "platform_admin", "company_admin", "ta_manager"):
        raise HTTPException(403, "TA Manager access required")
    return user
