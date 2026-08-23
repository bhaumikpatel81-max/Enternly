"""
Phase 4, Part C — proctoring integrity digest emails.

Reuses the existing email machinery unchanged: connectors.send_email is the
sender, email_templates.render_template + the 'proctoring_integrity_alert'
DEFAULTS entry is the template. Does not add a new background worker — it's
triggered synchronously (best-effort) from the same call sites that record
integrity flags (see proctoring_api.candidate_complete_session and
nexai_api.terminate_invite_session), mirroring campus_email_worker's atomic
claim-then-send shape but adapted to a per-session trigger instead of a
polling loop over a queue table.
"""
from ..db import query, query_one
from .connectors import send_email, _load_email_cfg
from .email_templates import render_template


_PLAIN_LANGUAGE = {
    'monitoring_gap': lambda d: (
        f"Proctoring lost signal for {_fmt_duration(d.get('duration_seconds'))} "
        f"starting at {d.get('gap_start', '?')} — could be connectivity or tampering."
    ),
    'termination_discrepancy': lambda d: (
        f"The candidate's browser reported ending the interview "
        f"({d.get('browser_strike_count', '?')} claimed strikes, reason: "
        f"\"{d.get('browser_reason') or '—'}\"), but the server's own records do not "
        f"support that (server finding: {d.get('server_outcome', '?')})."
    ),
    'secret_misuse': lambda d: (
        f"A request to {d.get('endpoint', 'a proctoring endpoint')} was rejected for "
        f"using a wrong or revoked security token against this session, at "
        f"{d.get('detected_at', '?')} — a possible tampering attempt."
    ),
    'impossible_data': lambda d: (
        f"Proctoring data for this session looked inconsistent with how the "
        f"system expects it to behave: {d.get('detail') or d}."
    ),
}


def _fmt_duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "an unknown duration"
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _plain_language(flag_kind, detail):
    fn = _PLAIN_LANGUAGE.get(flag_kind)
    if not fn:
        return f"{flag_kind}: {detail}"
    try:
        return fn(detail or {})
    except Exception:
        return f"{flag_kind}: {detail}"


def _resolve_recipients(session_id):
    """Invite creator's email (if resolvable) + every ta_manager/admin's email."""
    emails = set()
    creator = query_one(
        """SELECT cu.email AS creator_email
           FROM proctoring_session ps
           JOIN nexai_invite ni ON ni.application_id = ps.application_id
           LEFT JOIN app_user cu ON cu.id = ni.created_by
           WHERE ps.id = %s
           ORDER BY ni.invited_at DESC LIMIT 1""",
        [session_id],
    )
    if creator and creator.get('creator_email'):
        emails.add(creator['creator_email'])

    ta_admins = query(
        "SELECT email FROM app_user WHERE role IN ('ta_manager','admin') AND email IS NOT NULL AND email <> ''"
    ) or []
    for r in ta_admins:
        emails.add(r['email'])

    return sorted(emails)


def send_integrity_digest_for_session(session_id):
    """
    Atomically claims any integrity flags not yet included in a digest for
    this session (UPDATE ... WHERE emailed_at IS NULL RETURNING id — the same
    idempotent-claim shape as campus_email_worker, just per-session instead
    of per-batch). If nothing new, does nothing. If something new exists,
    composes ONE email bundling EVERY currently-unreviewed flag (not just the
    new ones, so recipients always see full context) and sends it to the
    invite creator + all ta_manager/admin users.

    Returns a dict describing what happened — used by callers and by tests;
    never raises (mirrors the rest of this codebase's "email must never break
    the calling flow" convention).
    """
    try:
        claimed = query(
            """UPDATE proctoring_integrity_flag
                   SET emailed_at = now()
               WHERE session_id = %s AND reviewed = false AND emailed_at IS NULL
               RETURNING id""",
            [session_id],
        )
        if not claimed:
            return {"sent": False, "reason": "no_new_flags"}

        all_unreviewed = query(
            """SELECT flag_kind, detail, created_at FROM proctoring_integrity_flag
               WHERE session_id = %s AND reviewed = false
               ORDER BY created_at ASC""",
            [session_id],
        ) or []

        ctx = query_one(
            """SELECT c.full_name AS candidate_name, r.title AS job_title, r.id AS req_id
               FROM proctoring_session ps
               JOIN application a ON a.id = ps.application_id
               JOIN candidate  c ON c.id = a.candidate_id
               JOIN requisition r ON r.id = a.requisition_id
               WHERE ps.id = %s""",
            [session_id],
        )
        if not ctx:
            return {"sent": False, "reason": "session_context_not_found"}

        base_url = (_load_email_cfg().get("base_url") or "http://localhost:8000").strip().rstrip("/")
        review_link = f"{base_url}/#proctoring_review"

        flag_summary = "\n".join(
            f"- {_plain_language(f['flag_kind'], f['detail'])}" for f in all_unreviewed
        ) or "- (no details available)"

        try:
            subject, plain = render_template("proctoring_integrity_alert", {
                "candidate_name": ctx["candidate_name"],
                "job_title":      ctx["job_title"],
                "flag_summary":   flag_summary,
                "review_link":    review_link,
            }, req_id=str(ctx["req_id"]))
        except ValueError as exc:
            print(f"[proctoring_alerts] template error for session {session_id}: {exc}")
            return {"sent": False, "reason": f"template_error: {exc}"}

        recipients = _resolve_recipients(session_id)
        if not recipients:
            return {
                "sent": False, "reason": "no_recipients",
                "subject": subject, "body": plain, "flag_count": len(all_unreviewed),
            }

        send_results = []
        for email in recipients:
            try:
                res = send_email(email, subject, plain)
            except Exception as exc:
                res = {"sent": False, "error": str(exc)}
            send_results.append({"to": email, "result": res})

        return {
            "sent": True,
            "recipients": recipients,
            "subject": subject,
            "body": plain,
            "flag_count": len(all_unreviewed),
            "send_results": send_results,
        }
    except Exception as exc:
        print(f"[proctoring_alerts] digest send failed for session {session_id}: {exc}")
        return {"sent": False, "reason": f"unexpected_error: {exc}"}


def send_relink_notification(candidate_name, job_title, actor, termination_reason):
    """
    Phase 7, Fix 1 follow-up — informational notice to every ta_manager/admin
    whenever a recruiter relinks a proctoring appeal. Does not affect the
    relink itself (caller wraps this in try/except and only calls it after
    the relink has already succeeded); never raises.
    """
    try:
        recipients = query(
            "SELECT email FROM app_user WHERE role IN ('ta_manager','admin') AND email IS NOT NULL AND email <> ''"
        ) or []
        emails = sorted({r['email'] for r in recipients})
        if not emails:
            return {"sent": False, "reason": "no_recipients"}

        try:
            subject, plain = render_template("proctoring_relink_notification", {
                "candidate_name":     candidate_name or "Candidate",
                "job_title":          job_title or "—",
                "termination_reason": termination_reason or "Not recorded",
            }, actor=actor)
        except ValueError as exc:
            print(f"[proctoring_alerts] relink notification template error: {exc}")
            return {"sent": False, "reason": f"template_error: {exc}"}

        send_results = []
        for email in emails:
            try:
                res = send_email(email, subject, plain)
            except Exception as exc:
                res = {"sent": False, "error": str(exc)}
            send_results.append({"to": email, "result": res})

        return {"sent": True, "recipients": emails, "subject": subject, "body": plain, "send_results": send_results}
    except Exception as exc:
        print(f"[proctoring_alerts] relink notification failed: {exc}")
        return {"sent": False, "reason": f"unexpected_error: {exc}"}
