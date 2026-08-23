# Enternly — Step-by-Step Change Handoff

Four changes, in safe order. Each step is self-contained: make the change, run, verify, then move on. Don't start a step until the previous one runs clean.

Files referenced below live alongside this README. Each contains a **FIND** (existing code) and **REPLACE WITH** (new code), or a full new file.

---

## What's realistic vs. not (read first)

| Your ask | Verdict | Notes |
|---|---|---|
| All email from `hr@amnex.com` via SMTP | ✅ Easy | SMTP already works; just force it on + lock From. |
| Auto **calendar invite** with a link | ✅ via ICS | hr@amnex.com sends a real `.ics` invite. The "link" is a **static meeting URL you paste** (Jitsi/Teams/Zoom/Meet) — ICS cannot auto-create a video room. |
| Auto **transcript of the panel interview** + report to recruiter | ⚠️ Partial | A live human-panel call can only be auto-transcribed via the meeting vendor (Google Meet → Drive, which is the Google dependency you're removing). **Realistic path:** keep auto-transcript+report for the **NexAI bot interview** (already built and emailed to the recruiter), and for panel rounds either keep Google Meet transcripts OR add a manual "upload recording → Whisper" button (Step 3 covers the Whisper engine you'd reuse). |
| OpenAI for the bot brain | ✅ Trivial | Bot already uses the OpenAI SDK pointed at Groq. Change 2 env vars. |
| OpenAI Whisper for candidate speech | ✅ New code | Today the browser transcribes for free. Whisper = server-side, better accuracy. Step 3 adds it. |
| Self-service password create/reset via email | ✅ New feature | Step 4: token table + 3 endpoints + 2 emails + 1 page. |

---

## Order of work

- **Step 1** — Force SMTP-only, lock From to hr@amnex.com, harden JWT secret. (Lowest risk, do first.)
- **Step 2** — Replace Google Calendar scheduling with ICS-over-SMTP invites.
- **Step 3** — Point the bot at OpenAI (env only) + add Whisper transcription endpoint.
- **Step 4** — Self-service password create / forgot / reset emails from hr@amnex.com.

After every step: `docker compose -f docker-compose.prod.yml --profile prod up --build`, hit `/api/health`, then the step's specific test.

---

## Decision you must make before Step 2/3

**The Meet-transcript feature (Google Drive).** Pick one:

- **(A) Drop it** — fully Google-free. The bot interview still emails its own transcript+score to the recruiter. Panel rounds get no auto-transcript (recruiters write scorecards manually, as the app already supports).
- **(B) Keep it** — leave `google_oauth.py` + `transcript_service.py` + `transcript_api.py` untouched; recruiters do a one-click Google connect just for pulling Meet transcripts. Email/scheduling/bot are unaffected.

Steps 1–4 work identically either way. If (A), also do `01b_drop_google_transcripts.md`.
# Step 1 — Force SMTP-only, lock From to hr@amnex.com, harden JWT

## 1A. `backend/app/services/connectors.py` — make `send_email` SMTP-only

The function currently tries SendGrid first. We make SMTP the only path. From address is already `cfg["user"]` (= `hr@amnex.com` once your .env.prod sets `SMTP_USER=hr@amnex.com`), so no From change needed.

### FIND (the whole SendGrid branch inside `send_email`):
```python
    cfg = _load_email_cfg()

    # ── 1. SendGrid ──────────────────────────────────────────────────────────
    if cfg["sendgrid_api_key"]:
        from_email = cfg["user"] or "noreply@amnex.io"
        _send_via_sendgrid(
            cfg["sendgrid_api_key"], from_email, cfg["from_name"],
            to_email, subject, body, html, reply_to=reply_to,
        )
        return {"sent": True, "to": to_email, "via": "sendgrid"}

    # ── 2. SMTP ───────────────────────────────────────────────────────────────
    if cfg["user"] and cfg["password"]:
```

### REPLACE WITH:
```python
    cfg = _load_email_cfg()

    # ── SMTP only (all mail sent from SMTP_USER, i.e. hr@amnex.com) ───────────
    if cfg["user"] and cfg["password"]:
```

> Leave the rest of the SMTP block and the stub fallback exactly as-is.
> You can optionally delete `_send_via_sendgrid` and the `sendgrid_api_key` line in `_load_email_cfg`, but it's harmless to leave them unused. Cleanest: remove them.

## 1B. `backend/app/routers/admin_users.py` — Settings test-email: SMTP only

