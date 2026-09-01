"""
Gmail CV ingest — polls recruiter mailboxes for attachments named
*_Resume.(pdf|docx|doc) and feeds them into the CV repository pipeline.

──────────────────────────────────────────────────────────────────────────────
ACTIVATION INSTRUCTIONS (for the dev/ops team)
──────────────────────────────────────────────────────────────────────────────
1. Create a Google Cloud project and enable the Gmail API.
2. Create OAuth 2.0 Desktop credentials and download credentials.json.
3. Set the env var in .env.prod:
       GOOGLE_OAUTH_CREDENTIALS=/app/secrets/gmail_credentials.json
4. On first run, complete the OAuth flow; a token file will be saved beside
   the credentials file.  Subsequent restarts reuse the saved token.
5. The recruiter email addresses to poll go in system_settings table:
       key='email_ingest_accounts', value='user1@company.com,user2@company.com'
6. Restart the backend container — the poller activates automatically.

Until GOOGLE_OAUTH_CREDENTIALS is set, this module logs a single idle message
per startup and does nothing.
──────────────────────────────────────────────────────────────────────────────
"""
import asyncio
import os
import re

_CREDS_ENV   = "GOOGLE_OAUTH_CREDENTIALS"
_POLL_EVERY  = 300  # seconds between polls (5 min)
_RESUME_RE   = re.compile(r'.*_resume\.(pdf|docx|doc)$', re.IGNORECASE)
_SCOPES      = ["https://www.googleapis.com/auth/gmail.readonly"]


