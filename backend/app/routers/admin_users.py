import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import (
    hash_password, require_company_admin, get_current_user,
    is_company_tier as _is_company_tier, is_platform_tier as _is_platform_tier,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

_VALID_ROLES = {
    "platform_admin", "company_admin",
    "admin", "ta_manager", "recruiter",
    "hiring_manager", "bu_head", "director", "interviewer", "hrbp",
    "placement_officer",  # College-tenant campus recruiting contact -- created only via
                          # POST /api/platform/tenants/{id}/placement-officers, not this router
}

# Roles a Company Admin (non-platform) may not create/promote-to or act on --
# reserved for a Platform Admin (Enternstech) or the legacy admin role.
_PLATFORM_ONLY_ROLES = {"admin", "platform_admin", "company_admin"}


# _is_company_tier/_is_platform_tier are now shared helpers imported from
# auth_utils.py (moved there in the Step 1 full audit, finding #3, so every
# router migrated off the retired platform_admin/company_admin role-string
# pattern reuses the same one definition instead of each having its own
# copy) -- kept as local aliases here so the many existing call sites below
# don't need touching.

_USER_COLS = """id, full_name, email, role, is_active, created_at, gmail_address,
    (SELECT COALESCE(array_agg(bu_id), ARRAY[]::uuid[]) FROM app_user_bu WHERE user_id = app_user.id) AS bu_ids,
    (SELECT full_name FROM app_user creator WHERE creator.id = app_user.created_by) AS created_by_name"""


def require_users_read(user: dict = Depends(get_current_user)) -> dict:
    """Company Admin gets full Users & Access; TA Manager gets a read-only
    view of their company's users (team management, not account management);
    Recruiters get a read-only Hiring Manager list plus the ability to create
    HM accounts (see create_user's recruiter branch below)."""
    if not (_is_company_tier(user) or user.get("role") in ("ta_manager", "recruiter")):
        raise HTTPException(403, "Company Admin, TA Manager, or Recruiter access required")
    return user


def require_user_write(user: dict = Depends(get_current_user)) -> dict:
    """Creating/editing staff accounts is a Company Admin action. TA Manager
    is deliberately excluded -- restricted to managing their team + reports,
    not the accounts themselves. Recruiters keep their narrower carve-out
    (may only create Hiring Manager accounts, enforced in create_user)."""
    if not (_is_company_tier(user) or user.get("role") == "recruiter"):
        raise HTTPException(403, "Company Admin or Recruiter access required")
    return user


def _set_hrbp_bus(user_id: str, bu_ids: List[str]) -> None:
    """Replace an HRBP's home-BU set (visibility fallback) wholesale."""
    query("DELETE FROM app_user_bu WHERE user_id = %s", [user_id], fetch=False)
    for bu_id in dict.fromkeys(bu_ids):  # de-dupe, keep order
        if bu_id:
            query(
                "INSERT INTO app_user_bu (user_id, bu_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                [user_id, bu_id],
                fetch=False,
            )


def _sync_hrbp_directory(full_name: str, email: str, role: str, is_active: bool, old_email: Optional[str] = None) -> None:
    """Keep the standalone `hrbp` lookup table (the requisition create/edit
    dropdown, see hrbp_api.py list_hrbp()) in sync with app_user accounts that
    hold the hrbp role. The two are separate tables -- creating/editing an
    app_user alone never touched `hrbp`, which is why new HRBP users didn't
    show up there."""
    if old_email and old_email.lower() != email.lower():
        query("UPDATE hrbp SET is_active = FALSE WHERE LOWER(email) = LOWER(%s)", [old_email], fetch=False)
    if role == "hrbp" and is_active:
        query(
            """INSERT INTO hrbp (full_name, email, is_active)
               VALUES (%s, %s, TRUE)
               ON CONFLICT (email) DO UPDATE
                 SET full_name = EXCLUDED.full_name, is_active = TRUE""",
            [full_name, email],
            fetch=False,
        )
    else:
        query("UPDATE hrbp SET is_active = FALSE WHERE LOWER(email) = LOWER(%s)", [email], fetch=False)


def _assert_can_assign_role(actor: dict, target_role: Optional[str]) -> None:
    """Only a Platform Admin (or the legacy admin role) may create or promote
    a user into admin/platform_admin/company_admin -- a Company Admin may
    manage TA Managers and Recruiters in their own company, but not mint
    another admin tier."""
    if target_role in _PLATFORM_ONLY_ROLES and not _is_platform_tier(actor):
        raise HTTPException(403, "Only a Platform Admin can assign that role")


def _assert_can_act_on_user(actor: dict, target_user_id: str) -> None:
    """A Company Admin (non-platform) may not modify, deactivate, delete, or
    reset the password of an account that holds admin/platform_admin/
    company_admin -- those accounts are a Platform Admin's to manage. Also
    blocks acting on another tenant's user entirely -- a cross-tenant target
    reads as 404, same as a nonexistent one, so no existence is leaked.
    admin/platform_admin are intentionally exempt from the tenant check
    (Platform Admin cross-tenant support access doesn't have its own
    audited path yet -- see the tenancy blueprint)."""
    if _is_platform_tier(actor):
        return
    target = query_one("SELECT role, tenant_id FROM app_user WHERE id = %s", [target_user_id])
    if not target:
        return
    if str(target.get("tenant_id")) != str(actor.get("tenant_id")):
        raise HTTPException(404, "User not found")
    if target.get("role") in _PLATFORM_ONLY_ROLES:
        raise HTTPException(403, "Only a Platform Admin can modify that account")


class CreateUserIn(BaseModel):
    full_name: str
    email: str
    role: str
    bu_ids: List[str] = []                # HRBP-login home BUs (used as the visibility fallback)
    password: Optional[str] = None       # if omitted, user sets it via emailed link
    send_setup_email: bool = True


class UpdateUserIn(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class ResetPasswordIn(BaseModel):
    new_password: str


@router.get("/users")
def list_users(
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    actor=Depends(require_users_read),
):
    conds = ["tenant_id = %s"]
    params = [actor.get("tenant_id")]
    if actor.get("role") == "recruiter":
        conds.append("role = 'hiring_manager'")
    where = "WHERE " + " AND ".join(conds)
    return query(
        f"SELECT {_USER_COLS} FROM app_user {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
        params + [limit, offset],
    )


@router.post("/users", status_code=201)
def create_user(body: CreateUserIn, actor=Depends(require_user_write)):
    if actor.get("role") == "recruiter" and body.role != "hiring_manager":
        raise HTTPException(403, "Recruiters may only create Hiring Manager accounts")
    if body.role not in _VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Choose from: {sorted(_VALID_ROLES)}")
    _assert_can_assign_role(actor, body.role)
    if body.password and len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    from ..services.email_validation import assert_real_email
    try:
        email = assert_real_email(body.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if query_one("SELECT id FROM app_user WHERE email = %s", [email]):
        raise HTTPException(400, "A user with that email already exists")
    pwd_hash = hash_password(body.password) if body.password else None
    new_id = query_one(
        """INSERT INTO app_user (full_name, email, role, password_hash, created_by, tenant_id)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        [body.full_name, email, body.role, pwd_hash, actor.get("sub"), actor.get("tenant_id")],
    )["id"]
    if body.bu_ids:
        _set_hrbp_bus(new_id, body.bu_ids)
    _sync_hrbp_directory(body.full_name, email, body.role, True)
    row = query_one(f"SELECT {_USER_COLS} FROM app_user WHERE id = %s", [new_id])
    # Auto-send first-time set-password email for all staff roles
    setup_email_sent = False
    if body.send_setup_email:
        try:
            from .password_api import _issue_token, _send_link_email
            raw = _issue_token(str(row["id"]), "invite")
            _send_link_email(row["email"], row["full_name"], raw, "invite", tenant_id=actor.get("tenant_id"))
            setup_email_sent = True
        except Exception as exc:
            print(f"[create_user] setup email failed: {exc}")
    # The account itself is created either way -- setup_email_sent=False just
    # tells the caller to warn instead of claiming the invite went out.
    return {**row, "setup_email_sent": setup_email_sent, "setup_email_requested": body.send_setup_email}


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UpdateUserIn, admin=Depends(require_company_admin)):
    if body.role and body.role not in _VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Choose from: {sorted(_VALID_ROLES)}")
    _assert_can_act_on_user(admin, user_id)
    _assert_can_assign_role(admin, body.role)
    sets, params = [], []
    if body.full_name is not None:
        sets.append("full_name = %s"); params.append(body.full_name)
    if body.role is not None:
        sets.append("role = %s"); params.append(body.role)
    if body.is_active is not None:
        sets.append("is_active = %s"); params.append(body.is_active)
    if not sets:
        raise HTTPException(400, "Nothing to update")
    if body.role is not None or body.is_active is not None:
        # Invalidate any JWT already issued to this user -- a role/active
        # change must take effect immediately, not wait out the token's
        # remaining TOKEN_HOURS (see auth_utils._refresh_staff_claims).
        sets.append("token_version = token_version + 1")
    params.append(user_id)
    row = query_one(
        f"UPDATE app_user SET {', '.join(sets)} WHERE id = %s RETURNING {_USER_COLS}",
        params,
    )
    if not row:
        raise HTTPException(404, "User not found")
    _sync_hrbp_directory(row["full_name"], row["email"], row["role"], row["is_active"])
    return row


class UpdateUserFullIn(BaseModel):
    full_name:  Optional[str] = None
    email:      Optional[str] = None
    role:       Optional[str] = None
    is_active:  Optional[bool] = None
    bu_ids:     Optional[List[str]] = None


@router.patch("/users/{user_id}/full")
def update_user_full(user_id: str, body: UpdateUserFullIn, admin=Depends(require_company_admin)):
    """Extended PATCH that also allows updating email and resetting the user's login email."""
    if body.role and body.role not in _VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Choose from: {sorted(_VALID_ROLES)}")
    _assert_can_act_on_user(admin, user_id)
    _assert_can_assign_role(admin, body.role)
    if body.email:
        conflict = query_one(
            "SELECT id FROM app_user WHERE LOWER(email)=LOWER(%s) AND id != %s",
            [body.email, user_id],
        )
        if conflict:
            raise HTTPException(400, "That email is already used by another user.")
    sets, params = [], []
    if body.full_name is not None:
        sets.append("full_name = %s"); params.append(body.full_name)
    if body.email is not None:
        sets.append("email = %s"); params.append(body.email.lower().strip())
    if body.role is not None:
        sets.append("role = %s"); params.append(body.role)
    if body.is_active is not None:
        sets.append("is_active = %s"); params.append(body.is_active)
    if not sets and body.bu_ids is None:
        raise HTTPException(400, "Nothing to update")
    if body.role is not None or body.is_active is not None:
        sets.append("token_version = token_version + 1")
    existing = query_one("SELECT email FROM app_user WHERE id = %s", [user_id])
    if not existing:
        raise HTTPException(404, "User not found")
    if body.bu_ids is not None:
        _set_hrbp_bus(user_id, body.bu_ids)
    if sets:
        params.append(user_id)
        row = query_one(f"UPDATE app_user SET {', '.join(sets)} WHERE id = %s RETURNING {_USER_COLS}", params)
    else:
        row = query_one(f"SELECT {_USER_COLS} FROM app_user WHERE id = %s", [user_id])
    _sync_hrbp_directory(row["full_name"], row["email"], row["role"], row["is_active"], old_email=existing["email"])
    return row


# ── Per-user Gmail / App Password (for individual email scanning) ─────────────

class UserEmailConfigIn(BaseModel):
    gmail_address:      Optional[str] = None
    gmail_app_password: Optional[str] = None   # 16-char Gmail App Password; None = keep current


@router.get("/users/{user_id}/email-config")
def get_user_email_config(user_id: str, admin=Depends(require_company_admin)):
    _assert_can_act_on_user(admin, user_id)
    row = query_one(
        "SELECT gmail_address, gmail_app_password FROM app_user WHERE id = %s",
        [user_id],
    )
    if not row:
        raise HTTPException(404, "User not found")
    has_pw = bool(row.get("gmail_app_password"))
    return {
        "gmail_address":      row.get("gmail_address") or "",
        "app_password_set":   has_pw,
        "app_password_hint":  "••••••••" if has_pw else "",
    }


@router.put("/users/{user_id}/email-config")
def set_user_email_config(user_id: str, body: UserEmailConfigIn, admin=Depends(require_company_admin)):
    _assert_can_act_on_user(admin, user_id)
    sets, params = [], []
    if body.gmail_address is not None:
        sets.append("gmail_address = %s")
        params.append(body.gmail_address.lower().strip() if body.gmail_address else None)
    if body.gmail_app_password and body.gmail_app_password not in ("", "••••••••"):
        cleaned = body.gmail_app_password.replace(" ", "")
        if len(cleaned) not in (16, 19):
            raise HTTPException(400, "App Password must be 16 characters (spaces ignored).")
        sets.append("gmail_app_password = %s")
        params.append(cleaned)
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(user_id)
    row = query_one(
        f"UPDATE app_user SET {', '.join(sets)} WHERE id = %s RETURNING id, gmail_address",
        params,
    )
    if not row:
        raise HTTPException(404, "User not found")
    return {"ok": True, "gmail_address": row.get("gmail_address") or ""}


@router.delete("/users/{user_id}/email-config")
def clear_user_email_config(user_id: str, admin=Depends(require_company_admin)):
    _assert_can_act_on_user(admin, user_id)
    row = query_one(
        "UPDATE app_user SET gmail_address=NULL, gmail_app_password=NULL WHERE id=%s RETURNING id",
        [user_id],
    )
    if not row:
        raise HTTPException(404, "User not found")
    return {"ok": True}


@router.delete("/users/{user_id}")
def deactivate_user(user_id: str, admin=Depends(require_company_admin)):
    _assert_can_act_on_user(admin, user_id)
    row = query_one(
        "UPDATE app_user SET is_active = false, token_version = token_version + 1 "
        "WHERE id = %s RETURNING id, full_name, email, role",
        [user_id],
    )
    if not row:
        raise HTTPException(404, "User not found")
    _sync_hrbp_directory(row["full_name"], row["email"], row["role"], False)
    return {"deactivated": True}


@router.delete("/users/{user_id}/permanent")
def delete_user_permanent(user_id: str, admin=Depends(require_company_admin)):
    """
    Permanently remove a user record.
    Fails with 409 if the user owns records (requisitions, scorecards, etc.)
    that cannot be orphaned — deactivate instead of deleting in that case.
    Self-delete is blocked.
    """
    if str(admin.get("sub")) == str(user_id):
        raise HTTPException(400, "You cannot delete your own account.")
    _assert_can_act_on_user(admin, user_id)
    row = query_one("SELECT id, full_name, email, role FROM app_user WHERE id = %s", [user_id])
    if not row:
        raise HTTPException(404, "User not found")
    import psycopg2
    try:
        query("DELETE FROM app_user WHERE id = %s", [user_id], fetch=False)
    except psycopg2.errors.ForeignKeyViolation:
        raise HTTPException(
            409,
            f"Cannot delete '{row['full_name']}' — they have associated records "
            "(requisitions, interviews, scorecards, etc.). "
            "Deactivate the account instead to preserve history."
        )
    except Exception as exc:
        raise HTTPException(500, f"Delete failed: {exc}")
    _sync_hrbp_directory(row["full_name"], row["email"], row["role"], False)
    return {"deleted": True, "full_name": row["full_name"]}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: str, body: ResetPasswordIn, admin=Depends(require_company_admin)):
    if len(body.new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    _assert_can_act_on_user(admin, user_id)
    row = query_one(
        "UPDATE app_user SET password_hash = %s, token_version = token_version + 1 "
        "WHERE id = %s RETURNING id",
        [hash_password(body.new_password), user_id],
    )
    if not row:
        raise HTTPException(404, "User not found")
    return {"ok": True}


# ── System Settings (Company Admin) ───────────────────────────────────────────

# Keys that hold sensitive values — shown masked in GET response
_SECRET_KEYS = {"smtp_password"}

# All recognised setting keys with their defaults
_SETTING_DEFAULTS = {
    "smtp_user":           "",
    "smtp_password":       "",
    "smtp_host":           "smtp.gmail.com",
    "smtp_port":           "587",
    "smtp_from_name":      "Enternly Hiring",
    "app_base_url":        "http://localhost:8000",
    "about_company_text":  "About EnternsTech: [Configure in Settings]",
    "auto_jd_email":       "true",
    "company_name":        "EnternsTech Pvt. Ltd.",
    "ta_default_signature": "Talent Acquisition Team",
}


def _require_settings_access(user: dict = Depends(get_current_user)) -> dict:
    if not _is_company_tier(user):
        raise HTTPException(403, "Company Admin access required")
    return user


# ── Per-tenant role labels ─────────────────────────────────────────────────────
# The role VALUE ('recruiter', 'bu_head', 'director', 'hrbp', ...) is a fixed
# system key that every permission check and report in the codebase keys off
# of -- it never changes. What a company CALLS that role in their own org is
# a separate, tenant-owned label layered on top, because every customer uses
# different job titles for the same underlying job (one company's
# "Recruiter" is another's "Talent Partner"; "BU Head" might be "Department
# Head" or "Vertical Lead" elsewhere). platform_admin/company_admin/admin are
# structural to the SaaS itself, not a customer's job-title vocabulary, so
# they're deliberately not included here.
_ROLE_LABEL_DEFAULTS = {
    "ta_manager":     "TA Manager",
    "recruiter":      "Recruiter",
    "hiring_manager": "Hiring Manager",
    "bu_head":        "BU Head",
    "director":       "Director",
    "interviewer":    "Interviewer",
    "hrbp":           "HRBP",
}


@router.get("/role-labels")
def get_role_labels(user: dict = Depends(get_current_user)):
    """Effective label set for the caller's own tenant (defaults merged with
    any overrides) -- every staff screen renders role names through this
    instead of the raw role key."""
    tenant_id = user.get("tenant_id")
    overrides = {}
    if tenant_id:
        row = query_one("SELECT role_labels FROM tenant WHERE id = %s", [tenant_id])
        overrides = (row or {}).get("role_labels") or {}
    return {**_ROLE_LABEL_DEFAULTS, **{k: v for k, v in overrides.items() if k in _ROLE_LABEL_DEFAULTS}}


class RoleLabelsIn(BaseModel):
    labels: dict[str, str]


@router.put("/role-labels")
def save_role_labels(body: RoleLabelsIn, user: dict = Depends(require_company_admin)):
    tenant_id = user.get("tenant_id")
    if not tenant_id:
        raise HTTPException(400, "No tenant on this account")
    cleaned = {
        k: v.strip() for k, v in body.labels.items()
        if k in _ROLE_LABEL_DEFAULTS and v and v.strip()
    }
    query(
        "UPDATE tenant SET role_labels = %s::jsonb WHERE id = %s",
        [json.dumps(cleaned), tenant_id],
        fetch=False,
    )
    return {**_ROLE_LABEL_DEFAULTS, **cleaned}


def _require_admin_settings(user: dict = Depends(get_current_user)) -> dict:
    if not _is_company_tier(user):
        raise HTTPException(403, "Company Admin access required")
    return user


# ── Per-recruiter module-access delegation (Company Admin controlled) ───────
from ..module_access import (
    DELEGABLE_MODULES,
    client_module_status,
    effective_module_access,
    get_recruiter_grants,
    recruiter_has_module,
    set_recruiter_grant,
)


def _require_module_access(module_key: str):
    def dep(user: dict = Depends(get_current_user)) -> dict:
        if _is_company_tier(user):
            return user
        if user.get("role") == "recruiter" and recruiter_has_module(user.get("sub"), module_key):
            return user
        raise HTTPException(403, "Company Admin or delegated Recruiter access required")
    return dep


_require_form_field_access = _require_module_access("form_fields")


@router.get("/module-access/recruiters")
def list_recruiters_for_delegation(user: dict = Depends(_require_settings_access)):
    """Recruiters to populate the TA Manager's delegation dropdown."""
    return query(
        "SELECT id, full_name, email FROM app_user WHERE role = 'recruiter' AND is_active = TRUE AND tenant_id = %s ORDER BY full_name",
        [user.get("tenant_id")],
    )


@router.get("/module-access/{recruiter_id}")
def get_module_access(recruiter_id: str, user: dict = Depends(_require_settings_access)):
    """Delegation state for ONE recruiter — for the TA Manager / admin toggle panel."""
    if not query_one(
        "SELECT id FROM app_user WHERE id = %s AND role = 'recruiter' AND tenant_id = %s",
        [recruiter_id, user.get("tenant_id")],
    ):
        raise HTTPException(404, "Recruiter not found")
    grants = get_recruiter_grants(recruiter_id)
    return {
        "modules": [
            {"key": k, "label": label, "enabled": grants[k]}
            for k, label in DELEGABLE_MODULES.items()
        ]
    }


class ModuleAccessIn(BaseModel):
    module: str
    enabled: bool


@router.post("/module-access/{recruiter_id}")
def save_module_access(
    recruiter_id: str, body: ModuleAccessIn, user: dict = Depends(_require_settings_access)
):
    if body.module not in DELEGABLE_MODULES:
        raise HTTPException(400, f"Unknown module. Choose from: {sorted(DELEGABLE_MODULES)}")
    if not query_one(
        "SELECT id FROM app_user WHERE id = %s AND role = 'recruiter' AND tenant_id = %s",
        [recruiter_id, user.get("tenant_id")],
    ):
        raise HTTPException(404, "Recruiter not found")
    set_recruiter_grant(recruiter_id, body.module, body.enabled, user["sub"])
    return {"ok": True, "module": body.module, "enabled": body.enabled}


@router.get("/my-module-access")
def get_my_module_access(user: dict = Depends(get_current_user)):
    """What modules the CURRENT user can see — used to build nav dynamically.
    Returns the 7 DELEGABLE_MODULES keys (per-user-aware) plus the 9
    GATED_NAV_MODULES keys (pure tenant-level status) in one flat dict --
    finding #2's nav-hiding relies on this same call, made for every role
    now, not just recruiters (see index.html's boot())."""
    return client_module_status(user)


@router.get("/settings")
def get_settings(user: dict = Depends(_require_admin_settings)):
    rows = query("SELECT key, value, updated_at FROM system_settings WHERE tenant_id = %s", [user.get("tenant_id")])
    stored = {r["key"]: r["value"] for r in (rows or [])}
    result = {}
    for k, default in _SETTING_DEFAULTS.items():
        val = stored.get(k, default)
        result[k] = "••••••••" if k in _SECRET_KEYS and val else val
    result["smtp_password_set"] = bool(stored.get("smtp_password", ""))
    return result


class SaveSettingsIn(BaseModel):
    smtp_user:           Optional[str] = None
    smtp_password:       Optional[str] = None
    smtp_host:           Optional[str] = None
    smtp_port:           Optional[str] = None
    smtp_from_name:      Optional[str] = None
    app_base_url:        Optional[str] = None
    about_company_text:  Optional[str] = None
    auto_jd_email:       Optional[str] = None
    company_name:        Optional[str] = None
    ta_default_signature: Optional[str] = None


@router.post("/settings")
def save_settings(body: SaveSettingsIn, user: dict = Depends(_require_admin_settings)):
    updates = {
        "smtp_user":           body.smtp_user,
        "smtp_host":           body.smtp_host,
        "smtp_port":           body.smtp_port,
        "smtp_from_name":      body.smtp_from_name,
        "app_base_url":        body.app_base_url,
        "about_company_text":  body.about_company_text,
        "auto_jd_email":       body.auto_jd_email,
        "company_name":        body.company_name,
        "ta_default_signature": body.ta_default_signature,
    }
    if body.smtp_password and body.smtp_password not in ("", "••••••••"):
        updates["smtp_password"] = body.smtp_password

    tenant_id = user.get("tenant_id")
    for k, v in updates.items():
        if v is None:
            continue
        query(
            """INSERT INTO system_settings (tenant_id, key, value, updated_by)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, key) DO UPDATE
                 SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
            [tenant_id, k, v.strip(), user["sub"]],
            fetch=False,
        )
    return {"ok": True}


# ── Application Form Field Config (Company Admin, or delegated recruiter) ────

_FORM_CFG_KEY = "app_form_required_fields"
_DEFAULT_REQUIRED_FIELDS = ["name", "email", "phone", "requisition", "resume"]


class FormFieldConfigIn(BaseModel):
    required_fields: list[str]


@router.get("/form-field-config")
def get_form_field_config(user: dict = Depends(_require_form_field_access)):
    """Return which application form fields are currently marked required."""
    row = query_one(
        "SELECT value FROM system_settings WHERE tenant_id = %s AND key = %s",
        [user.get("tenant_id"), _FORM_CFG_KEY],
    )
    if row:
        try:
            required = json.loads(row["value"])
        except Exception:
            required = _DEFAULT_REQUIRED_FIELDS
    else:
        required = _DEFAULT_REQUIRED_FIELDS
    return {"required_fields": required}


@router.post("/form-field-config")
def save_form_field_config(
    body: FormFieldConfigIn, user: dict = Depends(_require_form_field_access)
):
    """Persist the list of required application form fields."""
    query(
        """INSERT INTO system_settings (tenant_id, key, value, updated_by)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (tenant_id, key) DO UPDATE
             SET value      = EXCLUDED.value,
                 updated_at = now(),
                 updated_by = EXCLUDED.updated_by""",
        [user.get("tenant_id"), _FORM_CFG_KEY, json.dumps(body.required_fields), user["sub"]],
        fetch=False,
    )
    return {"ok": True}


@router.post("/settings/test-email")
async def test_email(user: dict = Depends(_require_admin_settings)):
    """
    Verify SMTP credentials and send a test email.
    Runs async with a hard 10-second timeout so the browser never hangs.
    """
    import asyncio, smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    # Read all settings directly from DB
    rows = query("SELECT key, value FROM system_settings WHERE tenant_id = %s", [user.get("tenant_id")])
    cfg  = {r["key"]: (r["value"] or "").strip() for r in (rows or [])}

    smtp_user = cfg.get("smtp_user", "")
    smtp_pass = cfg.get("smtp_password", "").replace(" ", "")
    smtp_host = cfg.get("smtp_host", "smtp.gmail.com") or "smtp.gmail.com"
    smtp_port = int(cfg.get("smtp_port", "587") or "587")
    from_name = cfg.get("smtp_from_name", "Enternly Hiring") or "Enternly Hiring"
    from_email = smtp_user or "noreply@your-enternly-domain.example"

    if not smtp_user:
        raise HTTPException(400,
            "No email method configured. "
            "Add Gmail SMTP credentials in Settings."
        )

    subject   = "Enternly — Email configuration test"
    body_text = (
        "This test confirms your Enternly email is working. "
        "AI interview invites will be delivered automatically to candidates."
    )
    to_addr   = from_email

    # ── SMTP ─────────────────────────────────────────────────────────────────
    if not smtp_pass:
        raise HTTPException(400, "SMTP password is empty — enter your App Password and save.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{from_name} <{smtp_user}>"
    msg["To"]      = smtp_user
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    def _do_smtp():
        errs = []
        for use_ssl, port in [(False, smtp_port), (True, 465)]:
            try:
                if use_ssl:
                    conn = smtplib.SMTP_SSL(smtp_host, port, timeout=5)
                else:
                    conn = smtplib.SMTP(smtp_host, port, timeout=5)
                with conn as s:
                    s.ehlo()
                    if not use_ssl:
                        s.starttls(); s.ehlo()
                    s.login(smtp_user, smtp_pass)
                    s.sendmail(smtp_user, [smtp_user], msg.as_string())
                return ("ok", f"SMTP {'SSL' if use_ssl else 'TLS'} :{port}")
            except smtplib.SMTPAuthenticationError as e:
                return ("auth", str(e))
            except Exception as e:
                errs.append(f"port {port}: {e}")
        return ("fail", " | ".join(errs))

    loop = asyncio.get_event_loop()
    try:
        status, detail = await asyncio.wait_for(
            loop.run_in_executor(None, _do_smtp), timeout=15
        )
    except asyncio.TimeoutError:
        raise HTTPException(400,
            "SMTP timed out — your network is blocking outbound email ports."
        )

    if status == "ok":
        return {"ok": True, "sent_to": smtp_user, "method": detail}
    if status == "auth":
        raise HTTPException(400,
            "Gmail rejected the App Password.\n"
            "1. Go to myaccount.google.com → Security\n"
            "2. Confirm 2-Step Verification is ON\n"
            "3. Search 'App passwords' → create one named Enternly\n"
            "4. Copy the 16 chars → paste into App password field → Save"
        )
    raise HTTPException(400,
        f"Cannot connect via SMTP ({detail}). "
        "Your network is blocking outbound email ports."
    )