### FIND the SendGrid block in the test-email endpoint (around line 295–314):
```python
        if resp.status_code in (200, 202):
            return {"ok": True, "sent_to": to_addr, "method": "SendGrid"}
        ...
        raise HTTPException(400, f"SendGrid error {resp.status_code}: {resp.text[:300]}")
```
Delete the entire SendGrid attempt (the `if sendgrid_key:` block that precedes the `# ── SMTP fallback ──` comment). Keep everything from `# ── SMTP fallback ──` onward — that's now the only path. Also remove the now-unused SendGrid imports/variables in that function if any.

## 1C. Lock the From address to hr@amnex.com (defensive)

In `connectors.py` inside `send_email`, the SMTP `From` line is:
```python
        msg["From"]    = f"{cfg['from_name']} <{cfg['user']}>"
```
This already uses `SMTP_USER`. As long as `.env.prod` has `SMTP_USER=hr@amnex.com`, every email is from hr@amnex.com. No change needed — just confirm your `.env.prod`.

> NOTE on Gmail/Workspace: an app password authenticates as the mailbox it belongs to. If the app password is for `hr@amnex.com`, Gmail will only let you send *as* `hr@amnex.com` (or verified aliases). So the From is enforced by Google too.

## 1D. Harden the JWT secret — `backend/app/auth_utils.py`

Today it falls back to a hardcoded dev secret. In prod every deploy would share the same signing key. Require the env var.

### FIND:
```python
SECRET_KEY = os.environ.get("JWT_SECRET", "enternly-dev-secret-change-in-prod")
```

### REPLACE WITH:
```python
SECRET_KEY = os.environ.get("JWT_SECRET", "").strip()
if not SECRET_KEY:
    # Allow a dev default ONLY when not in production.
    if os.environ.get("ENV", "").lower() in ("prod", "production"):
        raise RuntimeError(
            "JWT_SECRET is not set. Add a long random JWT_SECRET to .env.prod "
            "before starting in production."
        )
    SECRET_KEY = "enternly-dev-secret-change-in-prod"
```

Make sure `.env.prod` contains `ENV=prod` and a real `JWT_SECRET` (generate with `python -c "import secrets;print(secrets.token_urlsafe(48))"`).

## VERIFY Step 1
1. Rebuild + start. Logs should show no SendGrid references.
2. Settings → Send test email → confirm it arrives **from hr@amnex.com**.
3. Trigger a NexAI invite → candidate email arrives; logs show `[email] SMTP sent TO: ...`.
4. Log in works (JWT signing still fine with the env secret).
# Step 1b (OPTIONAL) — Drop the Google Meet transcript feature entirely

Do this ONLY if you chose option (A) fully-Google-free. Skip if you chose (B).

The bot interview's own transcript+score email to the recruiter is **separate** and is NOT removed by this step — that keeps working.

## Backend — `backend/app/main.py`

### FIND:
```python
from .routers.google_oauth import router as _google_oauth_router
```
DELETE that line.

### FIND:
```python
from .routers.transcript_api import router as _transcript_router
```
DELETE that line.

### FIND:
```python
app.include_router(_google_oauth_router)
```
DELETE that line.

### FIND:
```python
app.include_router(_transcript_router)
```
DELETE that line.

> The `interview_notes` migration in the startup block can stay (idempotent, harmless) or be removed — your call. Leaving it is safer.

## Delete these files
- `backend/app/routers/google_oauth.py`
- `backend/app/services/transcript_service.py`
- `backend/app/routers/transcript_api.py`
- `backend/app/services/notetaker.py` (already a deprecated stub)

## Frontend — `frontend/index.html`
Remove the Google/Drive UI so nothing 404s:
- The `#gcal-indicator` element (~line 399) and its CSS (~lines 36–38, 228).
- The "Google Calendar & Drive" card in the Interviews screen (~lines 2849–2858).
- The Drive banner (~lines 2782–2789).
- Functions: `refreshGCal` (~1237), `connectGCal` (~2865), `disconnectGCal` (~2866), `fetchTranscript` (~2870), the transcript modal renderer (~2900–2970), and the `recruiter_google_token`/`/api/google/...` fetches.
- The query-param handlers at ~8606–8607 (`connected=1`, `gcal_error`).
- The `refreshGCal().catch(()=>{})` call at ~9224.
- "Transcript" column header + cell in the interviews table (~2861).

> Easiest method for Claude Code: search the file for `gcal`, `Google`, `transcript`, `/api/google`, `connectGCal`, `fetchTranscript` and remove each matched block, then load the page and fix any JS console errors.