def _get_oauth_service(creds_path: str):
    """Build and return an authorised Gmail API service object."""
    import pickle
    from pathlib import Path
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = Path(creds_path).parent / "gmail_token.pickle"
    creds = None

    if token_path.exists():
        with open(token_path, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # run_local_server() opens a real browser and blocks waiting for
            # a human to click through consent -- inside this background
            # asyncio task on a headless server that human never arrives, so
            # it used to hang the poller forever with no error, no log, no
            # timeout. This is a server process, not an interactive CLI: the
            # token file must be generated once, out-of-band, by an operator
            # running the OAuth flow on their own machine and copying the
            # resulting gmail_token.pickle next to the credentials file.
            raise RuntimeError(
                f"No valid/refreshable Gmail OAuth token at {token_path}. "
                "The interactive consent flow cannot run inside this server process. "
                "Generate gmail_token.pickle offline (run the OAuth flow on a machine "
                "with a browser) and place it next to the credentials file, then restart."
            )
        # Left as a raw local-disk write (not routed through StorageBackend):
        # this is an OAuth token cache, not persistent user content. It's
        # written next to GOOGLE_OAUTH_CREDENTIALS, which is itself only ever
        # a local file path an operator places on the server (see this
        # module's docstring) -- StorageBackend has no equivalent for "the
        # credentials file itself", so moving only the token half to S3
        # wouldn't actually fix this feature's ephemeral-hosting fragility;
        # both would still need to survive a redeploy together. That's a
        # secrets-management problem, not a user-file-storage one -- out of
        # scope for this migration.
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    return build("gmail", "v1", credentials=creds)


def _mark_messages_read(service, user_email: str, message_ids: list[str]) -> None:
    """Strip the UNREAD label so a poll cycle doesn't keep re-fetching and
    re-parsing the same messages forever (dedup by file_hash already stops
    duplicate DB rows, but this was wasting API calls every 5 minutes and
    never gave visible feedback in the mailbox)."""
    if not message_ids:
        return
    service.users().messages().batchModify(
        userId=user_email,
        body={"ids": message_ids, "removeLabelIds": ["UNREAD"]},
    ).execute()


def _fetch_resume_attachments(service, user_email: str) -> list[dict]:
    """
    Search the inbox for unread mails with attachments matching the resume
    naming pattern. Returns list of {filename, data, message_id}.
    """
    results = service.users().messages().list(
        userId=user_email,
        q="has:attachment is:unread",
        maxResults=50,
    ).execute()

    attachments = []
    for msg_meta in results.get("messages", []):
        msg = service.users().messages().get(
            userId=user_email, id=msg_meta["id"], format="full"
        ).execute()
        parts = msg.get("payload", {}).get("parts", [])
        for part in parts:
            fname = part.get("filename") or ""
            if not _RESUME_RE.match(fname):
                continue
            body = part.get("body", {})
            att_id = body.get("attachmentId")
            if not att_id:
                continue
            att = service.users().messages().attachments().get(
                userId=user_email, messageId=msg_meta["id"], id=att_id
            ).execute()
            import base64
            data = base64.urlsafe_b64decode(att["data"] + "==")
            attachments.append({
                "filename":   fname,
                "data":       data,
                "message_id": msg_meta["id"],
            })
    return attachments


def _set_ingest_status(status: str, detail: str = "") -> None:
    """Persists poller status to system_settings so it's visible in the admin
    Settings screen, not just a boot-time log line nobody re-checks later."""
    try:
        from ..db import query
        query(
            """INSERT INTO system_status (key, value) VALUES ('email_ingest_status', %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            [f"{status}|{detail}"], fetch=False,
        )
    except Exception as exc:
        print(f"[email-ingest] could not persist status: {exc}")


async def _run_one_poll_cycle(creds_path: str, query, query_one, cv_parser) -> None:
    """One poll cycle across every configured mailbox. Raises on a cycle-
    level failure (e.g. can't build the Gmail service) -- per-account and
    per-attachment failures are caught internally and just logged, same as
    before. Extracted out of start_email_poller's while-loop body so both
    that loop (Redis-less fallback mode) and run_one_pass() (the Arq queued
    job, worker.py, Redis mode) call the exact same logic."""
    service = await asyncio.to_thread(_get_oauth_service, creds_path)

    # Read accounts from system_settings -- this key is tenant-scoped
    # (Migration 96), so every tenant that has configured its own
    # ingest mailboxes gets its rows polled, each stamped with ITS
    # OWN tenant_id below (never a single global reader picking one
    # tenant's row arbitrarily).
    settings_rows = await asyncio.to_thread(
        query,
        "SELECT tenant_id, value FROM system_settings WHERE key='email_ingest_accounts'",
        [],
    )
    acct_tenant_map = {}
    for row in (settings_rows or []):
        for a in (row.get("value") or "").split(","):
            a = a.strip()
            if a:
                acct_tenant_map[a] = row.get("tenant_id")

    for acct, tenant_id in acct_tenant_map.items():
        try:
            attachments = await asyncio.to_thread(
                _fetch_resume_attachments, service, acct
            )
        except Exception as exc:
            print(f"[email-ingest] error fetching attachments for {acct}: {exc}")
            continue

        # Ingest and mark-read PER MESSAGE, not as one all-or-nothing
        # batch -- previously one bad attachment raised before
        # _mark_messages_read ran at all, so the whole cycle's
        # messages (including already-ingested ones) stayed unread
        # and got re-fetched/re-failed on every poll forever.
        ok_message_ids = set()
        failed_message_ids = set()
        for att in attachments:
            mid = att["message_id"]
            if mid in failed_message_ids:
                continue  # a sibling attachment on this message already failed
            try:
                await _ingest_email_attachment(
                    att["data"], att["filename"], query, query_one, cv_parser, tenant_id
                )
                ok_message_ids.add(mid)
            except Exception as exc:
                failed_message_ids.add(mid)
                ok_message_ids.discard(mid)
                print(f"[email-ingest] failed to ingest {att['filename']!r} "
                      f"(msg {mid}) from {acct}: {exc}")

        message_ids = list(ok_message_ids)
        if message_ids:
            try:
                await asyncio.to_thread(_mark_messages_read, service, acct, message_ids)
            except Exception as exc:
                print(f"[email-ingest] failed to mark messages read for {acct}: {exc}")
        if failed_message_ids:
            print(f"[email-ingest] {len(failed_message_ids)} message(s) left unread "
                  f"for {acct} after ingest failure — will retry next poll: {failed_message_ids}")


async def run_one_pass() -> str:
    """
    One full poll cycle, or a no-op if not configured. Returns
    "idle_no_credentials" | "ok" | "error". Public entrypoint for the Arq
    queued job (worker.py) -- idempotency here is via Gmail's UNREAD-label
    removal plus cv_repository's file_hash uniqueness (an overlapping
    concurrent pass can at worst re-fetch/re-parse the same still-unread
    message once; the file_hash UNIQUE constraint rejects the duplicate
    INSERT rather than corrupting state). worker.py additionally wraps this
    in a job_lock so overlapping ticks skip instead of doing that
    concurrent work at all.
    """
    creds_path = os.environ.get(_CREDS_ENV)
    if not creds_path or not os.path.exists(creds_path):
        await asyncio.to_thread(_set_ingest_status, "idle_no_credentials", _CREDS_ENV)
        return "idle_no_credentials"

    try:
        from ..db import query, query_one
        from . import cv_parser
    except ImportError as exc:
        print(f"[email-ingest] import error: {exc}")
        return "error"

    try:
        await _run_one_poll_cycle(creds_path, query, query_one, cv_parser)
        await asyncio.to_thread(_set_ingest_status, "running")
        return "ok"
    except Exception as exc:
        print(f"[email-ingest] poll cycle error: {exc}")
        await asyncio.to_thread(_set_ingest_status, "poll_error", str(exc)[:500])
        return "error"


async def start_email_poller():
    """
    Background asyncio task — polls Gmail for CV attachments.
    Idle when GOOGLE_OAUTH_CREDENTIALS is not configured -- status is
    persisted to system_settings (key='email_ingest_status') so it shows up
    as a standing admin-settings warning instead of a one-time boot log line.
    """
    creds_path = os.environ.get(_CREDS_ENV)
    if not creds_path or not os.path.exists(creds_path):
        print(
            "[email-ingest] idle — awaiting Google OAuth credentials. "
            f"Set env var {_CREDS_ENV} to activate Gmail CV polling."
        )
        await asyncio.to_thread(_set_ingest_status, "idle_no_credentials", _CREDS_ENV)
        return  # Do not enter the loop — no credentials available

    print(f"[email-ingest] Gmail poller starting (credentials: {creds_path})")
    while True:
        try:
            await run_one_pass()
        except asyncio.CancelledError:
            print("[email-ingest] task cancelled, shutting down")
            return
        except Exception as exc:
            # run_one_pass() already catches its own cycle errors -- this is
            # only a backstop for something unexpected escaping it.
            print(f"[email-ingest] unexpected error: {exc}")

        await asyncio.sleep(_POLL_EVERY)


async def _ingest_email_attachment(data: bytes, filename: str, query, query_one, cv_parser, tenant_id=None):
    """Ingest one attachment into cv_repository with source='email'."""
    import uuid as _uuid
    import os

    file_hash = cv_parser.sha256_hash(data)
    if tenant_id:
        existing = await asyncio.to_thread(
            query_one,
            "SELECT id FROM cv_repository WHERE file_hash=%s AND tenant_id=%s",
            [file_hash, tenant_id],
        )
    else:
        existing = await asyncio.to_thread(
            query_one,
            "SELECT id FROM cv_repository WHERE file_hash=%s",
            [file_hash],
        )
    if existing:
        return  # duplicate

    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    raw_text = cv_parser.extract_text(data, ext)
    skills   = cv_parser.extract_tier1_skills(raw_text)
    name     = cv_parser.parse_candidate_name(filename)

    cv_id = str(_uuid.uuid4())
    from .storage import get_storage
    dest = get_storage(
        "cv", local_env_var="CV_STORE_DIR", local_default="/app/cv_store"
    ).save(f"{cv_id}.{ext}", data)

    candidate_id = req_id = None
    map_status = "pool"
    if name:
        if tenant_id:
            cand = await asyncio.to_thread(
                query_one,
                "SELECT id FROM candidate WHERE LOWER(TRIM(full_name)) = LOWER(TRIM(%s)) AND tenant_id=%s LIMIT 1",
                [name, tenant_id],
            )
        else:
            cand = await asyncio.to_thread(
                query_one,
                "SELECT id FROM candidate WHERE LOWER(TRIM(full_name)) = LOWER(TRIM(%s)) LIMIT 1",
                [name],
            )
        if cand:
            candidate_id = str(cand["id"])
            app_row = await asyncio.to_thread(
                query_one,
                """SELECT requisition_id FROM application WHERE candidate_id=%s
                   ORDER BY applied_at DESC LIMIT 1""",
                [candidate_id],
            )
            if app_row:
                req_id = str(app_row["requisition_id"]) if app_row["requisition_id"] else None
            map_status = "mapped"

    tenant_col, tenant_ph, tenant_val = ("tenant_id, ", "%s, ", [tenant_id]) if tenant_id else ("", "", [])
    await asyncio.to_thread(
        query,
        f"""INSERT INTO cv_repository
           ({tenant_col}id, file_name, file_path, file_hash, file_ext, candidate_name,
            candidate_id, requisition_id, map_status, raw_text,
            text_vector, skills, source)
           VALUES ({tenant_ph}%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                   to_tsvector('english', %s), %s, 'email')""",
        [*tenant_val, cv_id, filename, dest, file_hash, ext, name,
         candidate_id, req_id, map_status, raw_text,
         raw_text or '', skills],
        False,
    )
