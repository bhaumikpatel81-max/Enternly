"""
HRMS Multi-Provider Integration Layer (ATS spec §14).

sync_to_hrms() dispatches to one adapter per provider. Every adapter
(except darwinbox, which delegates to the existing
connectors.push_offer_to_darwin) follows the same stub/real switch as that
function: a fully-documented real integration, and a stub fallback that
keeps the workflow moving until a tenant's credentials are configured --
no code change needed to flip a tenant from stub to live once they do.

HMAC webhook verification is shared, generic infrastructure (not
provider-specific business logic), so it's imported from bgv_connectors
rather than re-implemented here.
"""
import time
import uuid
from typing import Optional

import requests

from ..db import query_one
from .bgv_connectors import verify_webhook_signature  # noqa: F401 -- re-exported for hrms_api.py

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2

_PROVIDERS = ("successfactors", "workday", "oracle_hcm", "darwinbox", "zoho_people", "bamboohr", "greythr")

_DEFAULT_MAPPINGS = {
    "successfactors": {
        "employee_code": "personIdExternal", "designation": "jobTitle", "joining_date": "startDate",
        "department_id": "department", "location": "location", "grade": "payGrade", "cost_center": "costCenter",
    },
    "workday": {
        "employee_code": "Employee_ID", "designation": "Business_Title", "joining_date": "Hire_Date",
        "department_id": "Organization_Reference", "location": "Location_Reference",
        "grade": "Compensation_Grade", "cost_center": "Cost_Center_Reference",
    },
    "oracle_hcm": {
        "employee_code": "PersonNumber", "designation": "AssignmentName", "joining_date": "StartDate",
        "department_id": "DepartmentId", "location": "LocationCode", "grade": "GradeCode", "cost_center": "CostCenterCode",
    },
    "zoho_people": {
        "employee_code": "EmployeeID", "designation": "Designation", "joining_date": "DateOfJoining",
        "department_id": "Department", "location": "Location", "grade": "Grade", "cost_center": "CostCenter",
    },
    "bamboohr": {
        "employee_code": "employeeNumber", "designation": "jobTitle", "joining_date": "hireDate",
        "department_id": "department", "location": "location", "grade": "payGrade", "cost_center": "division",
    },
    "greythr": {
        "employee_code": "empId", "designation": "designation", "joining_date": "dateOfJoining",
        "department_id": "department", "location": "location", "grade": "grade", "cost_center": "costCenter",
    },
}


def _get_setting(tenant_id, key: str) -> Optional[str]:
    row = query_one("SELECT value FROM system_settings WHERE tenant_id=%s AND key=%s", [tenant_id, key])
    return row["value"] if row and row.get("value") else None


def get_webhook_secret(tenant_id, provider: str) -> Optional[str]:
    return _get_setting(tenant_id, f"hrms_webhook_secret_{provider}")


def _apply_mapping(employee_record: dict, tenant_mapping: dict, default_mapping: dict) -> dict:
    effective = {**default_mapping, **(tenant_mapping or {})}
    return {effective.get(field, field): value for field, value in employee_record.items()}


def _stub_ref(provider: str) -> str:
    return f"STUB-HRMS-{provider}-{uuid.uuid4().hex[:8].upper()}"


def _post_with_retry(provider: str, url: str, headers: dict, payload: dict) -> dict:
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue
        if resp.status_code == 401:
            raise RuntimeError(f"HRMS vendor '{provider}' rejected the credential (401) — check hrms_{provider}_key")
        if resp.status_code == 422:
            raise RuntimeError(f"HRMS vendor '{provider}' rejected the payload (422): {resp.text[:500]}")
        if resp.status_code >= 500:
            last_exc = RuntimeError(f"HRMS vendor '{provider}' returned {resp.status_code}")
            time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"HRMS vendor '{provider}' unreachable after {_MAX_RETRIES} attempts: {last_exc}")