## requirements.txt
Remove (only if dropping Google):
```
google-auth==2.29.0
google-auth-oauthlib==1.2.0
google-api-python-client==2.127.0
google-cloud-storage>=2.16.0
```
KEEP `gtts` — it is a TTS fallback, not Google auth.

> If you keep the GCS-based avatar prerender (`prerender.py` / `avatar.py` import `google.cloud.storage`), keep `google-cloud-storage`. Check whether you use the GPU avatar path before removing it.

## VERIFY
App starts with no `ImportError`. Interviews screen renders with no Google card and no console errors.
# Step 2 — Calendar invites via ICS over SMTP (no Google)

Goal: scheduling an interview emails the candidate + each panel member, from hr@amnex.com, a real `.ics` invite they can Accept/Decline. The meeting link is a plain URL the recruiter pastes (or blank). No OAuth, no Google API.

## 2A. Add ICS helpers to `backend/app/services/connectors.py`

Add these two functions near the bottom of the EMAIL section (after `send_email`, before the AI bot stub):

```python
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
) -> str:
    """
    Build a minimal, valid VCALENDAR/VEVENT string (METHOD:REQUEST).
    start_dt_utc must be a UTC datetime (naive treated as UTC).
    """
    end_dt = start_dt_utc + timedelta(minutes=duration_min)
    fmt = "%Y%m%dT%H%M%SZ"
    stamp = datetime.utcnow().strftime(fmt)
    ev_uid = uid or f"{uuid.uuid4().hex}@enternly"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//EnternsTech//Enternly//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{ev_uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{start_dt_utc.strftime(fmt)}",
        f"DTEND:{end_dt.strftime(fmt)}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(location)}",
        f"ORGANIZER;CN=EnternsTech Talent Acquisition:mailto:{organizer_email}",
        "STATUS:CONFIRMED",
        "SEQUENCE:0",
        "TRANSP:OPAQUE",
    ]
    for em in attendee_emails:
        if em:
            lines.append(
                f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{em}"
            )
    lines += [
        "BEGIN:VALARM",
        "TRIGGER:-PT30M",
        "ACTION:DISPLAY",
        "DESCRIPTION:Interview reminder",
        "END:VALARM",
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
) -> dict:
    """
    Send a calendar invite (.ics attached) from hr@amnex.com to each recipient.
    One email per recipient so the candidate never sees the panel list.
    Falls back to a plain email if SMTP isn't configured (logged, never raises here).
    """
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders as _enc

    cfg = _load_email_cfg()
    if not (cfg["user"] and cfg["password"]):
        print(f"[calendar] SMTP not configured — invite NOT sent to {to_emails}")
        return {"sent": False, "stub": True, "to": to_emails}

    organizer = cfg["user"]  # hr@amnex.com
    shared_uid = f"{uuid.uuid4().hex}@enternly"
    ics_text = build_ics(
        summary=subject,
        description=body_text,
        start_dt_utc=start_dt_utc,
        duration_min=duration_min,
        organizer_email=organizer,
        attendee_emails=to_emails,
        location=location,
        uid=shared_uid,
    )

    sent_ok = []
    for to_email in to_emails:
        if not to_email:
            continue
        # multipart/mixed: text body + calendar part + .ics attachment (Outlook-friendly)
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = f"{cfg['from_name']} <{organizer}>"
        msg["To"]      = to_email
        if reply_to:
            msg["Reply-To"] = reply_to

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain", "utf-8"))
        cal_part = MIMEText(ics_text, "calendar", "utf-8")
        cal_part.add_header("Content-Type", 'text/calendar; method=REQUEST; charset="utf-8"')
        alt.attach(cal_part)
        msg.attach(alt)

        ics_attach = MIMEBase("application", "ics")
        ics_attach.set_payload(ics_text.encode("utf-8"))
        _enc.encode_base64(ics_attach)
        ics_attach.add_header("Content-Disposition", 'attachment; filename="invite.ics"')
        msg.attach(ics_attach)

        try:
            _send_smtp(cfg, to_email, msg)
            print(f"[calendar] invite sent TO: {to_email}")
            sent_ok.append(to_email)
        except Exception as exc:
            print(f"[calendar] invite FAILED to {to_email}: {exc}")

    return {"sent": bool(sent_ok), "to": sent_ok, "via": "smtp_ics"}
```

## 2B. Rewrite `schedule_meeting` in `connectors.py` (drop Google)

