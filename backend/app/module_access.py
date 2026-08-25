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


def effective_module_access(user: dict) -> dict:
    """What the CURRENT user can see, given their role/id. ta_manager is
    deliberately NOT granted blanket access here -- these are org-config
    modules (vendors, approvals, organisation, SLA, templates), and
    ta_manager is restricted to team management + reports."""
    role = user.get("role")
    if role in ("admin", "platform_admin", "company_admin"):
        return {k: True for k in DELEGABLE_MODULES}
    if role == "recruiter":
        return get_recruiter_grants(user.get("sub"))
    return {k: False for k in DELEGABLE_MODULES}


def all_recruiter_grants() -> dict:
    """{recruiter_id: {module_key: bool}} for every recruiter who has at least one row."""
    rows = query("SELECT recruiter_id, module, enabled FROM recruiter_module_access") or []
    out = {}
    for r in rows:
        rid = str(r["recruiter_id"])
        out.setdefault(rid, {k: False for k in DELEGABLE_MODULES})
        out[rid][r["module"]] = bool(r["enabled"])
    return out