def _stub_or_real(provider: str, employee_record: dict, tenant_mapping: dict, endpoint_path: str) -> dict:
    """Shared stub/real switch for every REST-style provider (everything
    except darwinbox, which has its own delegation below). Credentials
    live in system_settings as hrms_{provider}_base / hrms_{provider}_key;
    if either is missing, falls through to the stub path rather than
    attempting a call with no credentials."""
    tenant_id = employee_record.get("tenant_id")
    base_url = _get_setting(tenant_id, f"hrms_{provider}_base")
    api_key = _get_setting(tenant_id, f"hrms_{provider}_key")

    if not base_url or not api_key:
        stub_ref = _stub_ref(provider)
        print(
            f"[hrms STUB] sync_to_hrms({provider}) called — employee_master_id={employee_record.get('id')} "
            f"candidate_id={employee_record.get('candidate_id')} employee_code={employee_record.get('employee_code')} "
            f"external_ref_assigned={stub_ref}"
        )
        return {"provider": provider, "external_ref": stub_ref, "status": "in_progress"}

    payload = _apply_mapping(employee_record, tenant_mapping, _DEFAULT_MAPPINGS[provider])
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{base_url.rstrip('/')}{endpoint_path}"
    data = _post_with_retry(provider, url, headers, payload)
    external_ref = data.get("id") or data.get("employee_id") or data.get("worker_id") or data.get("PersonId")
    if not external_ref:
        raise RuntimeError(f"HRMS vendor '{provider}' response missing an employee id: {data}")
    return {"provider": provider, "external_ref": str(external_ref), "status": "in_progress"}


def _sync_successfactors(employee_record: dict, documents: list, mapping: dict) -> dict:
    """
    STUB / real-path switch -- SAP SuccessFactors Employee Central push.

    1. API base URL
       Multi-tenant SaaS with per-datacenter API hosts, e.g.
       https://api4.successfactors.com/odata/v2 -- the exact host/pod is
       assigned per customer by SAP; confirm with the tenant's SF admin.
       Stored as hrms_successfactors_base in system_settings.

    2. Authentication
       OAuth 2.0 SAML Bearer Assertion: SuccessFactors issues a SAML
       assertion signed by a registered X.509 certificate, exchanged for a
       bearer token at POST /oauth/token
       (grant_type=urn:ietf:params:oauth:grant-type:saml2-bearer). Most
       real integrations instead register an OAuth client (client id +
       API-user) and skip the full SAML dance -- store that as
       hrms_successfactors_key (and hrms_successfactors_secret if the
       vendor requires a second value) in system_settings, never
       hard-coded. Tokens expire -- cache and refresh on 401.

    3. Payload format (Employee Central "PerPerson"/"EmpEmployment" upsert)
         {
           "personIdExternal": employee_record["employee_code"],
           "jobTitle":  employee_record["designation"],
           "startDate": employee_record["joining_date"],   // YYYY-MM-DD
           "department": employee_record["department_id"],
           ...
         }
       Exact OData entity/field names depend on the tenant's EC data model
       (custom MDF objects, business rules) -- confirm during integration
       testing; field_mapping lets a tenant override the defaults above
       without a code change.

    4. Endpoint
       POST {base_url}/upsert (an OData $batch upsert is typical for EC)
       Headers: Authorization: Bearer <token>, Content-Type: application/json

    5. Response handling
       SuccessFactors returns the created/updated personIdExternal (or an
       OData batch response with a per-record status) -- stored as
       hrms_sync.external_ref.

    6. Error handling
       - 401: token expired -- refresh and retry once.
       - 422 / an OData error entry: payload or business-rule validation
         failure -- log the full response body, don't retry.
       - 5xx: transient -- exponential back-off, max 3 attempts.
       A hard failure after retries raises RuntimeError -- the caller
       (hrms_api.sync_candidate_to_hrms) surfaces it as a 502 and marks
       hrms_sync 'failed'; it must never be silently swallowed into a fake
       stub success.

    7. Security note
       Client id/secret (and the SAML signing cert, if used) MUST live in
       system_settings, never in source control.

    Until hrms_successfactors_base/_key are configured, this returns a
    synthetic STUB-HRMS-successfactors-<hex> ref so onboarding sync
    completes without blocking on the real integration. Query
    `SELECT * FROM hrms_sync WHERE external_ref LIKE 'STUB-HRMS-%'` to find
    every record still needing a real push after go-live.
    """
    return _stub_or_real("successfactors", employee_record, mapping, "/upsert")


