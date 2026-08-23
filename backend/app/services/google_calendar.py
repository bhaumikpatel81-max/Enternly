"""
Google Calendar OAuth + event creation -- one shared connection (hr@amnex.com,
or whichever mailbox a TA admin connects) used to auto-generate a real Google
Meet link for every panel round's confirmed interview.

Deliberately a SINGLE system-wide connection, not per-recruiter: a TA admin
connects once (GET /api/google/connect -> Google consent screen -> GET
/api/google/callback), and every panel round's interview event is created
under that one account from then on. See routers/google_calendar_api.py for
the connect/disconnect/status endpoints and scheduling_api.confirm_pick()
for where the generated link actually gets used.

Never raises out of the "am I connected / can I create an event" surface --
every public function here returns None/False on any failure so callers can
fall back to the recruiter-configured static round meeting_link without the
booking itself ever failing. Only the interactive connect/callback flow
(driven directly by an admin clicking through, not by candidate-facing code)
is allowed to raise/surface an error.
"""
import base64
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from ..db import query, query_one

_TOKEN_URL   = "https://oauth2.googleapis.com/token"
_AUTH_URL    = "https://accounts.google.com/o/oauth2/v2/auth"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
_EVENTS_URL  = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
# calendar.events alone issues a token that the userinfo endpoint below
# rejects with 401 UNAUTHENTICATED -- it's a different API needing its own
# scope. `openid email` (standard OpenID Connect scopes, not the legacy
# userinfo.email scope) makes Google return an ID token in the SAME token
# response we already know succeeds -- decoded locally below, so knowing
# which Google account this is no longer depends on a second, separately-
# authorized API call that can fail on its own.
_SCOPE = "https://www.googleapis.com/auth/calendar.events openid email"

# Refresh a bit before actual expiry so a slow request never straddles the
# boundary and gets rejected mid-call.
_EXPIRY_SAFETY_MARGIN_SEC = 60


def _client_config() -> Optional[tuple]:
    """(client_id, client_secret, redirect_uri) from env, or None if any of
    the three is unset/still the docker-compose placeholder -- callers treat
    that identically to "Google integration not configured"."""
    client_id     = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    redirect_uri  = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
    if not client_id or not client_secret or not redirect_uri or client_id == "placeholder":
        return None
    return client_id, client_secret, redirect_uri


def is_configured() -> bool:
    """Whether real OAuth credentials exist at all (independent of whether
    anyone has actually clicked through the consent flow yet)."""
    return _client_config() is not None


def get_connection() -> Optional[dict]:
    """The single active connection row, if one exists."""
    return query_one(
        "SELECT * FROM google_calendar_connection ORDER BY created_at DESC LIMIT 1"
    )


def is_connected() -> bool:
    return get_connection() is not None


def build_auth_url(state: str) -> Optional[str]:
    """The Google consent-screen URL an admin's browser should be sent to.
    None if Google credentials aren't configured yet."""
    cfg = _client_config()
    if not cfg:
        return None
    client_id, _secret, redirect_uri = cfg
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _SCOPE,
        "access_type": "offline",   # required to receive a refresh_token
        "prompt": "consent",        # force refresh_token issuance even on a re-connect
        "state": state,
    }
    query_str = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    return f"{_AUTH_URL}?{query_str}"


def _decode_id_token_email(id_token: str) -> Optional[str]:
    """Pull the `email` claim out of Google's ID token JWT without verifying
    the signature. Acceptable here: this token was just received directly
    from Google's token endpoint over an HTTPS call WE authenticated with
    our client_secret (not supplied by an untrusted browser client), and is
    only ever used to label the connection in the admin UI -- never for an
    authorization decision, which is what would actually require verifying
    the signature against Google's published keys."""
    try:
        payload_b64 = id_token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get("email")
    except Exception:
        return None


def _raise_with_google_detail(resp: "requests.Response", context: str) -> None:
    """resp.raise_for_status() only ever says "400 Client Error" -- Google's
    actual reason (invalid_grant, redirect_uri_mismatch, invalid_client, ...)
    is in the JSON body. Surface that instead so a failed connect attempt is
    debuggable from the flash message alone, without needing server logs."""
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        detail = body.get("error_description") or body.get("error") or resp.text
    except Exception:
        detail = resp.text or f"HTTP {resp.status_code}"
    raise RuntimeError(f"{context}: {detail}")


