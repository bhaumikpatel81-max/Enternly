"""
Self-service password flows — first-time set + forgot/reset.
All emails sent from the configured SMTP account. Tokens are single-use & expiring.

account_type discriminator (added Phase 1):
  'staff'     → updates app_user (original behaviour, default)
  'vendor'    → updates vendor_user
  'candidate' → updates candidate_user (Phase 2)
Every token stores its account_type so /reset-password knows which
table to write — the client never needs to send account_type explicitly.
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import hash_password, require_company_admin, get_current_user, is_company_tier
from ..services.connectors import send_email, _load_email_cfg
from ..services.activity_log import log_activity

router = APIRouter(prefix="/api/auth", tags=["password"])

_TOKEN_TTL_HOURS = 24


def _self_service_eligible(target: dict) -> bool:
    """Which staff roles may use self-service set/reset -- checks the
    TARGET account's own row (not the caller's), so is_company_tier() is
    applied directly to whatever dict was fetched via query_one(), same
    is_company_admin/is_platform_superadmin/role keys either way. Kept as
    a narrower allow-list than 'every staff role' on purpose -- deliberately
    excludes hiring_manager/bu_head/director/interviewer/hrbp/
    placement_officer, unchanged from the original role-string list's
    intent (Step 1 full audit, finding #3)."""
    return is_company_tier(target) or target.get("role") in ("ta_manager", "recruiter")


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _base_url(tenant_id: str = None) -> str:
    cfg = _load_email_cfg(tenant_id)
    return (cfg.get("base_url") or os.environ.get("APP_BASE_URL", "")).rstrip("/")


def _issue_token(user_id: str, purpose: str, account_type: str = "staff") -> str:
    """
    Create a single-use token, store its hash, return the raw token.
    account_type identifies which user table the token unlocks:
      'staff' → app_user  |  'vendor' → vendor_user  |  'candidate' → candidate_user
    """
    raw = secrets.token_urlsafe(32)
    query(
        """INSERT INTO password_reset_token
               (user_id, token_hash, purpose, expires_at, account_type)
           VALUES (%s, %s, %s, %s, %s)""",
        [
            user_id,
            _hash_token(raw),
            purpose,
            datetime.utcnow() + timedelta(hours=_TOKEN_TTL_HOURS),
            account_type,
        ],
        fetch=False,
    )
    return raw


def _build_password_html(full_name: str, to_email: str, link: str, purpose: str) -> str:
    from ..services.email_layout import build_branded_email
    if purpose == "invite":
        heading     = "Set Your<br>Password."
        hero_copy   = "An account has been created for you on Enternly. Set your password below to get started."
        button_txt  = "Set My Password"
        footer_note = "Questions? Simply reply to this email and our team will be happy to help."
    else:
        heading     = "Reset Your<br>Password."
        hero_copy   = "We received a request to reset your Enternly password. If you didn&#8217;t request this, you can safely ignore this email."
        button_txt  = "Reset My Password"
        footer_note = "Didn&#8217;t request this? You can safely ignore this email."
    name = full_name or to_email
    return build_branded_email(
        eyebrow="Application Tracking System",
        hero_title_html=heading,
        hero_subtitle=hero_copy,
        detail_cells=[("Name", name), ("Username", to_email)],
        about_text=f"This link expires in {_TOKEN_TTL_HOURS} hours and can be used only once.",
        about_heading=None,
        cta_label=button_txt, cta_link=link,
        footer_note=footer_note,
    )


def _send_link_email(to_email: str, full_name: str, raw_token: str, purpose: str, tenant_id: str = None):
    link = f"{_base_url(tenant_id)}/set-password?token={raw_token}"
    if purpose == "invite":
        subject = "Set your Enternly password"
        intro = (
            f"Hi {full_name or ''},\n\n"
            "An account has been created for you on Enternly (EnternsTech Talent Acquisition).\n"
            f"Your username is your email: {to_email}\n\n"
            "Set your password using the secure link below:"
        )
    else:
        subject = "Reset your Enternly password"
        intro = (
            f"Hi {full_name or ''},\n\n"
            "We received a request to reset your Enternly password.\n"
            "If you didn't request this, you can ignore this email.\n\n"
            "Reset your password using the secure link below:"
        )
    body = (
        f"{intro}\n\n{link}\n\n"
        f"This link expires in {_TOKEN_TTL_HOURS} hours and can be used once.\n\n"
        "— EnternsTech Talent Acquisition"
    )
    html = _build_password_html(full_name, to_email, link, purpose)
    send_email(to_email, subject, body, html=html, tenant_id=tenant_id)


def issue_invite_for_external_user(
    user_id: str, email: str, full_name: str, account_type: str, tenant_id: str = None
) -> str:
    """
    Issue a set-password invite for a vendor or candidate user.
    Sends the email and returns the raw token (caller can build the link).
    account_type must be 'vendor' or 'candidate'.
    """
    from ..services.email_validation import assert_real_email
    email = assert_real_email(email)  # last line of defence before any real send
    raw = _issue_token(str(user_id), "invite", account_type=account_type)
    _send_link_email(email, full_name, raw, "invite", tenant_id=tenant_id)
    return raw


# ── Admin: send a set-password invite to a STAFF user ────────────────────────

class InviteIn(BaseModel):
    email: str


@router.post("/send-setup-link")
def send_setup_link(body: InviteIn, admin=Depends(require_company_admin)):
    """Company Admin triggers a first-time 'set your password' email to a staff user."""
    user = query_one(
        "SELECT id, full_name, email, role, is_active, is_company_admin, is_platform_superadmin "
        "FROM app_user WHERE email=%s AND tenant_id=%s",
        [body.email.lower().strip(), admin.get("tenant_id")],
    )
    if not user or not user["is_active"]:
        raise HTTPException(404, "Active user with that email not found")
    if not _self_service_eligible(user):
        raise HTTPException(400, "Self-service password is only for staff roles (Company Admin / TA Manager / Recruiter)")
    raw = _issue_token(str(user["id"]), "invite", account_type="staff")
    _send_link_email(user["email"], user["full_name"], raw, "invite", tenant_id=admin.get("tenant_id"))
    return {"ok": True, "sent_to": user["email"]}


# ── Public: forgot password (staff / vendor / candidate) ─────────────────────

class ForgotIn(BaseModel):
    email: str


@router.post("/forgot-password")
def forgot_password(body: ForgotIn):
    """
    Public. Checks staff (app_user), then vendor (vendor_user), then candidate
    (candidate_user) for an active, eligible account matching the email, and
    issues a reset token for the first match. Always returns the same generic
    response regardless of match/no-match, to avoid leaking account existence
    across any of the three tables.
    """
    email = body.email.lower().strip()
    GENERIC = {"ok": True, "message": "If that account exists, a reset link has been sent."}

    # 1. Staff (app_user) — only self-service roles, active
    staff = query_one(
        "SELECT id, full_name, email, role, is_active, tenant_id, is_company_admin, is_platform_superadmin "
        "FROM app_user WHERE email=%s", [email]
    )
    if staff and staff["is_active"] and _self_service_eligible(staff):
        raw = _issue_token(str(staff["id"]), "reset", account_type="staff")
        try:
            _send_link_email(staff["email"], staff["full_name"], raw, "reset", tenant_id=staff.get("tenant_id"))
        except Exception as exc:
            print(f"[password] staff reset email failed: {exc}")
            # GENERIC is still returned below (anti-enumeration) -- this is the
            # only durable trace that the send failed, for an admin to notice
            # and fall back to POST /admin/users/{id}/reset-password.
            log_activity(
                "auth", "password_reset_email_failed",
                entity_id=str(staff["id"]), actor_id=None, actor_role="system",
                detail={"account_type": "staff", "email": staff["email"], "error": str(exc)},
            )
        return GENERIC

    # 2. Vendor user (vendor_user) — active only
    vu = query_one(
        "SELECT id, full_name, email, is_active, tenant_id FROM vendor_user WHERE email=%s", [email]
    )
    if vu and vu["is_active"]:
        raw = _issue_token(str(vu["id"]), "reset", account_type="vendor")
        try:
            _send_link_email(vu["email"], vu["full_name"], raw, "reset", tenant_id=vu.get("tenant_id"))
        except Exception as exc:
            print(f"[password] vendor reset email failed: {exc}")
            log_activity(
                "auth", "password_reset_email_failed",
                entity_id=str(vu["id"]), actor_id=None, actor_role="system",
                detail={"account_type": "vendor", "email": vu["email"], "error": str(exc)},
            )
        return GENERIC

    # 3. Candidate user (candidate_user) — active only. NOTE: candidate_user
    #    has no full_name column (it links to candidate). Resolve a display
    #    name via the candidate table; fall back to the email local-part.
    cu = query_one(
        """SELECT cu.id, cu.email, cu.is_active, c.full_name, c.tenant_id
           FROM candidate_user cu
           JOIN candidate c ON c.id = cu.candidate_id
           WHERE cu.email=%s""",
        [email],
    )
    if cu and cu["is_active"]:
        display_name = cu.get("full_name") or cu["email"].split("@")[0]
        raw = _issue_token(str(cu["id"]), "reset", account_type="candidate")
        try:
            _send_link_email(cu["email"], display_name, raw, "reset", tenant_id=cu.get("tenant_id"))
        except Exception as exc:
            print(f"[password] candidate reset email failed: {exc}")
            log_activity(
                "auth", "password_reset_email_failed",
                entity_id=str(cu["id"]), actor_id=None, actor_role="system",
                detail={"account_type": "candidate", "email": cu["email"], "error": str(exc)},
            )
        return GENERIC

    # No match anywhere — same generic response (no enumeration).
    return GENERIC


class AdminResetIn(BaseModel):
    account_type: str  # 'vendor' | 'candidate'
    user_id: str
    new_password: str


@router.post("/admin-reset-password")
def admin_reset_password(body: AdminResetIn, admin=Depends(require_company_admin)):
    """Manual escape hatch for vendor/candidate accounts whose self-service
    reset email silently failed to send -- mirrors the staff-only
    POST /admin/users/{id}/reset-password in admin_users.py, which has no
    vendor/candidate equivalent today."""
    if len(body.new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    table = {"vendor": "vendor_user", "candidate": "candidate_user"}.get(body.account_type)
    if not table:
        raise HTTPException(400, "account_type must be 'vendor' or 'candidate'")
    row = query_one(
        f"UPDATE {table} SET password_hash = %s WHERE id = %s AND tenant_id = %s RETURNING id",
        [hash_password(body.new_password), body.user_id, admin.get("tenant_id")],
    )
    if not row:
        raise HTTPException(404, "Account not found")
    log_activity(
        "auth", "password_admin_reset",
        entity_id=body.user_id, actor_id=admin["sub"], actor_role=admin["role"],
        detail={"account_type": body.account_type},
    )
    return {"ok": True}


# ── Public: validate token (for the set-password page) ────────────────────────

@router.get("/reset-token/validate")
def validate_token(token: str):
    row = query_one(
        """SELECT user_id, expires_at, used_at, account_type
           FROM password_reset_token WHERE token_hash=%s""",
        [_hash_token(token)],
    )
    if not row or row["used_at"] is not None:
        return {"valid": False}
    if row["expires_at"].replace(tzinfo=None) < datetime.utcnow():
        return {"valid": False}
    return {"valid": True, "account_type": row.get("account_type", "staff")}


# ── Public: submit new password ───────────────────────────────────────────────

class ResetSubmitIn(BaseModel):
    token: str
    new_password: str


@router.post("/reset-password")
def reset_password(body: ResetSubmitIn):
    if len(body.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    row = query_one(
        """SELECT id, user_id, expires_at, used_at, account_type
           FROM password_reset_token WHERE token_hash=%s""",
        [_hash_token(body.token)],
    )
    if not row or row["used_at"] is not None:
        raise HTTPException(400, "Invalid or already-used link")
    if row["expires_at"].replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(400, "This link has expired — request a new one")

    new_hash = hash_password(body.new_password)
    account_type = row.get("account_type") or "staff"

    # Update the correct user table based on the token's account_type
    if account_type == "staff":
        query(
            "UPDATE app_user SET password_hash=%s WHERE id=%s",
            [new_hash, row["user_id"]], fetch=False,
        )
    elif account_type == "vendor":
        query(
            "UPDATE vendor_user SET password_hash=%s WHERE id=%s",
            [new_hash, row["user_id"]], fetch=False,
        )
    elif account_type == "candidate":
        query(
            "UPDATE candidate_user SET password_hash=%s WHERE id=%s",
            [new_hash, row["user_id"]], fetch=False,
        )

    # Mark this token used
    query(
        "UPDATE password_reset_token SET used_at=now() WHERE id=%s",
        [row["id"]], fetch=False,
    )
    # Invalidate any other outstanding tokens for the same user + account_type
    query(
        """UPDATE password_reset_token SET used_at=now()
           WHERE user_id=%s AND account_type=%s AND used_at IS NULL""",
        [row["user_id"], account_type], fetch=False,
    )
    return {"ok": True}