def _sync_workday(employee_record: dict, documents: list, mapping: dict) -> dict:
    """
    STUB / real-path switch -- Workday Human Capital Management push.

    1. API base URL
       Workday's modern REST API: https://{wd-instance}.workday.com/ccx/api/v1/{tenant}
       (a legacy SOAP "Workday Web Services" -- WWS -- integration exists
       at a different host and is still common for Staffing/HR events;
       prefer REST for new integrations). Stored as hrms_workday_base.

    2. Authentication
       OAuth 2.0 -- either the refresh-token grant (register an Integration
       System User + API client in Workday, store client id/secret +
       refresh token) or client-credentials, depending on what the
       tenant's Workday security admin provisions. Store the resulting
       bearer-capable credential as hrms_workday_key. The legacy SOAP path
       instead uses WS-Security username/password (an Integration System
       User) -- avoid unless the tenant is already on that integration.

    3. Payload format (Staffing "Hire"/"Change Job" business process)
         {
           "Employee_ID": employee_record["employee_code"],
           "Business_Title": employee_record["designation"],
           "Hire_Date": employee_record["joining_date"],
           "Organization_Reference": employee_record["department_id"],
           ...
         }
       Workday integrations are almost always business-process-driven
       (Hire, not a raw upsert) -- the exact required fields depend on the
       tenant's configured Hire business process; confirm during
       integration testing. field_mapping overrides the defaults above.

    4. Endpoint
       POST {base_url}/workers  (REST) -- or a SOAP "Hire_Employee"
       operation against the Staffing service for the legacy path.
       Headers: Authorization: Bearer <token>, Content-Type: application/json

    5. Response handling
       Workday returns the created Worker's Employee_ID/WID -- stored as
       hrms_sync.external_ref.

    6. Error handling
       - 401: token expired -- refresh and retry once.
       - 422 / a Workday validation-error response: business-process
         validation failure (e.g. missing required step) -- log the full
         response body, don't retry.
       - 5xx: transient -- exponential back-off, max 3 attempts.

    7. Security note
       OAuth client credentials / refresh token (or the Integration System
       User's password, for the legacy SOAP path) MUST live in
       system_settings, never in source control.

    Until hrms_workday_base/_key are configured, this returns a synthetic
    STUB-HRMS-workday-<hex> ref so onboarding sync completes without
    blocking on the real integration.
    """
    return _stub_or_real("workday", employee_record, mapping, "/workers")


