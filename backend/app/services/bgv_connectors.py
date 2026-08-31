"""
Background Verification (BGV) vendor connector (ATS spec §10.1).

initiate_bgv / parse_bgv_webhook mirror connectors.push_offer_to_darwin's
stub pattern exactly: a fully-documented real integration, a manual/stub
fallback that keeps the workflow moving until a vendor is wired up, and a
credential presence check that decides which path runs -- no code change
needed to flip a tenant from stub to live once they sign with a vendor.
"""
import hashlib
import hmac
import time
import uuid
from typing import Optional

import requests

from ..db import query_one

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2


def _get_setting(tenant_id, key: str) -> Optional[str]:
    row = query_one("SELECT value FROM system_settings WHERE tenant_id=%s AND key=%s", [tenant_id, key])
    return row["value"] if row and row.get("value") else None


def get_webhook_secret(tenant_id, provider: str) -> Optional[str]:
    return _get_setting(tenant_id, f"bgv_webhook_secret_{provider}")


def verify_webhook_signature(secret: str, raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256 over the raw request body, hex-encoded, compared with a
    constant-time comparison. Accepts a bare hex digest or a
    "sha256=<hex>"-prefixed header (GitHub/Stripe-style) -- adjust the
    prefix-splitting here if a specific vendor uses a different header
    convention once one is actually wired up."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature.split("=", 1)[1] if "=" in signature else signature
    return hmac.compare_digest(expected, provided)


def initiate_bgv(case: dict, checks: list) -> dict:
    """
    STUB / real-path switch -- generic third-party BGV vendor handoff.
    Replace the "real path" branch below with the actual vendor call once a
    tenant signs with one (SpringVerify / AuthBridge / IDfy and similar
    vendors all follow roughly this same request/webhook shape -- swap the
    endpoint path and payload field names for whichever one it is).

    ── What the dev team needs to wire this up ──────────────────────────────────

    1. API base URL + credentials
       Stored per-tenant, per-provider in system_settings (never hard-coded
       or committed to source control):
         bgv_provider              -- vendor slug, e.g. 'springverify'
         bgv_api_base_{provider}   -- e.g. https://api.springverify.com/v2
         bgv_api_key_{provider}    -- vendor-issued API key (or OAuth client id)
         bgv_api_secret_{provider} -- OAuth client secret, if the vendor uses
                                       OAuth 2.0 client-credentials instead of
                                       a bare API key -- check the vendor's docs.
       initiate_bgv() reads these via _get_setting(tenant_id, key); if either
       the base URL or key is missing, it falls through to the stub path
       below rather than attempting a call with no credentials.

    2. Authentication
       Most BGV vendors use a bearer API key (Authorization: Bearer <key>)
       or an OAuth 2.0 client-credentials grant identical in shape to
       Darwinbox's (see push_offer_to_darwin in services/connectors.py) --
       POST /oauth/token with grant_type=client_credentials, cache the
       resulting access token until it expires, refresh on a 401.

    3. Payload format (case initiation)
       Typical fields for a BGV case-creation request:
         {
           "reference_id": case["id"],   // OUR case id, so the vendor's own
                                          // webhook can echo it back to us
           "candidate": {
             "full_name": "<from candidate>",
             "email":     "<candidate email>",
             "phone":     "<candidate phone>",
           },
           "checks": ["employment", "education", "identity", ...],
                                          // vendor-specific check codes --
                                          // map our bgv_check_type_config
                                          // keys to theirs here
         }
       Confirm exact field names and check-type codes with the vendor during
       integration testing -- they rarely match our internal keys 1:1.

    4. Endpoint
       POST {base_url}/cases
       Headers:
         Authorization: Bearer <api_key_or_token>
         Content-Type:  application/json

    5. Response handling
       On success the vendor returns their own case id -- store that as
       bgv_case.external_ref so the inbound webhook (parse_bgv_webhook,
       below) can match a later status update back to this case by
       external_ref, and so a human can look the case up on the vendor's
       own dashboard.

    6. Error handling
       - 401: API key/token expired or invalid -- refresh (OAuth) or raise
         immediately (a static API key has nothing to refresh; this means
         the credential in system_settings is wrong).
       - 422: payload validation error -- log the full response body, don't
         retry (retrying an invalid payload just repeats the same failure).
       - 5xx: transient -- retry with exponential back-off, max 3 attempts.
       A hard failure after retries raises RuntimeError, which the caller
       (bgv_api.initiate_bgv_case) surfaces as a 502 to the requesting staff
       user -- it must NOT be silently swallowed into a fake stub success,
       since that would tell HR a real BGV check is running when it isn't.

    7. Security note
       All vendor credentials MUST live in system_settings (per-tenant), or
       .env.prod for anything genuinely global. Never commit credentials to
       source control. The vendor's inbound webhook must be verified with an
       HMAC signature using a separate shared secret
       (bgv_webhook_secret_{provider}) before trusting ANY payload -- see
       verify_webhook_signature above and bgv_api.py's webhook route.

    ─────────────────────────────────────────────────────────────────────────────
    Until a provider's base URL + API key are configured in system_settings,
    this function logs the payload and returns a synthetic reference so the
    rest of the BGV workflow (case created, checks pending, HR can manually
    mark results) completes normally. The external_ref stored on the case
    will start with "STUB-BGV-" -- the dev team can query
    `SELECT * FROM bgv_case WHERE external_ref LIKE 'STUB-BGV-%'` to find
    every case that still needs a real vendor push after go-live.
    """
    tenant_id = case.get("tenant_id")
    provider = _get_setting(tenant_id, "bgv_provider") or "manual"
    base_url = _get_setting(tenant_id, f"bgv_api_base_{provider}")
    api_key = _get_setting(tenant_id, f"bgv_api_key_{provider}")

    if provider == "manual" or not base_url or not api_key:
        stub_ref = f"STUB-BGV-{uuid.uuid4().hex[:8].upper()}"
        print(
            f"[bgv STUB] initiate_bgv called — case_id={case.get('id')} "
            f"candidate_id={case.get('candidate_id')} "
            f"check_types={[c.get('check_type') for c in checks]} "
            f"external_ref_assigned={stub_ref}"
        )
        return {"provider": "manual", "external_ref": stub_ref}

    payload = {
        "reference_id": str(case.get("id")),
        "checks": [c.get("check_type") for c in checks],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{base_url.rstrip('/')}/cases"

    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue
        if resp.status_code == 401:
            raise RuntimeError(f"BGV vendor '{provider}' rejected the API key (401) — check bgv_api_key_{provider}")
        if resp.status_code == 422:
            raise RuntimeError(f"BGV vendor '{provider}' rejected the payload (422): {resp.text[:500]}")
        if resp.status_code >= 500:
            last_exc = RuntimeError(f"BGV vendor '{provider}' returned {resp.status_code}")
            time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue
        resp.raise_for_status()
        data = resp.json()
        external_ref = data.get("case_id") or data.get("id")
        if not external_ref:
            raise RuntimeError(f"BGV vendor '{provider}' response missing a case id: {data}")
        return {"provider": provider, "external_ref": str(external_ref)}

    raise RuntimeError(f"BGV vendor '{provider}' unreachable after {_MAX_RETRIES} attempts: {last_exc}")


_VENDOR_STATUS_MAP = {
    "clear": "approved", "pass": "approved", "passed": "approved", "completed": "approved", "approved": "approved",
    "flag": "flagged", "flagged": "flagged", "discrepancy": "flagged",
    "fail": "rejected", "failed": "rejected", "rejected": "rejected",
    "pending": "pending", "queued": "pending",
    "in_progress": "in_progress", "processing": "in_progress", "started": "in_progress",
}


def _map_vendor_status(raw) -> str:
    return _VENDOR_STATUS_MAP.get((raw or "").strip().lower(), "in_progress")


def parse_bgv_webhook(provider: str, payload: dict) -> dict:
    """
    Normalizes an inbound vendor webhook payload into our internal shape.
    Provider-agnostic for now since no vendor is actually wired up yet --
    once one is, branch on `provider` here for its specific field names.

    Generic expected vendor payload:
      {
        "case_id": "<the external_ref we stored when we called initiate_bgv>",
        "checks": [
          {"type": "employment", "status": "clear", "summary": "...", "evidence_url": "..."},
          ...
        ]
      }

    Returns:
      {
        "external_ref": "<vendor case id>",
        "checks": [{"check_type": ..., "status": <our vocabulary>,
                     "result_summary": ..., "evidence_url": ...}, ...],
      }

    bgv_api.py's webhook route resolves which tenant this belongs to by
    looking up bgv_case.external_ref (the request itself carries no
    tenant/JWT), verifies the HMAC signature using that tenant's stored
    secret, and only then applies these check updates.
    """
    external_ref = payload.get("case_id") or payload.get("reference_id") or payload.get("id")
    checks = []
    for c in (payload.get("checks") or []):
        check_type = c.get("type") or c.get("check_type")
        if not check_type:
            continue
        checks.append({
            "check_type": check_type,
            "status": _map_vendor_status(c.get("status")),
            "result_summary": c.get("summary") or c.get("result_summary"),
            "evidence_url": c.get("evidence_url"),
        })
    return {"external_ref": str(external_ref) if external_ref else None, "checks": checks}
