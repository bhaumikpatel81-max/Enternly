"""
HRMS Multi-Provider Integration Layer API (ATS spec §14).

Pushes employee_master (Module 4) + verified documents (Module 1) into
whichever external HRMS a tenant configures -- see
services/hrms_connectors.py for the per-provider stub/real adapters
(successfactors, workday, oracle_hcm, darwinbox, zoho_people, bamboohr,
greythr). The vendor's own webhook lands at
POST /api/integrations/hrms/webhooks/{provider} -- it carries no JWT (an
external system calls it), so it's listed in main.py's _PUBLIC_PREFIXES
and authenticates itself via an HMAC signature checked against a
per-tenant secret in system_settings, with the tenant resolved from the
stored hrms_sync row (looked up by external_ref), never from the request.

Gated tenant-wide via require_tenant_module -- no per-recruiter delegation
concept, mirroring document_api.py/bgv_api.py and the other
GATED_NAV_MODULES routers. require_tenant_module is a documented no-op for
any request without a decodable staff Bearer token, so mixing the vendor
webhook into this same router (which carries that dependency) is safe --
the webhook has no Authorization header at all.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth_utils import get_current_user, is_company_tier
from ..db import query, query_one
from ..module_access import require_tenant_module
from ..services import hrms_connectors
from ..services.activity_log import log_activity

router = APIRouter(prefix="/api/integrations/hrms", tags=["hrms"],
                    dependencies=[Depends(require_tenant_module("hrms"))])

_PROVIDERS = ("successfactors", "workday", "oracle_hcm", "darwinbox", "zoho_people", "bamboohr", "greythr")


def _require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not is_company_tier(user):
        raise HTTPException(403, "Company Admin access required")
    return user


def _set_setting(tenant_id, key: str, value: str, user_id) -> None:
    query(
        """INSERT INTO system_settings (tenant_id, key, value, updated_by)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (tenant_id, key) DO UPDATE
             SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
        [tenant_id, key, value, user_id], fetch=False,
    )


# ── Provider configuration (admin-tier; credentials never stored here) ──