### FIND the entire `def schedule_meeting(...)` function (the Google Calendar one) and REPLACE WITH:
```python
def schedule_meeting(organizer_email: str, candidate_email: str,
                     panel_emails: list, start_time: datetime,
                     duration_min: int = 45, meet_link: str = "",
                     candidate_name: str = "Candidate",
                     job_title: str = "the role") -> dict:
    """
    Create a calendar invite (.ics over SMTP) from hr@amnex.com and email it
    to the candidate + each panel member. No Google API.

    meet_link: optional video URL (Jitsi/Teams/Zoom/Meet) pasted by the recruiter.
    Returns the same shape callers already expect (gcal_event_id is None now).
    """
    when = start_time.strftime("%A, %d %B %Y at %I:%M %p UTC")
    location = meet_link or "To be confirmed"
    body = (
        f"You are invited to an interview for {job_title}.\n\n"
        f"When: {when}\n"
        f"Duration: {duration_min} minutes\n"
        f"Join link: {meet_link or 'will be shared separately'}\n\n"
        f"Please accept this invite to confirm.\n\n"
        f"— EnternsTech Talent Acquisition"
    )
    all_emails = list({candidate_email} | set(panel_emails or []))
    send_calendar_invite(
        to_emails=all_emails,
        subject=f"Interview – {candidate_name} – {job_title}",
        body_text=body,
        start_dt_utc=start_time,
        duration_min=duration_min,
        location=location,
        reply_to=organizer_email,
    )
    return {
        "gcal_event_id": None,
        "meet_link":     meet_link,
        "scheduled_at":  start_time.isoformat(),
        "conflicts":     [],
    }
```

> You can also delete `_get_calendar_service` (the Google helper) — nothing else uses it once `schedule_meeting` is rewritten (verify with a grep).

## 2C. Update `/api/schedule` route in `backend/app/main.py`

### FIND the `ScheduleIn` model:
```python
class ScheduleIn(BaseModel):
    application_id: str
    recruiter_id: str
    panel_emails: list[str] = []
    start_in_hours: int = 24
    duration_min: int = 45
```
### REPLACE WITH:
```python
class ScheduleIn(BaseModel):
    application_id: str
    panel_emails: list[str] = []
    start_in_hours: int = 24
    duration_min: int = 45
    meet_link: str = ""
```

### FIND inside `def schedule(...)` the meeting call:
```python
    start = datetime.utcnow() + timedelta(hours=payload.start_in_hours)
    try:
        meeting = connectors.schedule_meeting(
            payload.recruiter_id, app_row["email"], payload.panel_emails,
            start, payload.duration_min,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
```
### REPLACE WITH:
```python
    start = datetime.utcnow() + timedelta(hours=payload.start_in_hours)
    organizer_email = request.state.user.get("email") or ""
    meeting = connectors.schedule_meeting(
        organizer_email=organizer_email,
        candidate_email=app_row["email"],
        panel_emails=payload.panel_emails,
        start_time=start,
        duration_min=payload.duration_min,
        meet_link=payload.meet_link,
        candidate_name=app_row.get("full_name") or "Candidate",
        job_title=app_row.get("job_title") or "the role",
    )
```

> The `interview` INSERT below already stores `meeting["meet_link"]` and `meeting["gcal_event_id"]` (now None) — leave it. The follow-up `interview_scheduled` email block can stay; it's now redundant with the ICS body but harmless. Optional: delete it to avoid two emails.

## 2D. (Optional) Add a "Schedule Interview" button in the frontend
`/api/schedule` currently has NO caller. To actually use it, add a small modal on the pipeline/interviews screen that collects: panel emails (comma-sep), start-in-hours (or a datetime), duration, and an optional **meeting link** text box, then `POST /api/schedule`. If you'd rather wire this later, the endpoint is callable now via API and the dev team can build UI post-launch.

## VERIFY Step 2
1. Call `POST /api/schedule` (Postman or the new button) with a real candidate email + a meet_link.
2. Confirm an email from hr@amnex.com arrives with an **invite.ics** that adds to Google/Outlook/Apple calendar and shows Accept/Decline.
3. Logs show `[calendar] invite sent TO: ...`.
# Step 3 — OpenAI for the bot brain + Whisper for candidate speech

## 3A. Point the bot brain at OpenAI — ENV ONLY, no code change

`backend/app/services/interviewer_llm.py` already uses the **OpenAI Python SDK**, just pointed at Groq via `GROQ_BASE_URL`. To use OpenAI instead, set in `.env.prod`:

