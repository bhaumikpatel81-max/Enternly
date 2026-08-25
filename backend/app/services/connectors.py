"""
External connectors.

Google Calendar + Meet (Phase 3): real implementation.
  schedule_meeting uses the acting recruiter's stored OAuth token to check
  free/busy, create a Calendar event with a Meet link, and refresh the token
  automatically when it expires. Raises ValueError if the recruiter has not
  linked their Google account — the caller surfaces that as a 400.

Gmail, AI interview bot, Darwinbox: clearly-marked stubs for later phases.
"""
import os
import re as _re
import uuid
from typing import Optional
from datetime import datetime, timedelta, timezone

from ..db import query, query_one

# Every scheduling timestamp is stored/passed around in UTC, but recruiters,
# candidates and panelists are all IST-based -- convert at the point of
# display (emails, in-app notification text) rather than at storage, so the
# DB/ICS layer stays timezone-unambiguous.
IST = timezone(timedelta(hours=5, minutes=30))


def to_ist(dt: datetime) -> datetime:
    """Convert a tz-aware (or naive, assumed-UTC) datetime to IST for display."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


# ------------------------------------------------------------------ #
#  CALENDAR INVITES  (ICS over SMTP — no Google API)                  #
# ------------------------------------------------------------------ #

def _ics_escape(text: str) -> str:
    """Escape a value per RFC 5545 (commas, semicolons, backslashes, newlines)."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def build_ics(
    summary: str,
    description: str,
    start_dt_utc: datetime,
    duration_min: int,
    organizer_email: str,
    attendee_emails: list,
    location: str = "",
    uid: Optional[str] = None,
    sequence: int = 0,
    cancelled: bool = False,
) -> str:
    """
    Build a minimal, valid VCALENDAR/VEVENT string.
    start_dt_utc must be a UTC datetime (naive treated as UTC).

    sequence: bump this (keeping the same uid) when re-sending an invite for
    an interview that already had one -- e.g. a reschedule or cancellation.
    Calendar clients (Gmail/Outlook) use matching UID + a higher SEQUENCE to
    update the existing event in place instead of creating a duplicate.

    cancelled: emits METHOD:CANCEL / STATUS:CANCELLED instead of
    METHOD:REQUEST / STATUS:CONFIRMED -- calendar clients that already added
    this UID (via a real calendar sync or Gmail's "Events from Gmail"
    heuristic) remove it on receipt instead of leaving a stale entry behind
    after the interview is cancelled in the app.
    """
    end_dt = start_dt_utc + timedelta(minutes=duration_min)
    fmt = "%Y%m%dT%H%M%SZ"
    stamp = datetime.utcnow().strftime(fmt)
    ev_uid = uid or f"{uuid.uuid4().hex}@enternly-ats"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//EnternsTech//Enternly//EN",
        "CALSCALE:GREGORIAN",
        f"METHOD:{'CANCEL' if cancelled else 'REQUEST'}",
        "BEGIN:VEVENT",
        f"UID:{ev_uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{start_dt_utc.strftime(fmt)}",
        f"DTEND:{end_dt.strftime(fmt)}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(location)}",
        f"ORGANIZER;CN=EnternsTech Talent Acquisition:mailto:{organizer_email}",
        f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
        f"SEQUENCE:{int(sequence or 0)}",
        "TRANSP:OPAQUE",
    ]
    for em in attendee_emails:
        if em:
            lines.append(
                f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{em}"
            )
    if not cancelled:
        lines += [
            "BEGIN:VALARM",
            "TRIGGER:-PT30M",
            "ACTION:DISPLAY",
            "DESCRIPTION:Interview reminder",
            "END:VALARM",
        ]
    lines += [
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines)


