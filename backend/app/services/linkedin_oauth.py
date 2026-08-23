"""
LinkedIn "Sign in with LinkedIn" (OpenID Connect) for the candidate portal.

Mirrors services/google_calendar.py's shape (env-based client config,
is_configured() gate, state-token connect/callback), but this is a PER-
CANDIDATE connection (each candidate authorizes their own LinkedIn account),
not a single shared system connection.

Hard constraint, not a bug: LinkedIn's general-access OIDC product only ever
returns identity claims (sub, name, given_name, picture, email) -- there is
no public-profile-URL or headline/work-history claim available without a
Talent/Marketing partner agreement. So this module can verify who someone is
and refresh their name/photo, but it can NEVER populate candidate.linkedin_url
itself -- that field is always a manually-entered value (see
candidate_portal_api.py's /portal/linkedin/manual). Do not add a "derive the
profile URL from the OAuth response" TODO here; there is no claim to derive
it from.

Never raises out of the "is this configured" surface -- every function here
returns None/False when LinkedIn isn't configured so the candidate-facing
API can show a "Coming Soon" state instead of a 500. Only the interactive
connect/callback flow (driven by a candidate clicking through) is allowed to
raise/surface an error.
"""
import os
from typing import Optional

import requests

_AUTH_URL     = "https://www.linkedin.com/oauth/v2/authorization"
_TOKEN_URL    = "https://www.linkedin.com/oauth/v2/accessToken"
_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
_SCOPE        = "openid profile email"


def _client_config() -> Optional[tuple]:
    """(client_id, client_secret, redirect_uri) from env, or None if any of
    the three is unset -- callers treat that identically to "LinkedIn
    integration not configured / coming soon"."""
    client_id     = os.environ.get("LINKEDIN_CLIENT_ID", "").strip()
    client_secret = os.environ.get("LINKEDIN_CLIENT_SECRET", "").strip()
    redirect_uri  = os.environ.get("LINKEDIN_REDIRECT_URI", "").strip()
    if not client_id or not client_secret or not redirect_uri:
        return None
    return client_id, client_secret, redirect_uri


def is_configured() -> bool:
    """Whether real LinkedIn OAuth credentials exist yet. False until the
    LinkedIn Developer app is registered and its Client ID/Secret/Redirect
    URI are set as env vars -- the candidate-facing "Update LinkedIn" button
    reads this (via GET /api/candidate/portal/linkedin/status) to show a
    "Coming Soon" state instead of attempting a connect that would fail."""
    return _client_config() is not None


def build_auth_url(state: str) -> Optional[str]:
    """The LinkedIn consent-screen URL a candidate's browser should be sent
    to. None if LinkedIn credentials aren't configured yet."""
    cfg = _client_config()
    if not cfg:
        return None
    client_id, _secret, redirect_uri = cfg
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _SCOPE,
        "state": state,
    }
    query_str = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    return f"{_AUTH_URL}?{query_str}"


def _raise_with_linkedin_detail(resp: "requests.Response", context: str) -> None:
    """resp.raise_for_status() only ever says "400 Client Error" -- LinkedIn's
    actual reason is in the JSON body. Surface that instead so a failed
    connect attempt is debuggable without server logs."""
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        detail = body.get("error_description") or body.get("error") or resp.text
    except Exception:
        detail = resp.text or f"HTTP {resp.status_code}"
    raise RuntimeError(f"{context}: {detail}")


def exchange_code(code: str) -> dict:
    """Exchange an OAuth `code` for tokens and fetch the OIDC userinfo.
    Raises on failure -- only ever runs inside the interactive callback
    endpoint, which surfaces a real error to the connecting candidate rather
    than failing silently.

    Returns {"sub", "name", "given_name", "picture", "email"} -- exactly
    what LinkedIn's general-access product provides. No headline, no
    profile URL, no work history: those require partner-tier scopes this
    app does not have.
    """
    cfg = _client_config()
    if not cfg:
        raise RuntimeError("LinkedIn OAuth is not configured (LINKEDIN_CLIENT_ID/SECRET/REDIRECT_URI)")
    client_id, client_secret, redirect_uri = cfg

    resp = requests.post(_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }, timeout=15)
    _raise_with_linkedin_detail(resp, "Token exchange failed")
    tokens = resp.json()
    access_token = tokens.get("access_token")
    if not access_token:
        raise RuntimeError("LinkedIn did not return an access token")

    userinfo = requests.get(
        _USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15,
    )
    _raise_with_linkedin_detail(userinfo, "Fetching LinkedIn profile failed")
    info = userinfo.json()

    return {
        "sub":        info.get("sub"),
        "name":       info.get("name"),
        "given_name": info.get("given_name"),
        "picture":    info.get("picture"),
        "email":      info.get("email"),
    }