```
# Use OpenAI for NexAI's conversation + scoring
GROQ_API_KEY=sk-YOUR_OPENAI_KEY          # the client reads this var name
GROQ_BASE_URL=https://api.openai.com/v1   # OpenAI endpoint
LLM_MODEL=gpt-4o-mini                      # or gpt-4o / gpt-4.1-mini etc.
```

That's it — the SDK calls `chat.completions.create` which is identical on OpenAI.

### OPTIONAL cleanup (so the var names aren't confusing)
If you'd rather use `OPENAI_API_KEY`, make these two edits in `interviewer_llm.py`:

#### FIND:
```python
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Add it to the backend .env file."
            )
        base_url = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
```
#### REPLACE WITH:
```python
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to .env.prod."
            )
        base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get(
            "GROQ_BASE_URL", "https://api.openai.com/v1"
        )
```
Then in `.env.prod`:
```
OPENAI_API_KEY=sk-YOUR_OPENAI_KEY
OPENAI_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

> Also check `requirements.txt` already has `openai>=1.40.0` — it does. No new dependency.

## 3B. Add Whisper transcription (server-side candidate speech)

Today the candidate's spoken answers are transcribed **in the browser** by the free Web `SpeechRecognition` API (Chrome-only, variable quality). Whisper moves transcription to the server for better accuracy and cross-browser support.

### New service file: `backend/app/services/stt.py`
```python
"""
Speech-to-text via OpenAI Whisper API.
Used by the conversational NexAI interview to transcribe candidate audio.
Env: OPENAI_API_KEY (reuses the same key as the LLM brain).
"""
import os
import tempfile
from typing import Optional

import openai

_client: Optional[openai.OpenAI] = None


def _get_client() -> openai.OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set — required for Whisper STT.")
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        _client = openai.OpenAI(api_key=api_key, base_url=base_url)
    return _client


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.webm",
                     model: str = None) -> str:
    """
    Transcribe a single audio blob with Whisper. Returns plain text.
    model defaults to env WHISPER_MODEL or 'whisper-1'.
    """
    model = model or os.environ.get("WHISPER_MODEL", "whisper-1")
    suffix = "." + filename.rsplit(".", 1)[-1] if "." in filename else ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tf:
        tf.write(audio_bytes)
        tf.flush()
        tf.seek(0)
        with open(tf.name, "rb") as fh:
            resp = _get_client().audio.transcriptions.create(
                model=model,
                file=fh,
                response_format="text",
            )
    # SDK returns a str when response_format="text"
    return (resp if isinstance(resp, str) else getattr(resp, "text", "")).strip()
```

### New endpoint in `backend/app/routers/nexai_api.py`
Add near the other public invite endpoints (public — candidate has no JWT). It accepts an audio file and returns text; the frontend then passes that text into the existing `/invite/converse` turn.

```python
@router.post("/invite/transcribe")
async def transcribe_candidate_audio(file: UploadFile = File(...)):
    """
    Public — transcribe one candidate audio blob via Whisper.
    Returns {"text": "..."}. The frontend sends this text to /invite/converse.
    """
    from ..services.stt import transcribe_audio
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "Empty audio")
    try:
        text = transcribe_audio(audio, file.filename or "audio.webm")
    except Exception as exc:
        print(f"[stt] transcription failed: {exc}")
        raise HTTPException(502, "Transcription failed")
    return {"text": text}
```
At the top of `nexai_api.py` ensure these imports exist:
```python
from fastapi import UploadFile, File
```
(add `UploadFile, File` to the existing fastapi import line).

### Make the endpoint public — `backend/app/main.py`
The auth middleware already lets through anything starting with `/api/nexai/invite`. Confirm this line is present (it is):
```python
    if path.startswith("/api/nexai/invite") or path == "/nexai-interview":