def send_calendar_invite(
    to_emails: list,
    subject: str,
    body_text: str,
    start_dt_utc: datetime,
    duration_min: int,
    location: str = "",
    reply_to: Optional[str] = None,
    attachments: Optional[list] = None,
    html_body: Optional[str] = None,
    uid: Optional[str] = None,
    sequence: int = 0,
    cancelled: bool = False,
    tenant_id: str = None,
) -> dict:
    """
    Send a calendar invite (.ics attached) from hr@amnex.com to each recipient.
    One email per recipient so the candidate never sees the panel list.
    Falls back to a plain email if SMTP isn't configured (logged, never raises here).

    attachments: optional list of (filename, bytes, mimetype) tuples — e.g. the
    candidate's CV — attached alongside the .ics on every copy of this invite.

    html_body: optional branded HTML rendering of the same invite. Placed as an
    additional multipart/alternative part BEFORE the text/calendar part (not
    replacing it) — calendar-aware clients (Gmail/Outlook) still pick the
    text/calendar part and show their native Accept/Decline + auto-add-to-
    calendar UI; anything that ignores/strips that part falls back to the
    branded HTML instead of plain text.

    uid: pass the SAME value across two calls (e.g. one for the candidate, one
    for the panel) so both sides see one logical calendar event rather than
    two independently-generated ones.

    sequence: forwarded to build_ics -- bump on a reschedule so the SAME uid
    with a higher sequence updates the existing calendar entry in place.
    """
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders as _enc

    cfg = _load_email_cfg(tenant_id)
    if not (cfg["user"] and cfg["password"]):
        print(f"[calendar] SMTP not configured — invite NOT sent to {to_emails}")
        return {"sent": False, "stub": True, "to": to_emails}

    organizer = cfg["user"]  # hr@amnex.com
    shared_uid = uid or f"{uuid.uuid4().hex}@enternly-ats"
    ics_text = build_ics(
        summary=subject,
        description=body_text,
        start_dt_utc=start_dt_utc,
        duration_min=duration_min,
        organizer_email=organizer,
        attendee_emails=to_emails,
        location=location,
        sequence=sequence,
        uid=shared_uid,
        cancelled=cancelled,
    )

    sent_ok = []
    for to_email in to_emails:
        if not to_email:
            continue
        # multipart/mixed: text/html/calendar alternatives + .ics attachment (Outlook-friendly)
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = f"{cfg['from_name']} <{organizer}>"
        msg["To"]      = to_email
        if reply_to:
            msg["Reply-To"] = reply_to

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain", "utf-8"))
        if html_body:
            alt.attach(MIMEText(html_body, "html", "utf-8"))
        cal_part = MIMEText(ics_text, "calendar", "utf-8")
        cal_method = "CANCEL" if cancelled else "REQUEST"
        cal_part.add_header("Content-Type", f'text/calendar; method={cal_method}; charset="utf-8"')
        alt.attach(cal_part)
        msg.attach(alt)

        ics_attach = MIMEBase("application", "ics")
        ics_attach.set_payload(ics_text.encode("utf-8"))
        _enc.encode_base64(ics_attach)
        ics_attach.add_header("Content-Disposition", 'attachment; filename="invite.ics"')
        msg.attach(ics_attach)

        for fname, fbytes, fmime in (attachments or []):
            maintype, _, subtype = (fmime or "application/octet-stream").partition("/")
            part = MIMEBase(maintype or "application", subtype or "octet-stream")
            part.set_payload(fbytes)
            _enc.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
            msg.attach(part)

        try:
            _send_smtp(cfg, to_email, msg)
            print(f"[calendar] invite sent TO: {to_email}")
            sent_ok.append(to_email)
        except Exception as exc:
            print(f"[calendar] invite FAILED to {to_email}: {exc}")

    return {"sent": bool(sent_ok), "to": sent_ok, "via": "smtp_ics"}


# ------------------------------------------------------------------ #
#  CANDIDATE CV ATTACHMENT — read the resume off disk for emails      #
# ------------------------------------------------------------------ #

_ATTACH_UPLOADS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "uploads")
)
_ATTACH_CV_STORE_DIR = os.environ.get("CV_STORE_DIR", "/app/cv_store")
_ATTACH_RESUME_MIME = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
}