def exchange_code_and_store(code: str, connected_by: Optional[str]) -> dict:
    """Exchange an OAuth `code` for tokens, fetch which Google account it is,
    and store it as THE connection (replacing any previous one). Raises on
    failure -- this only ever runs inside the interactive callback endpoint,
    which is expected to surface a real error to the connecting admin rather
    than fail silently."""
    cfg = _client_config()
    if not cfg:
        raise RuntimeError("Google OAuth is not configured (GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI)")
    client_id, client_secret, redirect_uri = cfg

    resp = requests.post(_TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=15)
    _raise_with_google_detail(resp, "Token exchange failed")
    tokens = resp.json()
    access_token  = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        # Google only issues a refresh_token on the FIRST consent (or when
        # prompt=consent forces re-issuance, which build_auth_url always sets)
        # -- if it's still missing here, the connection is useless the moment
        # the short-lived access_token expires.
        raise RuntimeError(
            "Google did not return a refresh token -- revoke this app's access at "
            "https://myaccount.google.com/permissions and try connecting again."
        )
    expires_in = int(tokens.get("expires_in") or 3600)
    scope = tokens.get("scope") or _SCOPE

    google_email = _decode_id_token_email(tokens.get("id_token") or "")
    if not google_email:
        # Fall back to the userinfo endpoint, but don't let a failure here
        # abort an otherwise-successful connection -- the refresh_token (the
        # part that actually matters for creating events) is already good;
        # losing the display label isn't worth discarding that.
        try:
            userinfo = requests.get(
                _USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15,
            )
            _raise_with_google_detail(userinfo, "Fetching account email failed")
            google_email = userinfo.json().get("email")
        except Exception as exc:
            print(f"[google_calendar] userinfo fallback also failed (non-fatal): {exc}")
    google_email = google_email or "unknown"

    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    # Single-row semantics: replace whatever was connected before.
    query("DELETE FROM google_calendar_connection", fetch=False)
    query(
        """INSERT INTO google_calendar_connection
             (google_email, access_token, refresh_token, token_expiry, scope, connected_by)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        [google_email, access_token, refresh_token, expiry, scope, connected_by],
        fetch=False,
    )
    return {"google_email": google_email}


def disconnect() -> None:
    query("DELETE FROM google_calendar_connection", fetch=False)


def _refresh_access_token(conn: dict) -> Optional[str]:
    cfg = _client_config()
    if not cfg:
        return None
    client_id, client_secret, _redirect_uri = cfg
    try:
        resp = requests.post(_TOKEN_URL, data={
            "refresh_token": conn["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        }, timeout=15)
        resp.raise_for_status()
        tokens = resp.json()
    except Exception as exc:
        print(f"[google_calendar] token refresh failed: {exc}")
        return None
    access_token = tokens.get("access_token")
    if not access_token:
        return None
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in") or 3600))
    query(
        "UPDATE google_calendar_connection SET access_token=%s, token_expiry=%s, updated_at=now() WHERE id=%s",
        [access_token, expiry, conn["id"]], fetch=False,
    )
    return access_token


def _get_valid_access_token() -> Optional[str]:
    conn = get_connection()
    if not conn:
        return None
    expiry = conn.get("token_expiry")
    now = datetime.now(timezone.utc)
    if expiry and (expiry - now).total_seconds() > _EXPIRY_SAFETY_MARGIN_SEC:
        return conn["access_token"]
    return _refresh_access_token(conn)


def create_event_with_meet(
    summary: str,
    description: str,
    start_dt_utc: datetime,
    duration_min: int,
    attendee_emails: list,
) -> Optional[dict]:
    """Create a real Calendar event (on the connected account's primary
    calendar) with an auto-generated Google Meet link. Returns
    {"event_id", "meet_link", "html_link"} on success, None on any failure
    (not connected, network error, API error) -- callers must treat None as
    "fall back to the recruiter's configured static link," never as fatal.

    sendUpdates=none: we send our own branded confirmation email + .ics
    separately (scheduling_api.confirm_pick) -- without this, Google would
    ALSO email every attendee its own bare calendar-invite notification,
    landing as a confusing duplicate alongside the branded one.
    """
    access_token = _get_valid_access_token()
    if not access_token:
        return None

    end_dt_utc = start_dt_utc + timedelta(minutes=duration_min)
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt_utc.isoformat()},
        "end":   {"dateTime": end_dt_utc.isoformat()},
        "attendees": [{"email": e} for e in attendee_emails if e],
        "conferenceData": {
            "createRequest": {
                "requestId": f"ats-{start_dt_utc.timestamp():.0f}-{abs(hash(summary)) % 100000}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    try:
        resp = requests.post(
            _EVENTS_URL,
            params={"conferenceDataVersion": 1, "sendUpdates": "none"},
            headers={"Authorization": f"Bearer {access_token}"},
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        event = resp.json()
    except Exception as exc:
        print(f"[google_calendar] event creation failed: {exc}")
        return None

    meet_link = event.get("hangoutLink")
    if not meet_link:
        for entry in (event.get("conferenceData", {}) or {}).get("entryPoints", []) or []:
            if entry.get("entryPointType") == "video":
                meet_link = entry.get("uri")
                break
    if not meet_link:
        # Event was created but Google didn't attach a Meet link -- still
        # report the event itself; the caller keeps the round's static
        # fallback link rather than pointing "Join Interview" at nothing.
        print(f"[google_calendar] event {event.get('id')} created with no Meet link in the response")
        return {"event_id": event.get("id"), "meet_link": None, "html_link": event.get("htmlLink")}

    return {"event_id": event.get("id"), "meet_link": meet_link, "html_link": event.get("htmlLink")}


def delete_event(event_id: str) -> bool:
    """Best-effort delete of a Google Calendar event this app created via
    create_event_with_meet -- e.g. when an interview is cancelled. Same
    "never raises, returns falsy if not connected/fails" contract as
    create_event_with_meet."""
    access_token = _get_valid_access_token()
    if not access_token or not event_id:
        return False
    try:
        resp = requests.delete(
            f"{_EVENTS_URL}/{event_id}",
            params={"sendUpdates": "none"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        # 404/410 means it's already gone (e.g. deleted manually) -- treat as success.
        if resp.status_code not in (204, 404, 410):
            resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[google_calendar] event deletion failed: {exc}")
        return False
