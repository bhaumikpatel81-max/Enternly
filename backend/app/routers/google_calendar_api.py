"""
Admin-facing connect/disconnect for the single shared Google Calendar
connection (see services/google_calendar.py for the OAuth + event-creation
logic, and scheduling_api.confirm_pick() for where the generated Meet link
actually gets used).

/api/google/connect and /api/google/status are normal JWT-authenticated
endpoints (admin/ta_manager only). /api/google/callback is the one public
exception -- Google redirects the admin's browser there directly with just
a `?code=&state=` query string, no Authorization header -- so it's listed in
main.py's _PUBLIC set and instead authenticates the *request* via the
one-time `state` nonce (created only by an already-authenticated connect()
call, consumed exactly once here).
"""
import secrets
from urllib.parse import quote as _urlquote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from ..db import query, query_one
from ..auth_utils import get_current_user, is_company_tier
from ..services import google_calendar as gcal
from .enteri_ai_api import _get_base_url

router = APIRouter(prefix="/api/google", tags=["google-calendar"])

_STATE_TTL_MINUTES = 10


def _require_admin(user: dict):
    if not is_company_tier(user):
        raise HTTPException(403, "Company Admin access required")


@router.get("/status")
def status(user: dict = Depends(get_current_user)):
    _require_admin(user)
    conn = gcal.get_connection(user.get("tenant_id"))
    return {
        "configured": gcal.is_configured(),
        "connected": conn is not None,
        "google_email": conn and conn.get("google_email"),
        "connected_at": conn and conn.get("created_at"),
    }


@router.get("/connect")
def connect(user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not gcal.is_configured():
        raise HTTPException(
            400,
            "Google OAuth isn't configured yet -- set GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/"
            "GOOGLE_REDIRECT_URI in .env.prod first.",
        )
    query("DELETE FROM google_oauth_state WHERE created_at < now() - interval '{} minutes'".format(_STATE_TTL_MINUTES), fetch=False)
    state = secrets.token_urlsafe(32)
    query(
        "INSERT INTO google_oauth_state (state, created_by) VALUES (%s, %s)",
        [state, user["sub"]], fetch=False,
    )
    auth_url = gcal.build_auth_url(state)
    return {"auth_url": auth_url}


@router.get("/callback")
def callback(code: str = None, state: str = None, error: str = None):
    """Never let this endpoint crash to a raw 500 -- it's the tail end of an
    interactive admin flow with no other way to see what went wrong, so
    EVERY exception anywhere in here must still end in a redirect back to
    the SPA carrying the real reason, not a blank error page."""
    try:
        base_url, _src = _get_base_url()
    except Exception:
        import os
        # TODO: set APP_BASE_URL in .env.prod to your real production domain --
        # this placeholder is not a live address.
        base_url = os.environ.get("APP_BASE_URL", "").rstrip("/") or "https://your-enternly-domain.example"

    try:
        if error:
            return RedirectResponse(f"{base_url}/?gcalError={_urlquote(error, safe='')}")
        if not code or not state:
            return RedirectResponse(f"{base_url}/?gcalError=missing_code_or_state")

        row = query_one("SELECT * FROM google_oauth_state WHERE state = %s", [state])
        # One-time use regardless of outcome -- a replayed/guessed state must
        # never be accepted twice.
        query("DELETE FROM google_oauth_state WHERE state = %s", [state], fetch=False)
        if not row:
            return RedirectResponse(f"{base_url}/?gcalError=invalid_or_expired_state")

        result = gcal.exchange_code_and_store(code, connected_by=str(row["created_by"]))
        return RedirectResponse(f"{base_url}/?gcalConnected={result['google_email']}")
    except Exception as exc:
        print(f"[google_calendar] connect failed: {exc}")
        # Surface the actual reason (Google's own error code/description, our
        # RuntimeError text, or now a raw DB/library error too) in the flash
        # message itself -- a bare "connect_failed" with no detail meant every
        # failure needed a server log to diagnose, which most people testing
        # this don't have handy.
        detail = str(exc)[:300]
        return RedirectResponse(f"{base_url}/?gcalError={_urlquote(detail, safe='')}")


@router.post("/disconnect")
def disconnect(user: dict = Depends(get_current_user)):
    _require_admin(user)
    gcal.disconnect(user.get("tenant_id"))
    return {"ok": True}