def load_candidate_cv_attachment(candidate_id: str) -> Optional[tuple]:
    """
    Read the candidate's resume off disk (same lookup main.py's serve_resume
    uses: uploads/ then cv_store/ fallback) for attaching to an email.
    Returns (filename, bytes, mimetype) or None if there's no CV on file.
    """
    row = query_one("SELECT resume_url FROM candidate WHERE id = %s", [candidate_id])
    resume_url = row and row.get("resume_url")
    if not resume_url:
        return None
    safe_name = os.path.basename(resume_url)
    for d in (_ATTACH_UPLOADS_DIR, _ATTACH_CV_STORE_DIR):
        path = os.path.join(d, safe_name)
        if os.path.isfile(path):
            ext = os.path.splitext(safe_name)[1].lower()
            mime = _ATTACH_RESUME_MIME.get(ext, "application/octet-stream")
            with open(path, "rb") as f:
                return (safe_name, f.read(), mime)
    return None


def schedule_meeting(organizer_email: str, candidate_email: str,
                     panel_emails: list, start_time: datetime,
                     duration_min: int = 45, meet_link: str = "",
                     candidate_name: str = "Candidate",
                     job_title: str = "the role",
                     candidate_html: Optional[str] = None,
                     panel_html: Optional[str] = None,
                     candidate_attachments: Optional[list] = None,
                     panel_attachments: Optional[list] = None,
                     uid: Optional[str] = None,
                     sequence: int = 0,
                     tenant_id: str = None) -> dict:
    """
    Create a calendar invite (.ics over SMTP) from hr@amnex.com and email it
    to the candidate + each panel member. No Google API — meet_link is
    whatever the recruiter configured on the round (or passed explicitly),
    not one generated by Google's Calendar API.

    Sent as two separate sends (candidate vs. panel/HM) sharing one calendar
    UID, so each side gets its own branded HTML + its own attachments (e.g.
    the candidate's own CV only makes sense on the panel/HM copies) while
    still reading as one logical event, not two.

    meet_link: optional video URL (Google Meet/Jitsi/Teams/Zoom) configured by
    the recruiter on the round; used verbatim as both the ICS location and
    (via candidate_html/panel_html's own "Join Interview" button) the join link.
    Returns the same shape callers already expect (gcal_event_id is None now).

    uid/sequence: pass the interview's persisted calendar_uid + ics_sequence
    on a reschedule (rather than leaving uid unset, which generates a fresh
    one every call) so calendar clients update the existing event in place
    instead of showing a duplicate.
    """
    when = to_ist(start_time).strftime("%A, %d %B %Y at %I:%M %p IST")
    location = meet_link or "To be confirmed"
    body = (
        f"You are invited to an interview for {job_title}.\n\n"
        f"When: {when}\n"
        f"Duration: {duration_min} minutes\n"
        f"Join link: {meet_link or 'will be shared separately'}\n\n"
        f"Please accept this invite to confirm.\n\n"
        f"— EnternsTech Talent Acquisition"
    )
    subject = f"Interview – {candidate_name} – {job_title}"
    shared_uid = uid or f"{uuid.uuid4().hex}@enternly-ats"
    panel_only = [e for e in (panel_emails or []) if e and e != candidate_email]

    cand_result = send_calendar_invite(
        to_emails=[candidate_email] if candidate_email else [],
        subject=subject, body_text=body, html_body=candidate_html,
        start_dt_utc=start_time, duration_min=duration_min, location=location,
        reply_to=organizer_email, attachments=candidate_attachments, uid=shared_uid,
        sequence=sequence, tenant_id=tenant_id,
    )
    panel_result = send_calendar_invite(
        to_emails=panel_only,
        subject=subject, body_text=body, html_body=panel_html,
        start_dt_utc=start_time, duration_min=duration_min, location=location,
        reply_to=organizer_email, attachments=panel_attachments, uid=shared_uid,
        sequence=sequence, tenant_id=tenant_id,
    )

    all_emails = ([candidate_email] if candidate_email else []) + panel_only
    sent_to = (cand_result.get("to") or []) + (panel_result.get("to") or [])
    missing = [e for e in all_emails if e not in sent_to]
    return {
        "gcal_event_id": None,
        "meet_link":     meet_link,
        "scheduled_at":  start_time.isoformat(),
        "conflicts":     [],
        # Real signal instead of an assumed success -- True only when every
        # invited recipient actually received the invite. Callers must
        # surface a warning (not a success toast) when this is False,
        # while still keeping whatever record they already created.
        "invite_sent":    bool(sent_to) and not missing,
        "invite_stub":    cand_result.get("stub", False) or panel_result.get("stub", False),
        "invite_sent_to": sent_to,
        "invite_missing": missing,
    }