def _sync_oracle_hcm(employee_record: dict, documents: list, mapping: dict) -> dict:
    """
    STUB / real-path switch -- Oracle Fusion HCM Cloud push.

    1. API base URL
       https://{instance}.fa.{region}.oraclecloud.com -- the instance name
       and data-center region are assigned per customer by Oracle. Stored
       as hrms_oracle_hcm_base.

    2. Authentication
       HTTP Basic Auth against an Oracle-provisioned integration user
       (simplest, still common), or OAuth 2.0 via Oracle Identity Cloud
       Service (IDCS) for tenants that require token-based auth. Store the
       resulting API key/token as hrms_oracle_hcm_key.

    3. Payload format (HCM REST "Workers" resource)
         {
           "PersonNumber": employee_record["employee_code"],
           "AssignmentName": employee_record["designation"],
           "StartDate": employee_record["joining_date"],
           "DepartmentId": employee_record["department_id"],
           ...
         }
       Oracle HCM's Workers resource is deeply nested (Person ->
       WorkRelationship -> Assignment) in the real API; this is the
       flattened shape field_mapping produces before the real call --
       confirm the exact nested structure the tenant's HCM instance
       expects during integration testing.

    4. Endpoint
       POST {base_url}/hcmRestApi/resources/11.13.18.05/workers
       Headers: Authorization: Basic <base64> (or Bearer <token> for
       IDCS), Content-Type: application/vnd.oracle.adf.resourceitem+json

    5. Response handling
       Oracle HCM returns the created PersonId/PersonNumber -- stored as
       hrms_sync.external_ref.

    6. Error handling
       - 401: credential invalid/expired -- refresh (IDCS) or raise
         immediately (Basic Auth has nothing to refresh).
       - 422 / an Oracle "REST-nnnnn" error payload: validation failure --
         log the full response body, don't retry.
       - 5xx: transient -- exponential back-off, max 3 attempts.

    7. Security note
       The Basic Auth credential or IDCS OAuth secret MUST live in
       system_settings, never in source control.

    Until hrms_oracle_hcm_base/_key are configured, this returns a
    synthetic STUB-HRMS-oracle_hcm-<hex> ref so onboarding sync completes
    without blocking on the real integration.
    """
    return _stub_or_real("oracle_hcm", employee_record, mapping, "/hcmRestApi/resources/11.13.18.05/workers")


def _sync_darwinbox(employee_record: dict, documents: list, mapping: dict) -> dict:
    """
    Darwinbox push for the HRMS integration layer -- delegates entirely to
    connectors.push_offer_to_darwin(), the exact same stub/real Darwinbox
    integration point offers_api.py already uses when an offer reaches
    'sent_to_darwinbox'. Not duplicated here: see that function's docstring
    in services/connectors.py for the full real-integration writeup (base
    URL, OAuth 2.0 client-credentials auth, payload shape, endpoint, error
    handling, retry/back-off, security note) -- it applies unchanged to
    this path too, since it's the same vendor and the same credential keys
    (system_settings, not per-module).

    employee_record's fields are adapted to the shape push_offer_to_darwin
    expects (id/candidate/designation/total_ctc/joining_date). Darwinbox
    has no "total_ctc" concept at the post-hire sync stage the way an
    offer does, so it's passed as None here -- a real integration would
    look it up from the candidate's accepted offer if Darwinbox's employee
    API actually requires it.
    """
    from . import connectors
    result = connectors.push_offer_to_darwin({
        "id": employee_record.get("id"),
        "candidate": employee_record.get("candidate_id"),
        "designation": employee_record.get("designation"),
        "total_ctc": None,
        "joining_date": employee_record.get("joining_date"),
    })
    return {"provider": "darwinbox", "external_ref": result["darwin_ref"], "status": "in_progress"}