```
`/api/nexai/invite/transcribe` matches that prefix → already public. No change needed.

### Frontend — `frontend/interview.html` (conversational mode)
Today `_recog` (SpeechRecognition) fills `_listenText`, and on silence the code sends `_listenText` to `/invite/converse`. To use Whisper instead, record the mic with `MediaRecorder`, and on turn-end POST the blob to `/invite/transcribe`, then send the returned text to `/invite/converse`.

Minimal approach (let Claude Code implement against the existing `_convMicStream`):
1. When a candidate turn starts, create `const rec = new MediaRecorder(_convMicStream, {mimeType:'audio/webm'})`, collect chunks.
2. On silence/turn-end, `rec.stop()`, build `new Blob(chunks,{type:'audio/webm'})`.
3. `POST` it as `FormData` (`file`) to `/api/nexai/invite/transcribe?...`, read `{text}`.
4. Pass `text` as `candidate_text` to the existing `/invite/converse` call (replacing `_listenText`).
5. Keep the SpeechRecognition path as a fallback if `MediaRecorder` or the network call fails, so the interview never dead-ends.

> Recommendation: **ship Whisper as an enhancement with the browser STT kept as fallback.** That way a Whisper/API hiccup never blocks a live candidate.

### requirements.txt
No change — `openai>=1.40.0` covers Whisper too.

## 3C. (Optional) Reuse Whisper for panel-interview transcripts
This is the realistic answer to "transcript of the panel interview without Google": add a recruiter-facing "Upload interview recording" button that POSTs an audio/video file to a new authenticated endpoint, runs `transcribe_audio`, stores it in `interview_notes.transcript_text`, optionally summarizes with the LLM, and emails the recruiter. This avoids Google entirely but requires someone to record + upload the call. Scope it as a follow-up if needed.

## VERIFY Step 3
1. Set OpenAI env vars, restart. Run a NexAI conversational interview → bot replies are coherent (now from OpenAI).
2. If Whisper wired: speak an answer → network tab shows `/invite/transcribe` returning text → conversation continues.
3. Completion email to the recruiter still arrives with transcript + score.
# Step 4 — Self-service password: create (first-time) + forgot/reset, emailed from hr@amnex.com

Flow:
- Username = the user's official email (e.g. `recruiter@amnex.com`).
- When an admin creates a recruiter/TA-manager (or for an existing user), the system can send a "Set your password" email from hr@amnex.com containing a one-time link.
- "Forgot password" on the login page emails a reset link from hr@amnex.com.
- The link opens a Set-Password page; submitting it hashes + stores the password and consumes the token.

Tokens are single-use, time-limited, stored hashed in the DB.

## 4A. Migration — add a password-reset token table

Add to the `migrations` list in `backend/app/main.py` `_auto_migrate()` (append at the end, before the closing `]`):

```python
        # ── Password reset / first-time set-password tokens ─────────────
        """CREATE TABLE IF NOT EXISTS password_reset_token (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            token_hash  TEXT NOT NULL UNIQUE,
            purpose     TEXT NOT NULL DEFAULT 'reset'
                        CHECK (purpose IN ('reset','invite')),
            expires_at  TIMESTAMPTZ NOT NULL,
            used_at     TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_prt_user ON password_reset_token(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_prt_hash ON password_reset_token(token_hash)",
```

## 4B. New router file: `backend/app/routers/password_api.py`

```python
"""
Self-service password flows — first-time set + forgot/reset.
All emails sent from hr@amnex.com (SMTP). Tokens are single-use & expiring.
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import hash_password, require_admin
from ..services.connectors import send_email, _load_email_cfg

router = APIRouter(prefix="/api/auth", tags=["password"])

_TOKEN_TTL_HOURS = 24
# Only these roles may use self-service password set/reset
_SELF_SERVICE_ROLES = {"admin", "ta_manager", "recruiter"}


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _base_url() -> str:
    cfg = _load_email_cfg()
    return (cfg.get("base_url") or os.environ.get("APP_BASE_URL", "")).rstrip("/")


def _issue_token(user_id: str, purpose: str) -> str:
    """Create a single-use token, store its hash, return the raw token."""
    raw = secrets.token_urlsafe(32)
    query(
        """INSERT INTO password_reset_token (user_id, token_hash, purpose, expires_at)
           VALUES (%s, %s, %s, %s)""",
        [user_id, _hash_token(raw), purpose,
         datetime.utcnow() + timedelta(hours=_TOKEN_TTL_HOURS)],
        fetch=False,
    )
    return raw


def _send_link_email(to_email: str, full_name: str, raw_token: str, purpose: str):
    link = f"{_base_url()}/set-password?token={raw_token}"
    if purpose == "invite":
        subject = "Set your Enternly password"
        intro = (
            f"Hi {full_name or ''},\n\n"
            "An account has been created for you on Enternly (EnternsTech Talent Acquisition).\n"
            f"Your username is your email: {to_email}\n\n"
            "Set your password using the secure link below:"
        )
    else:
        subject = "Reset your Enternly password"
        intro = (
            f"Hi {full_name or ''},\n\n"
            "We received a request to reset your Enternly password.\n"
            "If you didn't request this, you can ignore this email.\n\n"
            "Reset your password using the secure link below:"
        )
    body = (
        f"{intro}\n\n{link}\n\n"
        f"This link expires in {_TOKEN_TTL_HOURS} hours and can be used once.\n\n"
        "— EnternsTech Talent Acquisition"
    )
    send_email(to_email, subject, body)


