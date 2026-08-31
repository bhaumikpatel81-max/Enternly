"""
Per-recruiter module-access delegation.

A Company Admin (or the legacy admin / a Platform Admin) picks an individual
recruiter from a dropdown and toggles which otherwise Company-Admin-only
modules that ONE recruiter may use. Off by default — a recruiter has no
delegated access until a Company Admin explicitly grants it, and grants
apply only to the chosen recruiter, not the whole role. ta_manager holds
neither the delegation power nor blanket module access — it's restricted to
team management + reports.

"Users & Access" (account/role management) is intentionally never
delegable here, to prevent a recruiter from ever creating accounts or
changing roles.
"""
import json

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import query, query_one, transaction, tx_exec

DELEGABLE_MODULES = {
    "vendors":         "Vendor Management",
    "form_fields":     "Application Form Fields",
    "req_approvals":   "Requisition Approvals",
    "organisation":    "Organisation",
    "sla_settings":    "SLA Settings",
    "chain_templates": "Approval Chain Templates",
    "email_templates": "Email Templates",
}


def get_recruiter_grants(recruiter_id: str) -> dict:
    """Returns {module_key: bool} for one recruiter (defaults to False)."""
    rows = query(
        "SELECT module, enabled FROM recruiter_module_access WHERE recruiter_id = %s",
        [recruiter_id],
    ) or []
    stored = {r["module"]: bool(r["enabled"]) for r in rows}
    return {k: stored.get(k, False) for k in DELEGABLE_MODULES}


def set_recruiter_grant(recruiter_id: str, module: str, enabled: bool, granted_by) -> None:
    """
    Grant + its activity_log row commit atomically -- activity_log.py's own
    docstring names module-access grants as one of the cases that must not
    use the best-effort log_activity() helper (grant write and audit row
    were previously two independent auto-committed statements, so a DB
    hiccup between them could silently leave a grant/revoke with no audit
    trail at all).
    """
    if module not in DELEGABLE_MODULES:
        raise ValueError(f"Unknown module '{module}'")
    with transaction() as cur:
        tx_exec(
            cur,
            """INSERT INTO recruiter_module_access (recruiter_id, module, enabled, granted_by)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (recruiter_id, module) DO UPDATE
                 SET enabled = EXCLUDED.enabled, granted_by = EXCLUDED.granted_by, granted_at = now()""",
            [recruiter_id, module, enabled, granted_by],
        )
        tx_exec(
            cur,
            """INSERT INTO activity_log
                 (entity_type, action, entity_id, actor_id, to_value, detail)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
            [
                "module_access", "module_access_granted" if enabled else "module_access_revoked",
                recruiter_id, granted_by, module, json.dumps({"enabled": enabled}),
            ],
        )


def recruiter_has_module(recruiter_id: str, module: str) -> bool:
    row = query_one(
        "SELECT enabled FROM recruiter_module_access WHERE recruiter_id = %s AND module = %s",
        [recruiter_id, module],
    )
    return bool(row and row["enabled"])


def tenant_module_enabled(tenant_id, module_key: str) -> bool:
    """The platform-admin OUTER gate (Feature D): a module must be enabled
    for the tenant before ANY user of that tenant -- even a company admin --
    can use it; the per-user grants above are the INNER gate on top of that.
    Defaults to True when there's no tenant_module_config row at all (e.g. a
    module key added to the catalog after this tenant's row-per-module
    default-enable backfill ran), so a missing row can never silently lock
    a tenant out of something it was never explicitly toggled off."""
    if not tenant_id:
        return True
    row = query_one(
        "SELECT is_enabled FROM tenant_module_config WHERE tenant_id = %s AND module_key = %s",
        [tenant_id, module_key],
    )
    return True if row is None else bool(row["is_enabled"])


_bearer_optional = HTTPBearer(auto_error=False)


def require_tenant_module(module_key: str):
    """Router-level outer gate for the 9 module_catalog keys that aren't
    among the 7 DELEGABLE_MODULES (those are gated via effective_module_access
    above instead). Apply via APIRouter(..., dependencies=[Depends(
    require_tenant_module('key'))]) so it runs ahead of every route in that
    file, closing the "nav hidden client-side, API still open to a direct
    staff call" gap for a disabled module.

    Deliberately a no-op for any request that ISN'T a decodable staff Bearer
    token: several of these routers (enteri_ai_api.py, proctoring_api.py,
    campus_bulk_api.py) also serve public candidate-facing routes
    authenticated by their own invite/session token, not a staff JWT, and
    those must keep working exactly as before -- this only ever blocks a
    genuine STAFF request whose tenant has the module disabled, never a
    candidate/vendor/public request, which the route's own auth already
    validates independently."""
    def dep(creds: HTTPAuthorizationCredentials | None = Depends(_bearer_optional)):
        if not creds:
            return
        from .auth_utils import decode_staff_token
        try:
            user = decode_staff_token(creds.credentials)
        except HTTPException:
            return
        if not tenant_module_enabled(user.get("tenant_id"), module_key):
            raise HTTPException(403, f"The '{module_key}' module is disabled for your company")
    return dep


def effective_module_access(user: dict) -> dict:
    """What the CURRENT user can see, given their role/id. ta_manager is
    deliberately NOT granted blanket access here -- these are org-config
    modules (vendors, approvals, organisation, SLA, templates), and
    ta_manager is restricted to team management + reports.

    Each of the 7 keys here is additionally AND-ed with the tenant-level
    outer gate (Feature D) -- a company admin's blanket True or a
    recruiter's delegated grant only actually applies if the tenant has
    that module enabled at all."""
    role = user.get("role")
    if role in ("admin", "platform_admin", "company_admin"):
        base = {k: True for k in DELEGABLE_MODULES}
    elif role == "recruiter":
        base = get_recruiter_grants(user.get("sub"))
    else:
        base = {k: False for k in DELEGABLE_MODULES}
    tenant_id = user.get("tenant_id")
    return {k: (v and tenant_module_enabled(tenant_id, k)) for k, v in base.items()}


def all_recruiter_grants() -> dict:
    """{recruiter_id: {module_key: bool}} for every recruiter who has at least one row."""
    rows = query("SELECT recruiter_id, module, enabled FROM recruiter_module_access") or []
    out = {}
    for r in rows:
        rid = str(r["recruiter_id"])
        out.setdefault(rid, {k: False for k in DELEGABLE_MODULES})
        out[rid][r["module"]] = bool(r["enabled"])
    return out