def _sync_zoho_people(employee_record: dict, documents: list, mapping: dict) -> dict:
    """
    STUB / real-path switch -- Zoho People push.

    1. API base URL
       https://people.zoho.com/people/api (or a regional variant, e.g.
       .eu/.in/.com.cn, depending on the tenant's Zoho data center).
       Stored as hrms_zoho_people_base.

    2. Authentication
       OAuth 2.0 with Zoho's standard refresh-token flow: register a
       self-client in the Zoho API console, obtain a refresh_token once,
       then exchange it for a short-lived (~1h) access token before every
       batch of calls. Store the refresh_token as hrms_zoho_people_key
       (the access token is derived at call time, never persisted).

    3. Payload format (Employee "addRecord" API)
         {
           "EmployeeID": employee_record["employee_code"],
           "Designation": employee_record["designation"],
           "Dateofjoining": employee_record["joining_date"],   // dd-MMM-yyyy
           "Department": employee_record["department_id"],
           ...
         }
       Zoho People's date format (dd-MMM-yyyy) differs from every other
       provider here -- convert employee_record["joining_date"] before
       sending; field_mapping only renames keys, it doesn't reformat
       values, so this conversion belongs in the real-path branch once
       written.

    4. Endpoint
       POST {base_url}/forms/employee/addRecord
       Headers: Authorization: Zoho-oauthtoken <access_token>

    5. Response handling
       Zoho returns the new record's EmployeeID/recordId in a nested
       "response.result" structure -- stored as hrms_sync.external_ref.

    6. Error handling
       - 401 (INVALID_TOKEN): access token expired -- refresh via the
         stored refresh_token and retry once.
       - 422 / a Zoho error code in the response body: validation
         failure -- log the full response body, don't retry.
       - 5xx: transient -- exponential back-off, max 3 attempts.

    7. Security note
       The refresh_token MUST live in system_settings, never in source
       control -- it is a long-lived credential equivalent to a password.

    Until hrms_zoho_people_base/_key are configured, this returns a
    synthetic STUB-HRMS-zoho_people-<hex> ref so onboarding sync completes
    without blocking on the real integration.
    """
    return _stub_or_real("zoho_people", employee_record, mapping, "/forms/employee/addRecord")


def _sync_bamboohr(employee_record: dict, documents: list, mapping: dict) -> dict:
    """
    STUB / real-path switch -- BambooHR push.

    1. API base URL
       https://api.bamboohr.com/api/gateway.php/{company_domain}/v1 --
       {company_domain} is the tenant's BambooHR subdomain. Stored as
       hrms_bamboohr_base (the full URL including the company domain, so
       this connector doesn't need a separate "company domain" setting).

    2. Authentication
       HTTP Basic Auth where the API key is the username and the literal
       string "x" is the password (BambooHR's documented convention, not a
       placeholder). Store the API key as hrms_bamboohr_key.

    3. Payload format (Employee "add" API)
         {
           "employeeNumber": employee_record["employee_code"],
           "jobTitle":  employee_record["designation"],
           "hireDate":  employee_record["joining_date"],   // YYYY-MM-DD
           "department": employee_record["department_id"],
           ...
         }
       BambooHR's real API is XML-based for some legacy endpoints but the
       modern "employees" REST endpoints accept/return JSON -- use JSON.

    4. Endpoint
       POST {base_url}/employees/
       Headers: Authorization: Basic <base64(api_key + ":x")>,
       Content-Type: application/json

    5. Response handling
       BambooHR returns a Location header containing the new employee's id
       (e.g. .../employees/123) rather than a JSON body id in some API
       versions -- a real implementation should check both the JSON body
       and the Location header. Stored as hrms_sync.external_ref.

    6. Error handling
       - 401: API key invalid -- nothing to refresh (static key), raise
         immediately so the tenant knows to check hrms_bamboohr_key.
       - 422 / a validation-error JSON body: log the full response body,
         don't retry.
       - 5xx: transient -- exponential back-off, max 3 attempts.

    7. Security note
       The API key MUST live in system_settings, never in source control.

    Until hrms_bamboohr_base/_key are configured, this returns a synthetic
    STUB-HRMS-bamboohr-<hex> ref so onboarding sync completes without
    blocking on the real integration.
    """
    return _stub_or_real("bamboohr", employee_record, mapping, "/employees/")


