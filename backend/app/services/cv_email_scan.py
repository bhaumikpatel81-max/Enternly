"""
Shared Gmail IMAP CV-attachment scan.

Used by both:
  - routers/cv_api.py's POST /api/cv/scan-email (a recruiter clicking
    "Scan My Email" — on-demand, single mailbox)
  - services/recruiter_email_worker.py (background poller — loops over
    every recruiter who has a Gmail App Password configured)

Emails vary a lot in sender, subject, and attachment naming (Naukri,
LinkedIn, Indeed, direct candidate emails, referrals — none share a
convention), so this does NOT filter by sender or filename pattern. Instead
every PDF/DOCX/DOC attachment goes through cv_enricher.classify_and_enrich()
— a cheap keyword pre-filter first, then one thorough Groq call that both
decides is_resume and extracts the profile fields — before it's ever
stored. A single email with multiple attachments (e.g. a resume plus an
unrelated form) only keeps the resume; the rest are counted as "skipped"
and logged with a reason, never silently discarded without a trace.
"""
from datetime import datetime, timedelta
from pathlib import Path

from . import cv_parser as _parser
from .cv_ingest import ingest_one

CV_EXTS = {".pdf", ".docx", ".doc"}

# First-ever scan of a mailbox (no checkpoint yet) looks back this far.
_DEFAULT_LOOKBACK_DAYS = 30
# Re-scans re-check a couple of days before the last checkpoint rather than
# exactly at it, to absorb IMAP's date-only (no time-of-day) SINCE precision
# and any clock skew between this server and Gmail.
_CHECKPOINT_BUFFER_DAYS = 2


def scan_message_for_cvs(msg, uploaded_by, tenant_id=None) -> dict:
    """
    Walk one already-parsed email.message.Message for CV-like attachments.
    Returns {"processed", "mapped", "pooled", "duplicates", "skipped", "errors"}.
    """
    from ..db import query_one

    result = {"processed": 0, "mapped": 0, "pooled": 0,
              "duplicates": 0, "skipped": 0, "errors": []}

    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        ext = Path(filename).suffix.lower()
        if ext not in CV_EXTS:
            continue
        try:
            file_data = part.get_payload(decode=True)
            if not file_data:
                continue

            # Hash-check BEFORE extracting text — a widened scan window
            # (see scan_gmail_inbox) means the same already-ingested
            # attachment gets walked again on every cycle; skip the
            # expensive PDF/DOCX parse for anything we already have.
            file_hash = _parser.sha256_hash(file_data)
            if tenant_id:
                dup = query_one("SELECT id FROM cv_repository WHERE file_hash=%s AND tenant_id=%s", [file_hash, tenant_id])
            else:
                dup = query_one("SELECT id FROM cv_repository WHERE file_hash=%s", [file_hash])
            if dup:
                result["duplicates"] += 1
                continue

            raw_text = _parser.extract_text(file_data, ext.lstrip("."))

            from .cv_enricher import classify_and_enrich_sync
            accept, reason, llm_result = classify_and_enrich_sync(raw_text, filename)
            if not accept:
                result["skipped"] += 1
                result["errors"].append({"file": filename, "error": f"skipped (not a CV — {reason})"})
                continue

            r = ingest_one(file_data, filename, "email_ingest", uploaded_by, raw_text=raw_text, tenant_id=tenant_id)
            if r["status"] == "ok":
                result["processed"] += 1
                if r.get("mapped"):
                    result["mapped"] += 1
                else:
                    result["pooled"] += 1
                if llm_result and llm_result.get("is_resume"):
                    from .cv_enricher import _persist_enrichment_result
                    _persist_enrichment_result(r["cv_id"], llm_result)
            elif r["status"] == "duplicate":
                result["duplicates"] += 1
            else:
                result["errors"].append({"file": filename, "error": r.get("error", "unknown")})
        except Exception as exc:
            result["errors"].append({"file": filename, "error": str(exc)})

    return result


def scan_gmail_inbox(gmail_address: str, app_password: str, uploaded_by, since: datetime = None, should_stop=None, tenant_id=None) -> dict:
    """
    Log into one Gmail inbox via IMAP and scan for CV attachments.

    Deliberately does NOT filter on the IMAP \\Seen flag — a recruiter who
    simply opened/read a candidate's email (as anyone naturally would)
    marks it read, and an "UNSEEN only" search would then skip it forever
    even though the CV was never ingested. Instead this scans every message
    since `since` (or the last _DEFAULT_LOOKBACK_DAYS days on a mailbox's
    first-ever scan) regardless of read state, and relies on the
    file-hash dedup in scan_message_for_cvs/ingest_one to make re-scanning
    the same window cheap and side-effect-free.

    Raises on IMAP login failure so callers can distinguish "bad
    credentials" from "zero attachments found".
    """
    import imaplib
    import email as _email_lib
    from email import policy as _ep

    IMAP_HOST = "imap.gmail.com"
    IMAP_PORT = 993

    totals = {"processed": 0, "mapped": 0, "pooled": 0,
              "duplicates": 0, "skipped": 0, "errors": [], "cancelled": False}

    if since is not None:
        cutoff = since - timedelta(days=_CHECKPOINT_BUFFER_DAYS)
    else:
        cutoff = datetime.utcnow() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)

    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(gmail_address, app_password)
    try:
        mail.select("INBOX")

        # Gmail's IMAP extension lets the SEARCH itself filter to only
        # messages with an attachment, in the same round trip as the date
        # window — most real inbox traffic (notifications, threads,
        # newsletters) has no attachment at all, and fetching+parsing the
        # full RFC822 body for each of those just to find nothing was most
        # of the wall-clock time a scan spent. Falls back to the plain
        # date-only search if the server ever rejects the Gmail-specific
        # query (defensive — this scanner only ever targets imap.gmail.com,
        # so X-GM-RAW should always be available in practice).
        gmail_query = f'has:attachment after:{cutoff.strftime("%Y/%m/%d")}'
        try:
            _, msg_ids = mail.search(None, "X-GM-RAW", f'"{gmail_query}"')
        except Exception as exc:
            print(f"[cv-email-scan] X-GM-RAW search failed ({exc}), falling back to date-only search")
            _, msg_ids = mail.search(None, f'(SINCE "{cutoff.strftime("%d-%b-%Y")}")')
        ids = msg_ids[0].split() if msg_ids[0] else []

        for uid in ids:
            if should_stop is not None and should_stop():
                totals["cancelled"] = True
                break

            _, data = mail.fetch(uid, "(RFC822)")
            raw = data[0][1]
            msg = _email_lib.message_from_bytes(raw, policy=_ep.default)

            per_msg = scan_message_for_cvs(msg, uploaded_by, tenant_id)
            for k in ("processed", "mapped", "pooled", "duplicates", "skipped"):
                totals[k] += per_msg[k]
            totals["errors"].extend(per_msg["errors"])
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return totals