def send_calendar_cancellation(
    candidate_email: str, panel_emails: list, start_time: datetime,
    duration_min: int, candidate_name: str, job_title: str,
    uid: Optional[str], sequence: int,
    reply_to: Optional[str] = None,
    tenant_id: str = None,
) -> dict:
    """
    Cancel a previously-sent calendar invite: emails every recipient a
    METHOD:CANCEL .ics using the SAME uid + a bumped sequence, so calendar
    clients that added the original invite (Google Calendar sync, Outlook,
    or Gmail's "Events from Gmail" heuristic) remove it automatically --
    otherwise the event sits on everyone's calendar forever after
    interview.status flips to 'cancelled' in the app.
    """
    when = to_ist(start_time).strftime("%A, %d %B %Y at %I:%M %p IST")
    body = (
        f"Your interview for {job_title}, originally scheduled for {when}, has been cancelled.\n\n"
        f"— EnternsTech Talent Acquisition"
    )
    subject = f"Cancelled: Interview – {candidate_name} – {job_title}"
    shared_uid = uid or f"{uuid.uuid4().hex}@enternly-ats"
    panel_only = [e for e in (panel_emails or []) if e and e != candidate_email]

    cand_result = send_calendar_invite(
        to_emails=[candidate_email] if candidate_email else [],
        subject=subject, body_text=body,
        start_dt_utc=start_time, duration_min=duration_min,
        reply_to=reply_to, uid=shared_uid, sequence=sequence, cancelled=True,
        tenant_id=tenant_id,
    )
    panel_result = send_calendar_invite(
        to_emails=panel_only,
        subject=subject, body_text=body,
        start_dt_utc=start_time, duration_min=duration_min,
        reply_to=reply_to, uid=shared_uid, sequence=sequence, cancelled=True,
        tenant_id=tenant_id,
    )
    sent_to = (cand_result.get("to") or []) + (panel_result.get("to") or [])
    return {"sent_to": sent_to}


# ------------------------------------------------------------------ #
#  EMAIL  (SMTP — reads SMTP_USER / SMTP_PASSWORD from env)            #
# ------------------------------------------------------------------ #

def resolve_global_placeholders(
    req_id: Optional[str] = None,
    actor: Optional[dict] = None,
) -> dict:
    """
    Resolve the three global placeholders injected into every email template.

    company_name   → system_settings['company_name']
    recruiter_name → (1) actor['full_name'] if provided,
                     (2) first assigned recruiter for req_id,
                     (3) system_settings['ta_default_signature']
    recruiter_email → (1) actor['email'] if provided,
                      (2) first assigned recruiter's email for req_id,
                      (3) empty string

    Never raises — returns safe defaults on any error.
    """
    # Resolve which tenant's company_name/signature to use -- the acting
    # user's own tenant_id if we have one, else the requisition's, else fall
    # back to an unscoped read (today's single-tenant behaviour).
    tenant_id = None
    if actor and isinstance(actor, dict):
        tenant_id = actor.get("tenant_id")
    if not tenant_id and req_id:
        try:
            req_row = query_one("SELECT tenant_id FROM requisition WHERE id=%s", [req_id])
            tenant_id = (req_row or {}).get("tenant_id")
        except Exception:
            pass

    try:
        if tenant_id:
            rows = query(
                "SELECT key, value FROM system_settings WHERE tenant_id=%s AND key IN ('company_name','ta_default_signature')",
                [tenant_id],
            )
        else:
            rows = query(
                "SELECT key, value FROM system_settings WHERE key IN ('company_name','ta_default_signature')"
            )
        cfg = {r["key"]: (r["value"] or "").strip() for r in (rows or [])}
    except Exception:
        cfg = {}

    company_name = cfg.get("company_name") or "EnternsTech Pvt. Ltd."
    ta_sig       = cfg.get("ta_default_signature") or "Talent Acquisition Team"

    recruiter_name  = None
    recruiter_email = None

    # 1. Logged-in actor
    if actor and isinstance(actor, dict):
        recruiter_name  = (actor.get("full_name") or actor.get("name") or "").strip() or None
        recruiter_email = (actor.get("email") or "").strip() or None

    # 2. Assigned recruiter for the requisition
    if req_id and not (recruiter_name and recruiter_email):
        try:
            rec = query_one(
                """SELECT u.full_name, u.email
                   FROM requisition_recruiter rr
                   JOIN app_user u ON u.id = rr.recruiter_id
                   WHERE rr.requisition_id = %s AND u.is_active = TRUE
                   ORDER BY rr.created_at LIMIT 1""",
                [req_id],
            )
            if rec:
                recruiter_name  = recruiter_name  or (rec["full_name"] or "").strip() or None
                recruiter_email = recruiter_email or (rec["email"]     or "").strip() or None
        except Exception:
            pass

    # 3. Fallback
    recruiter_name  = recruiter_name  or ta_sig
    recruiter_email = recruiter_email or ""

    return {
        "company_name":    company_name,
        "recruiter_name":  recruiter_name,
        "recruiter_email": recruiter_email,
    }