# ── Admin: send a set-password invite to a user ───────────────────────────────

class InviteIn(BaseModel):
    email: str


@router.post("/send-setup-link")
def send_setup_link(body: InviteIn, admin=Depends(require_admin)):
    """Admin triggers a first-time 'set your password' email to a user."""
    user = query_one(
        "SELECT id, full_name, email, role, is_active FROM app_user WHERE email=%s",
        [body.email.lower().strip()],
    )
    if not user or not user["is_active"]:
        raise HTTPException(404, "Active user with that email not found")
    if user["role"] not in _SELF_SERVICE_ROLES:
        raise HTTPException(400, "Self-service password is only for admin / TA manager / recruiter")
    raw = _issue_token(str(user["id"]), "invite")
    _send_link_email(user["email"], user["full_name"], raw, "invite")
    return {"ok": True, "sent_to": user["email"]}


# ── Public: forgot password ───────────────────────────────────────────────────

class ForgotIn(BaseModel):
    email: str


@router.post("/forgot-password")
def forgot_password(body: ForgotIn):
    """
    Public. Always returns ok (don't reveal whether an email exists).
    Sends a reset link only if the email maps to an eligible active user.
    """
    user = query_one(
        "SELECT id, full_name, email, role, is_active FROM app_user WHERE email=%s",
        [body.email.lower().strip()],
    )
    if user and user["is_active"] and user["role"] in _SELF_SERVICE_ROLES:
        raw = _issue_token(str(user["id"]), "reset")
        try:
            _send_link_email(user["email"], user["full_name"], raw, "reset")
        except Exception as exc:
            print(f"[password] reset email failed for {user['email']}: {exc}")
    return {"ok": True, "message": "If that account exists, a reset link has been sent."}


# ── Public: validate token (for the set-password page) ────────────────────────

@router.get("/reset-token/validate")
def validate_token(token: str):
    row = query_one(
        "SELECT user_id, expires_at, used_at FROM password_reset_token WHERE token_hash=%s",
        [_hash_token(token)],
    )
    if not row or row["used_at"] is not None:
        return {"valid": False}
    if row["expires_at"].replace(tzinfo=None) < datetime.utcnow():
        return {"valid": False}
    return {"valid": True}


# ── Public: submit new password ───────────────────────────────────────────────

class ResetSubmitIn(BaseModel):
    token: str
    new_password: str