@router.get("/providers")
def list_providers(user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    rows = query(
        "SELECT provider, is_enabled, field_mapping FROM hrms_provider_config WHERE tenant_id=%s",
        [tenant_id],
    ) or []
    by_provider = {r["provider"]: r for r in rows}
    return {"providers": [
        {
            "provider": p,
            "configured": p in by_provider,
            "is_enabled": by_provider.get(p, {}).get("is_enabled", False),
            "field_mapping": by_provider.get(p, {}).get("field_mapping", {}),
        }
        for p in _PROVIDERS
    ]}


class ConfigureProviderIn(BaseModel):
    is_enabled: bool = True
    field_mapping: dict = {}
    base_url: Optional[str] = None
    api_key: Optional[str] = None


@router.post("/{provider}/configure")
def configure_provider(provider: str, body: ConfigureProviderIn, user: dict = Depends(_require_admin)):
    if provider not in _PROVIDERS:
        raise HTTPException(400, f"Unknown provider '{provider}'")
    tenant_id = user.get("tenant_id")

    row = query_one(
        """INSERT INTO hrms_provider_config (tenant_id, provider, is_enabled, field_mapping)
           VALUES (%s,%s,%s,%s::jsonb)
           ON CONFLICT (tenant_id, provider) DO UPDATE
             SET is_enabled = EXCLUDED.is_enabled, field_mapping = EXCLUDED.field_mapping
           RETURNING id, provider, is_enabled, field_mapping""",
        [tenant_id, provider, body.is_enabled, json.dumps(body.field_mapping)],
    )

    # Credentials go to system_settings, never into hrms_provider_config.
    if body.base_url:
        _set_setting(tenant_id, f"hrms_{provider}_base", body.base_url, user["sub"])
    if body.api_key:
        _set_setting(tenant_id, f"hrms_{provider}_key", body.api_key, user["sub"])

    log_activity("hrms_provider_config", "hrms_provider_configured",
                 entity_id=row["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"provider": provider, "is_enabled": body.is_enabled})
    return row


# ── Sync ──────────────────────────────────────────────────────────────

@router.post("/{provider}/sync/{candidate_id}")
def sync_candidate_to_hrms(provider: str, candidate_id: str, user: dict = Depends(get_current_user)):
    if provider not in _PROVIDERS:
        raise HTTPException(400, f"Unknown provider '{provider}'")
    tenant_id = user.get("tenant_id")
    if not query_one("SELECT id FROM candidate WHERE id=%s AND tenant_id=%s", [candidate_id, tenant_id]):
        raise HTTPException(404, "Candidate not found")

    config = query_one(
        "SELECT is_enabled, field_mapping FROM hrms_provider_config WHERE tenant_id=%s AND provider=%s",
        [tenant_id, provider],
    )
    if not config or not config["is_enabled"]:
        raise HTTPException(409, f"Provider '{provider}' is not enabled for this tenant")

    employee = query_one(
        """SELECT id, tenant_id, candidate_id, application_id, employee_code, designation, department_id,
                  manager_id, location, grade, cost_center, joining_date, status
           FROM employee_master WHERE candidate_id=%s AND tenant_id=%s""",
        [candidate_id, tenant_id],
    )
    if not employee:
        raise HTTPException(409, "No employee record exists for this candidate yet — convert to employee first")

    documents = query(
        "SELECT doc_type, file_name, status FROM candidate_document WHERE candidate_id=%s AND tenant_id=%s AND status='verified'",
        [candidate_id, tenant_id],
    ) or []

    employee_dict = dict(employee)
    sync_row = query_one(
        """INSERT INTO hrms_sync (tenant_id, provider, candidate_id, employee_master_id, status, request_payload, synced_by)
           VALUES (%s,%s,%s,%s,'in_progress',%s::jsonb,%s)
           RETURNING id""",
        [tenant_id, provider, candidate_id, employee["id"], json.dumps(employee_dict, default=str), user["sub"]],
    )

    try:
        result = hrms_connectors.sync_to_hrms(provider, employee_dict, [dict(d) for d in documents], config["field_mapping"] or {})
    except RuntimeError as exc:
        query(
            "UPDATE hrms_sync SET status='failed', error=%s, completed_at=now() WHERE id=%s",
            [str(exc)[:500], sync_row["id"]], fetch=False,
        )
        log_activity("hrms_sync", "hrms_sync_failed",
                     entity_id=sync_row["id"], actor_id=user["sub"], actor_role=user.get("role"),
                     detail={"provider": provider, "candidate_id": candidate_id, "error": str(exc)[:300]})
        raise HTTPException(502, f"Could not sync to {provider}: {exc}")

    external_ref = result.get("external_ref")
    status = result.get("status", "in_progress")
    query("UPDATE hrms_sync SET status=%s, external_ref=%s WHERE id=%s", [status, external_ref, sync_row["id"]], fetch=False)

    log_activity("hrms_sync", "hrms_sync_initiated",
                 entity_id=sync_row["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"provider": provider, "candidate_id": candidate_id, "external_ref": external_ref})

    return {"id": str(sync_row["id"]), "provider": provider, "external_ref": external_ref, "status": status}


@router.get("/{provider}/sync/{candidate_id}/status")
def get_sync_status(provider: str, candidate_id: str, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    row = query_one(
        """SELECT id, provider, external_ref, status, response_summary, error, created_at, completed_at
           FROM hrms_sync WHERE candidate_id=%s AND tenant_id=%s AND provider=%s
           ORDER BY created_at DESC LIMIT 1""",
        [candidate_id, tenant_id, provider],
    )
    if not row:
        raise HTTPException(404, "No sync record found for this candidate/provider")
    return {**row, "id": str(row["id"])}


# ── Inbound vendor webhook ───────────────────────────────────────────────

@router.post("/webhooks/{provider}")
async def hrms_webhook(provider: str, request: Request):
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON payload")

    parsed = hrms_connectors.parse_hrms_webhook(provider, payload)
    external_ref = parsed.get("external_ref")
    if not external_ref:
        raise HTTPException(400, "Payload missing a sync reference")

    sync_row = query_one(
        "SELECT id, tenant_id FROM hrms_sync WHERE external_ref=%s AND provider=%s",
        [external_ref, provider],
    )
    if not sync_row:
        raise HTTPException(404, "Unknown HRMS sync record")

    # Fail closed: no secret configured means no signature can ever verify.
    secret = hrms_connectors.get_webhook_secret(sync_row["tenant_id"], provider)
    signature = request.headers.get("X-Signature", "")
    if not secret or not hrms_connectors.verify_webhook_signature(secret, raw_body, signature):
        raise HTTPException(401, "Invalid signature")

    status = parsed.get("status", "in_progress")
    if status in ("success", "failed"):
        query(
            "UPDATE hrms_sync SET status=%s, response_summary=%s, error=%s, completed_at=now() WHERE id=%s",
            [status, parsed.get("response_summary"), parsed.get("error"), sync_row["id"]], fetch=False,
        )
    else:
        query(
            "UPDATE hrms_sync SET status=%s, response_summary=COALESCE(%s, response_summary) WHERE id=%s",
            [status, parsed.get("response_summary"), sync_row["id"]], fetch=False,
        )

    log_activity("hrms_sync", "hrms_webhook_received",
                 entity_id=sync_row["id"],
                 detail={"provider": provider, "external_ref": external_ref, "status": status})

    return {"ok": True, "status": status}