def _load_email_cfg(tenant_id: str = None) -> dict:
    """
    Load all email config from system_settings (DB first, env vars as fallback).
    Returns a dict with keys: user, password, host, port, from_name, base_url.

    system_settings is tenant-scoped (Migration 96) -- tenant_id is optional
    only for the callers that genuinely have no per-item tenant context yet
    (a handful of background loops, tracked as follow-up work); every other
    caller should pass the tenant whose mailbox this send belongs to.
    """
    db: dict = {}
    try:
        from ..db import query as _q
        if tenant_id:
            rows = _q("SELECT key, value FROM system_settings WHERE tenant_id = %s", [tenant_id])
        else:
            rows = _q("SELECT key, value FROM system_settings")
        db = {r["key"]: (r["value"] or "").strip() for r in (rows or [])}
    except Exception as exc:
        print(f"[email] WARNING: could not read system_settings: {exc}")

    def _g(key, env_key, default=""):
        return (db.get(key) or os.environ.get(env_key, default) or "").strip()

    raw_pass = _g("smtp_password", "SMTP_PASSWORD")
    return {
        "user":      _g("smtp_user",      "SMTP_USER"),
        "password":  raw_pass.replace(" ", ""),   # strip spaces from App Passwords
        "host":      _g("smtp_host",      "SMTP_HOST",      "smtp.gmail.com"),
        "port":      int(_g("smtp_port",  "SMTP_PORT",      "587") or "587"),
        "from_name": _g("smtp_from_name", "SMTP_FROM_NAME", "Enternly (EnternsTech Talent Acquisition Team)"),
        "base_url":  _g("app_base_url",   "APP_BASE_URL",   "http://localhost:8000"),
    }

# keep old name as alias so existing callers don't break
_load_smtp_settings = _load_email_cfg




def _send_smtp(cfg: dict, to_email: str, msg_obj) -> None:
    """Inner SMTP send — tries TLS (587) first, then SSL (465) as fallback."""
    import smtplib

    host = cfg["host"]
    port = cfg["port"]
    user = cfg["user"]
    pwd  = cfg["password"]
    tls_err_msg = ""

    # Primary: STARTTLS on port 587
    try:
        with smtplib.SMTP(host, port, timeout=8) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(user, pwd)
            s.sendmail(user, [to_email], msg_obj.as_string())
        return
    except smtplib.SMTPAuthenticationError:
        raise
    except Exception as exc:
        tls_err_msg = str(exc)
        print(f"[email] TLS port {port} failed ({exc}), trying SSL 465…")

    # Fallback: SSL on port 465
    try:
        with smtplib.SMTP_SSL(host, 465, timeout=8) as s:
            s.ehlo()
            s.login(user, pwd)
            s.sendmail(user, [to_email], msg_obj.as_string())
    except Exception as ssl_err:
        raise RuntimeError(
            f"TLS port {port}: {tls_err_msg} | SSL port 465: {ssl_err}"
        )