@router.post("/reset-password")
def reset_password(body: ResetSubmitIn):
    if len(body.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    row = query_one(
        """SELECT id, user_id, expires_at, used_at
           FROM password_reset_token WHERE token_hash=%s""",
        [_hash_token(body.token)],
    )
    if not row or row["used_at"] is not None:
        raise HTTPException(400, "Invalid or already-used link")
    if row["expires_at"].replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(400, "This link has expired — request a new one")
    query(
        "UPDATE app_user SET password_hash=%s WHERE id=%s",
        [hash_password(body.new_password), row["user_id"]],
        fetch=False,
    )
    query(
        "UPDATE password_reset_token SET used_at=now() WHERE id=%s",
        [row["id"]], fetch=False,
    )
    # Invalidate any other outstanding tokens for this user
    query(
        "UPDATE password_reset_token SET used_at=now() WHERE user_id=%s AND used_at IS NULL",
        [row["user_id"]], fetch=False,
    )
    return {"ok": True}
```

## 4C. Register the router + make endpoints public — `backend/app/main.py`

### Add import near the other router imports:
```python
from .routers.password_api import router as _password_router
```
### Add include near the other includes:
```python
app.include_router(_password_router)
```

### Make the public ones bypass JWT. FIND the `_PUBLIC` set and add:
```python
_PUBLIC = {
    "/", "/login", "/api/health", "/api/auth/login",
    "/set-password",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/reset-token/validate",
    "/nexai-interview",
    "/api/nexai/invite/validate",
    "/api/nexai/invite/begin",
}
```
(`/api/auth/send-setup-link` stays admin-protected — do NOT add it.)

### Serve the set-password page. In the `if os.path.isdir(_FRONTEND_DIR):` block, add:
```python
    @app.get("/set-password", response_class=HTMLResponse)
    def set_password_page():
        with open(os.path.join(_FRONTEND_DIR, "set-password.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)
```

## 4D. Auto-invite on user creation (optional but matches your ask)
In `backend/app/routers/admin_users.py`, the `create_user` endpoint currently requires a password. To support "create user → they get an email to set their own password," either:

**Option 1 (recommended):** make password optional and auto-send the invite.
### FIND:
```python
class CreateUserIn(BaseModel):
    full_name: str
    email: str
    role: str
    password: str
```
### REPLACE WITH:
```python
class CreateUserIn(BaseModel):
    full_name: str
    email: str
    role: str
    password: Optional[str] = None       # if omitted, user sets it via emailed link
    send_setup_email: bool = True
```
### FIND the body of `create_user` after the duplicate-email check and REPLACE the INSERT + return with:
```python
    pwd_hash = hash_password(body.password) if body.password else None
    row = query_one(
        f"""INSERT INTO app_user (full_name, email, role, password_hash)
            VALUES (%s, %s, %s, %s) RETURNING {_USER_COLS}""",
        [body.full_name, body.email.lower(), body.role, pwd_hash],
    )
    # Auto-send first-time set-password email for self-service roles
    if body.send_setup_email and body.role in {"admin", "ta_manager", "recruiter"}:
        try:
            from .password_api import _issue_token, _send_link_email
            raw = _issue_token(str(row["id"]), "invite")
            _send_link_email(row["email"], row["full_name"], raw, "invite")
        except Exception as exc:
            print(f"[create_user] setup email failed: {exc}")
    return row
```
Also remove the hard `len(body.password) < 6` check, or guard it with `if body.password and len(body.password) < 6:`.

## 4E. Frontend — set-password page + "Forgot password?" link

### New file: `frontend/set-password.html`
A minimal standalone page (match login.html's style). It reads `?token=` from the URL, calls `GET /api/auth/reset-token/validate`, and if valid shows two password fields → `POST /api/auth/reset-password` → on success redirect to `/login`. Keep it dependency-free (vanilla JS, same look as login.html). Hand login.html to Claude Code as the style reference.

### `frontend/login.html` — add a "Forgot password?" link
Below the login button, add a link that prompts for email and calls `POST /api/auth/forgot-password`, then shows "If that account exists, a reset link has been sent." (Always show that message regardless of response, to avoid leaking which emails exist.)

### Settings / Users screen — add a "Send setup link" button (optional)
Next to each user, an admin button calling `POST /api/auth/send-setup-link {email}`.

## VERIFY Step 4
1. Admin creates a recruiter without a password → recruiter receives "Set your Enternly password" email **from hr@amnex.com** with a link.
2. Open the link → set-password page validates the token → set password → redirected to login → log in works.
3. On login page, "Forgot password?" → enter email → reset email arrives → reset works → old token no longer usable.
4. Reusing a consumed link shows "Invalid or already-used link."
# `.env.prod` — keys these changes need

You already have `.env.prod`. Add / confirm these keys.

## SMTP (Step 1 + 2 + 4 — all email from hr@amnex.com)
```
SMTP_USER=hr@amnex.com
SMTP_PASSWORD=your_16_char_app_password      # spaces are stripped automatically
SMTP_HOST=smtp.gmail.com                      # or smtp.office365.com if not Workspace
SMTP_PORT=587
SMTP_FROM_NAME=EnternsTech Talent Acquisition
SENDGRID_API_KEY=                             # leave blank — SMTP-only now
APP_BASE_URL=https://your-prod-domain         # used in password + invite links — MUST be the real public URL
```

## Security (Step 1)
```
ENV=prod
JWT_SECRET=long_random_string                 # python -c "import secrets;print(secrets.token_urlsafe(48))"
```

## OpenAI bot brain (Step 3A)
Either reuse the existing GROQ_* var names:
```
GROQ_API_KEY=sk-your-openai-key
GROQ_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```
Or, if you applied the optional rename in Step 3A:
```
OPENAI_API_KEY=sk-your-openai-key
OPENAI_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

## Whisper STT (Step 3B)
```
OPENAI_API_KEY=sk-your-openai-key             # same key; required for /invite/transcribe
WHISPER_MODEL=whisper-1
```

## Notes
- `APP_BASE_URL` is critical for Steps 2 & 4 — password/invite links and any absolute URLs are built from it. If it's `http://localhost:8000`, emailed links won't work for real users.
- If `hr@amnex.com` is Office 365, set `SMTP_HOST=smtp.office365.com`, `SMTP_PORT=587`. App-password mechanics are the same.
- The app reads settings from the `system_settings` DB table FIRST, then env. If old SMTP/SendGrid values are saved in Settings, clear or overwrite them in the Settings screen so env/`hr@amnex.com` wins.