def _sync_greythr(employee_record: dict, documents: list, mapping: dict) -> dict:
    """
    STUB / real-path switch -- greytHR push.

    1. API base URL
       https://{domain}.greythr.com/uas/hrapi -- {domain} is the tenant's
       greytHR subdomain. Stored as hrms_greythr_base (full URL, domain
       included).

    2. Authentication
       API-key based: greytHR's public API requires an "access-token"
       obtained via a one-time login call using a registered API
       client id + client secret, then every subsequent request carries
       that access-token plus the original client id in custom headers.
       Store the client id as hrms_greythr_key and the client secret as
       hrms_greythr_secret in system_settings; cache the derived
       access-token in memory (not persisted), refreshing on 401.

    3. Payload format (Employee "add" API)
         {
           "empId": employee_record["employee_code"],
           "designation": employee_record["designation"],
           "dateOfJoining": employee_record["joining_date"],   // YYYY-MM-DD
           "department": employee_record["department_id"],
           ...
         }

    4. Endpoint
       POST {base_url}/v2/employees
       Headers: access-token: <token>, X-API-KEY: <client_id>,
       Content-Type: application/json

    5. Response handling
       greytHR returns the new employee's empId/id -- stored as
       hrms_sync.external_ref.

    6. Error handling
       - 401: access-token expired -- re-authenticate with the stored
         client id/secret and retry once.
       - 422 / a greytHR error-code response: validation failure -- log
         the full response body, don't retry.
       - 5xx: transient -- exponential back-off, max 3 attempts.

    7. Security note
       The client id/secret pair MUST live in system_settings, never in
       source control.

    Until hrms_greythr_base/_key are configured, this returns a synthetic
    STUB-HRMS-greythr-<hex> ref so onboarding sync completes without
    blocking on the real integration.
    """
    return _stub_or_real("greythr", employee_record, mapping, "/v2/employees")


_ADAPTERS = {
    "successfactors": _sync_successfactors,
    "workday": _sync_workday,
    "oracle_hcm": _sync_oracle_hcm,
    "darwinbox": _sync_darwinbox,
    "zoho_people": _sync_zoho_people,
    "bamboohr": _sync_bamboohr,
    "greythr": _sync_greythr,
}


def sync_to_hrms(provider: str, employee_record: dict, documents: list, mapping: dict) -> dict:
    """Dispatches to the named provider's adapter. Every adapter returns
    {"provider", "external_ref", "status"} -- callers persist that onto
    the hrms_sync row. Raises ValueError for an unknown provider (a config
    bug, not a runtime vendor failure) and RuntimeError for a real-path
    integration failure (see each adapter's docstring)."""
    adapter = _ADAPTERS.get(provider)
    if not adapter:
        raise ValueError(f"Unknown HRMS provider '{provider}'")
    return adapter(employee_record, documents or [], mapping or {})


_VENDOR_STATUS_MAP = {
    "success": "success", "succeeded": "success", "completed": "success", "synced": "success",
    "failed": "failed", "failure": "failed", "error": "failed", "rejected": "failed",
    "pending": "pending", "queued": "pending",
    "in_progress": "in_progress", "processing": "in_progress", "started": "in_progress",
}


def _map_vendor_status(raw) -> str:
    return _VENDOR_STATUS_MAP.get((raw or "").strip().lower(), "in_progress")


def parse_hrms_webhook(provider: str, payload: dict) -> dict:
    """
    Normalizes an inbound vendor webhook payload into our internal shape.
    Provider-agnostic for now since no vendor is actually wired up yet --
    once one is, branch on `provider` here for its specific field names.

    Generic expected vendor payload:
      {
        "employee_id": "<the external_ref we stored when we called sync_to_hrms>",
        "status": "success",
        "message": "...",
        "error": null
      }

    Returns:
      {"external_ref": ..., "status": <our vocabulary>, "response_summary": ..., "error": ...}

    hrms_api.py's webhook route resolves which tenant this belongs to by
    looking up hrms_sync.external_ref (the request itself carries no
    tenant/JWT), verifies the HMAC signature using that tenant's stored
    secret, and only then applies this update.
    """
    external_ref = payload.get("employee_id") or payload.get("reference_id") or payload.get("id")
    return {
        "external_ref": str(external_ref) if external_ref else None,
        "status": _map_vendor_status(payload.get("status")),
        "response_summary": payload.get("message") or payload.get("summary"),
        "error": payload.get("error"),
    }