def _real_send_enabled() -> bool:
    return os.environ.get("EMAIL_REAL_SEND_ENABLED", "").strip().lower() in ("1", "true", "yes")


def send_email(
    to_email: str,
    subject: str,
    body: str,
    html: str = None,
    reply_to: str = None,
    attachments: Optional[list] = None,
    tenant_id: str = None,
) -> dict:
    """
    Send email.  Priority:
      1. Gmail / SMTP  (set smtp_user + smtp_password in Settings)
      2. Stub          (logs to console only — not configured)

    reply_to: if set, adds Reply-To header so candidate replies go to the recruiter.
    attachments: optional list of (filename, bytes, mimetype) tuples.

    Non-prod safety gate (added 2026-08, after real test proctoring-alert
    emails were accidentally sent from this repo's local dev stack): this
    codebase's own ENV var is NOT a reliable prod/non-prod signal —
    docker-compose.dev.yml deliberately loads .env.prod as its env_file (to
    keep API keys in sync), so a dev container can and does report ENV=prod
    and carry real SMTP credentials. Real transmission therefore requires an
    explicit, dedicated opt-in — EMAIL_REAL_SEND_ENABLED=true — set ONLY in
    docker-compose.prod.yml's own environment: block (which takes precedence
    over env_file:), never in the shared .env.prod file itself. Every
    environment that hasn't set this (including a misconfigured/shared-secret
    dev box) is safe by default: sends are logged, not transmitted, unless
    EMAIL_TEST_REDIRECT names a safe address to actually deliver to instead.
    """
    import smtplib

    # Defensive sweep — strip any unresolved {{placeholders}} before sending.
    # This is the last line of defence after all caller substitutions have run.
    subject = _re.sub(r'\{\{[^}]+\}\}', '', subject)
    body    = _re.sub(r'\{\{[^}]+\}\}', '', body)
    if html:
        html = _re.sub(r'\{\{[^}]+\}\}', '', html)

    cfg = _load_email_cfg(tenant_id)

    if not _real_send_enabled():
        redirect = os.environ.get("EMAIL_TEST_REDIRECT", "").strip()
        if not redirect:
            print(
                f"[email] SUPPRESSED (EMAIL_REAL_SEND_ENABLED not set) — "
                f"would have sent TO: {to_email} | SUBJECT: {subject}\n"
                f"--- BODY ---\n{body}\n--- END BODY ---"
            )
            return {"sent": False, "suppressed_non_prod": True, "to": to_email}
        print(
            f"[email] NON-PROD REDIRECT (EMAIL_REAL_SEND_ENABLED not set) — "
            f"original TO: {to_email} | redirecting delivery to: {redirect} | SUBJECT: {subject}"
        )
        to_email = redirect

    # ── SMTP only (all mail sent from SMTP_USER, i.e. hr@amnex.com) ───────────
    if cfg["user"] and cfg["password"]:
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders as _enc
        msg = MIMEMultipart("mixed") if attachments else MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{cfg['from_name']} <{cfg['user']}>"
        msg["To"]      = to_email
        if reply_to:
            msg["Reply-To"] = reply_to
        if attachments:
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(body, "plain", "utf-8"))
            if html:
                alt.attach(MIMEText(html, "html", "utf-8"))
            msg.attach(alt)
            for fname, fbytes, fmime in attachments:
                maintype, _, subtype = (fmime or "application/octet-stream").partition("/")
                part = MIMEBase(maintype or "application", subtype or "octet-stream")
                part.set_payload(fbytes)
                _enc.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
                msg.attach(part)
        else:
            msg.attach(MIMEText(body, "plain", "utf-8"))
            if html:
                msg.attach(MIMEText(html, "html", "utf-8"))
        try:
            _send_smtp(cfg, to_email, msg)
            print(f"[email] SMTP sent TO: {to_email}")
            return {"sent": True, "to": to_email, "via": "smtp"}
        except smtplib.SMTPAuthenticationError:
            raise RuntimeError(
                "Gmail rejected the App Password — check Settings."
            )
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    # ── 2. Stub ───────────────────────────────────────────────────────────────
    print(f"[email] Not configured — skipping send TO: {to_email} | {subject}")
    return {"sent": False, "stub": True, "to": to_email}


# ------------------------------------------------------------------ #
#  AI INTERVIEW BOT  (stub — Phase 5)                                  #
# ------------------------------------------------------------------ #

def run_bot_interview(candidate_id: str, job_description: str) -> dict:
    """
    STUB: replace body with real AI bot call.
    Assistive only — bot scores and ranks, but a human makes every advance/reject.
    """
    seed  = sum(ord(c) for c in str(candidate_id))
    score = 50 + (seed % 50)
    return {
        "bot_score": float(score),
        "summary":   "Stubbed bot interview summary (replace in production).",
    }


# ------------------------------------------------------------------ #
#  DARWINBOX  (stub — Phase 6)                                         #
# ------------------------------------------------------------------ #

def push_offer_to_darwin(offer: dict) -> dict:
    """
    STUB — Darwinbox offer handoff.  Replace the body of this function with the
    real Darwinbox REST API call when the integration is ready.

    ── What the dev team needs to wire this up ──────────────────────────────────

    1. API base URL
       Darwinbox provides a tenant-specific base URL, typically:
         https://<your-tenant>.darwinbox.in/apiv2/
       Obtain this from your Darwinbox implementation partner or admin portal.

    2. Authentication
       Darwinbox uses OAuth 2.0 client credentials for API access:
         POST /oauth/token
           grant_type    = client_credentials
           client_id     = <from Darwinbox admin panel>
           client_secret = <from Darwinbox admin panel>
       Store client_id and client_secret in system_settings or .env.prod,
       NOT hard-coded here.  Bearer token expires — implement token caching.

    3. Payload format (candidate offer)
       The exact field names depend on your Darwinbox module configuration.
       Typical fields for an offer record:
         {
           "employee_code":    "<auto-assigned or passed>",
           "first_name":       "<from candidate>",
           "last_name":        "<from candidate>",
           "email":            "<candidate email>",
           "designation":      offer["designation"],
           "date_of_joining":  offer["joining_date"],  // "YYYY-MM-DD"
           "cost_to_company":  offer["total_ctc"],      // annual, numeric
           "department":       "<from requisition BU>",
           "location":         "<from requisition>",
           "employment_type":  "full_time" | "contract",
         }
       Confirm exact keys with Darwinbox during integration testing.

    4. Endpoint
       POST /apiv2/employee/create  (or /apiv2/offer/create — verify with Darwinbox)
       Headers:
         Authorization: Bearer <access_token>
         Content-Type:  application/json

    5. Response
       On success Darwinbox returns an employee/offer ID — store that in
       offer.darwin_ref so you can look up the record later.

    6. Error handling
       - 401: token expired, refresh and retry once
       - 422: payload validation error — log the full response body
       - 5xx: transient — use exponential back-off (max 3 retries)

    7. Security note
       All Darwinbox credentials MUST live in system_settings or .env.prod.
       Never commit credentials to source control.

    ─────────────────────────────────────────────────────────────────────────────
    Until the integration is wired up, this function logs the payload and returns
    a synthetic reference so the rest of the approval workflow completes normally.
    The darwin_ref stored in the offer table will start with "STUB-" — the dev
    team can query `SELECT * FROM offer WHERE darwin_ref LIKE 'STUB-%'` to find
    all offers that still need real Darwinbox pushes after go-live.
    """
    stub_ref = f"STUB-DRW-{uuid.uuid4().hex[:8].upper()}"
    print(
        f"[darwinbox STUB] push_offer_to_darwin called — offer_id={offer.get('id')} "
        f"candidate='{offer.get('candidate')}' designation='{offer.get('designation')}' "
        f"total_ctc={offer.get('total_ctc')} joining_date={offer.get('joining_date')} "
        f"darwin_ref_assigned={stub_ref}"
    )
    return {
        "darwin_pushed": True,
        "darwin_ref":    stub_ref,
    }
