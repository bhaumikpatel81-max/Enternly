"""
Email template service.

Loads templates from the email_template table (keyed by template_key).
Falls back to built-in defaults so email never silently fails if the DB row
is missing.  Validates and substitutes {{placeholder}} tokens at send time —
NEVER sends a message containing raw {{ }} braces.
"""
import json
import re
from typing import Optional

from ..db import query, query_one

# ── Placeholder regex ─────────────────────────────────────────────────────────
_PH_RE = re.compile(r'\{\{(\w+)\}\}')

# ── Built-in defaults ─────────────────────────────────────────────────────────
# These reproduce the CURRENT hardcoded emails exactly.
# They are inserted into the DB if the row is absent (idempotent via template_key).

DEFAULTS: dict[str, dict] = {
    "enteri_ai_invite": {
        "name":    "Enteri AI Interview Invite (Candidate)",
        "subject": "AI Interview Invitation: {{job_title}} — {{company_name}}",
        "body": (
            "Hi {{candidate_name}},\n\n"
            "Congratulations! You have been shortlisted for an AI Screening Interview "
            "for the position of {{job_title}} at {{company_name}}.\n\n"
            "Please use the link below to attend your interview at your convenience:\n"
            "  {{interview_link}}\n\n"
            "The interview takes approximately 25–30 minutes. "
            "You will need a microphone and a quiet environment.\n\n"
            "Important:\n"
            "- Once you start, you have 48 hours to complete the interview\n"
            "- You can close and re-open the link within that window if needed\n"
            "- Enteri AI never auto-rejects — all scores are reviewed by a human recruiter\n\n"
            "Best regards,\n"
            "{{recruiter_name}} | {{company_name}}"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "company_name", "interview_link",
            "recruiter_name", "recruiter_email",
        ],
        "category": "candidate",
    },
    "enteri_ai_completion": {
        "name":    "Enteri AI Interview Completed (Recruiter)",
        "subject": "Enteri AI interview completed — {{candidate_name}} — {{job_title}}",
        "body": (
            "Enteri AI Interview Completed\n\n"
            "Candidate: {{candidate_name}}\n"
            "Role: {{job_title}}\n"
            "AI Score: {{ai_score}}\n\n"
            "Strengths:\n{{strengths}}\n\n"
            "Areas to Probe:\n{{concerns}}\n\n"
            "Regards,\n{{company_name}} Hiring System"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "ai_score", "strengths", "concerns",
            "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "panel",
    },
    "interview_scheduled": {
        "name":    "Interview Scheduled (Candidate)",
        "subject": "Interview scheduled: {{job_title}}",
        "body": (
            "Hi {{candidate_name}},\n\n"
            "Your interview for {{job_title}} has been scheduled.\n\n"
            "Date & Time: {{interview_time}}\n"
            "Meeting Link: {{meet_link}}\n\n"
            "Please join on time. If you have any questions, please reply to this email.\n\n"
            "Regards,\n{{recruiter_name}}\n{{company_name}} Talent Acquisition"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "interview_time", "meet_link",
            "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "candidate",
    },

    # ── Phase 4, Part C — proctoring integrity digest (TA manager/admin) ─────

    "proctoring_integrity_alert": {
        "name":    "Proctoring Integrity Alert (Recruiter/TA Digest)",
        "subject": "⚠ Proctoring review needed — {{candidate_name}} — {{job_title}}",
        "body": (
            "A Enteri AI proctoring session has integrity signals that need human review.\n\n"
            "Candidate: {{candidate_name}}\n"
            "Role: {{job_title}}\n\n"
            "What was flagged:\n"
            "{{flag_summary}}\n\n"
            "IMPORTANT: Nothing was auto-terminated because of this. These signals "
            "are informational only — please watch the recording yourself and make "
            "the call. No automated decision has been made.\n\n"
            "Review this session: {{review_link}}\n\n"
            "Regards,\n{{company_name}} Proctoring System"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "flag_summary", "review_link",
            "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "proctoring",
    },

    # ── Phase 7, Fix 1 follow-up — relink notification (TA manager/admin) ────

    "proctoring_relink_notification": {
        "name":    "Proctoring Appeal Relinked (TA/Admin Notice)",
        "subject": "Proctoring appeal relinked — {{candidate_name}} — {{job_title}}",
        "body": (
            "{{recruiter_name}} has relinked a proctoring appeal, sending "
            "{{candidate_name}} a fresh interview attempt.\n\n"
            "Role: {{job_title}}\n"
            "Original termination reason: {{termination_reason}}\n\n"
            "This is an informational notice only — no action is required unless "
            "you want to review the decision yourself.\n\n"
            "Regards,\n{{company_name}} Proctoring System"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "recruiter_name", "termination_reason",
            "company_name", "recruiter_email",
        ],
        "category": "proctoring",
    },

    # ── Meeting Notetaker email template ─────────────────────────────────────

    "meeting_summary": {
        "name":    "Interview Transcript Summary (Recruiter)",
        "subject": "Interview summary: {{candidate_name}} — {{job_title}} ({{interview_date}})",
        "body": (
            "Hi {{recruiter_name}},\n\n"
            "Here is the AI-generated summary for your interview with {{candidate_name}} "
            "for the position of {{job_title}} on {{interview_date}}.\n\n"
            "── DISCUSSION POINTS ──\n{{discussion_points}}\n\n"
            "── STRENGTHS ──\n{{strengths}}\n\n"
            "── CONCERNS / GAPS ──\n{{concerns}}\n\n"
            "── OVERALL NOTE ──\n{{overall_note}}\n\n"
            "The full transcript is available in Enternly (Interviews → Transcript).\n\n"
            "Regards,\n{{company_name}} Hiring System"
        ),
        "valid_placeholders": [
            "recruiter_name", "candidate_name", "job_title", "interview_date",
            "discussion_points", "strengths", "concerns", "overall_note",
            "company_name", "recruiter_email",
        ],
        "category": "panel",
    },

    # ── Offer & Approvals email templates ─────────────────────────────────────

    "offer_awaiting_approval": {
        "name":    "Offer Awaiting Approval (Approver)",
        "subject": "Action required: Offer approval needed — {{candidate_name}} ({{job_title}})",
        "body": (
            "Hi {{approver_name}},\n\n"
            "An offer is awaiting your approval (step {{step_num}} of {{total_steps}}).\n\n"
            "Candidate:    {{candidate_name}}\n"
            "Role:         {{job_title}}\n"
            "Designation:  {{designation}}\n"
            "Total CTC:    {{total_ctc}}\n"
            "Joining Date: {{joining_date}}\n\n"
            "Please log in to Enternly and navigate to Offers & Approvals to approve or reject this offer.\n\n"
            "Regards,\n{{recruiter_name}}\n{{company_name}} Talent Acquisition"
        ),
        "valid_placeholders": [
            "approver_name", "candidate_name", "job_title", "designation",
            "total_ctc", "joining_date", "step_num", "total_steps",
            "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "panel",
    },

    "offer_step_approved": {
        "name":    "Offer Step Approved — Audit (Recruiter + TA Manager)",
        "subject": "Offer approved at step {{step_num}}/{{total_steps}} — {{candidate_name}} ({{job_title}})",
        "body": (
            "Offer Approval Audit\n\n"
            "Candidate:   {{candidate_name}}\n"
            "Role:        {{job_title}}\n"
            "Approved by: {{approver_name}}\n"
            "Step:        {{step_num}} of {{total_steps}}\n"
            "At:          {{approved_at}}\n"
            "Notes:       {{notes}}\n\n"
            "This is an automated audit notification. No action is required at this stage.\n\n"
            "Regards,\n{{company_name}} Hiring System"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "approver_name",
            "step_num", "total_steps", "approved_at", "notes",
            "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "panel",
    },

    "offer_rejected": {
        "name":    "Offer Rejected — Action Required (Recruiter + TA Manager)",
        "subject": "Offer REJECTED at step {{step_num}} — {{candidate_name}} ({{job_title}})",
        "body": (
            "Offer Rejected\n\n"
            "Candidate:   {{candidate_name}}\n"
            "Role:        {{job_title}}\n"
            "Rejected by: {{approver_name}}\n"
            "Step:        {{step_num}}\n"
            "Reason:      {{notes}}\n"
            "At:          {{rejected_at}}\n\n"
            "The offer is now in 'Revising' state. The recruiter must update the offer details "
            "and resubmit it to restart the approval chain.\n\n"
            "Regards,\n{{company_name}} Hiring System"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "approver_name",
            "step_num", "notes", "rejected_at",
            "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "panel",
    },

    # ── HM Requisition Approval email templates ──────────────────────────────

    "hm_req_approval_request": {
        "name":    "HM Requisition Approval Request (TA Manager)",
        "subject": "Approval required: New requisition '{{req_title}}' from {{hm_name}}",
        "body": (
            "Hi TA Team,\n\n"
            "{{hm_name}} has created a new requisition that requires your approval "
            "before it becomes active in the pipeline.\n\n"
            "Requisition: {{req_title}}\n"
            "Submitted by: {{hm_name}} (Hiring Manager)\n\n"
            "Please log in to Enternly and navigate to 'Req Approvals' to approve or "
            "reject this requisition.\n\n"
            "Regards,\n{{company_name}} Hiring System"
        ),
        "valid_placeholders": [
            "hm_name", "req_title",
            "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "panel",
    },
    "hm_req_approved": {
        "name":    "HM Requisition Approved (Hiring Manager Notification)",
        "subject": "Your requisition '{{req_title}}' has been approved",
        "body": (
            "Hi {{hm_name}},\n\n"
            "Your requisition '{{req_title}}' has been approved by the TA team "
            "and is now active in the pipeline.\n\n"
            "Candidates can now be received and processed for this position.\n\n"
            "Regards,\n{{company_name}} Hiring System"
        ),
        "valid_placeholders": [
            "hm_name", "req_title",
            "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "panel",
    },
    "hm_req_rejected": {
        "name":    "HM Requisition Rejected (Hiring Manager Notification)",
        "subject": "Your requisition '{{req_title}}' was not approved",
        "body": (
            "Hi {{hm_name}},\n\n"
            "Your requisition '{{req_title}}' could not be approved at this time.\n\n"
            "Reason: {{reason}}\n\n"
            "Please contact the TA team if you have questions or wish to revise "
            "and resubmit.\n\n"
            "Regards,\n{{company_name}} Hiring System"
        ),
        "valid_placeholders": [
            "hm_name", "req_title", "reason",
            "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "panel",
    },

    "application_received_jd": {
        "name":    "Application Received — Job Description (Candidate Confirmation)",
        "subject": "Your application for {{job_title}} has been received",
        "body": (
            "Hi {{candidate_name}},\n\n"
            "Thank you for applying — your application for {{job_title}} "
            "({{location}}) has been submitted successfully.\n\n"
            "Please find the job description below:\n\n"
            "──────────────────────────────\n"
            "Job Title:      {{job_title}}\n"
            "Location:       {{location}}\n"
            "Experience:     {{experience}}\n"
            "Qualification:  {{qualification}}\n"
            "──────────────────────────────\n\n"
            "{{jd_body}}\n\n"
            "About {{company_name}}:\n{{about_company}}\n\n"
            "Our recruitment team will review your profile and be in touch.\n\n"
            "Regards,\n{{recruiter_name}}\n{{company_name}} Talent Acquisition"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "location",
            "experience", "qualification", "jd_body", "about_company",
            "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "candidate",
    },

    "application_rejected": {
        "name":    "Application Rejected (Candidate)",
        "subject": "Update on your application — {{job_title}}",
        "body": (
            "Hi {{candidate_name}},\n\n"
            "Thank you for your interest in the {{job_title}} role at {{company_name}}, and for "
            "the time you invested in our hiring process.\n\n"
            "After careful consideration, we have decided not to move forward with your application "
            "at this time. This decision doesn't diminish the strength of your background — we simply "
            "found candidates whose experience more closely matched this specific role.\n\n"
            "We encourage you to apply for future openings at {{company_name}} that match your skills "
            "and experience.\n\n"
            "Wishing you all the best in your search.\n\n"
            "Regards,\n{{recruiter_name}}\n{{company_name}} Talent Acquisition"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "candidate",
    },

    "offer_approved_darwinbox": {
        "name":    "Offer Fully Approved — Sent to Darwinbox (Recruiter + TA Manager)",
        "subject": "Offer fully approved — {{candidate_name}} sent to Darwinbox (Ref: {{darwin_ref}})",
        "body": (
            "Offer Fully Approved\n\n"
            "Candidate:    {{candidate_name}}\n"
            "Role:         {{job_title}}\n"
            "Designation:  {{designation}}\n"
            "Total CTC:    {{total_ctc}}\n"
            "Joining Date: {{joining_date}}\n"
            "Darwinbox Ref: {{darwin_ref}}\n"
            "Approved At:  {{approved_at}}\n\n"
            "The offer has cleared all approval steps and has been handed off to Darwinbox "
            "for letter generation and onboarding initiation.\n\n"
            "Regards,\n{{company_name}} Hiring System"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "designation", "total_ctc",
            "joining_date", "darwin_ref", "approved_at",
            "company_name", "recruiter_name", "recruiter_email",
        ],
        "category": "panel",
    },
}

# Placeholders guaranteed to be fillable for ANY application (used by custom templates)
CUSTOM_PLACEHOLDERS: list[str] = [
    "candidate_name", "job_title", "company_name", "recruiter_name", "recruiter_email",
]

# Keys of all built-in templates (used to guard against deletion)
BUILTIN_KEYS: frozenset[str] = frozenset(DEFAULTS)

# ── Sample data for live preview ──────────────────────────────────────────────
SAMPLE_VALUES: dict[str, str] = {
    "candidate_name":     "Rimjhim Rai",
    "job_title":          "Account Manager – Sales",
    "company_name":       "EnternsTech Pvt. Ltd.",
    "interview_link":     "https://your-enternly-domain.example/enteri-ai-interview?token=preview_sample",
    "ai_score":           "78/100",
    "strengths":          "Strong communication, relevant industry experience, clear articulation of achievements.",
    "concerns":           "Limited enterprise CRM experience — probe on technical sales cycle management.",
    "interview_time":     "Thursday, 12 June 2026 at 11:00 AM IST",
    "meet_link":          "https://meet.google.com/abc-defg-hij",
    "recruiter_name":     "Priya Sharma",
    "recruiter_email":    "priya.sharma@enternly.example",
    # meeting_summary placeholders
    "interview_date":     "12 June 2026",
    "discussion_points":  "Candidate's sales experience, key accounts managed, CRM tools used.",
    "overall_note":       "Strong candidate — recommend advancing to panel interview.",
    # offer email placeholders
    "approver_name":      "Rajesh Mehta",
    "designation":        "Senior Account Manager",
    "total_ctc":          "₹14,00,000",
    "joining_date":       "01 August 2026",
    "step_num":           "1",
    "total_steps":        "3",
    "approved_at":        "12 June 2026 at 2:30 PM",
    "rejected_at":        "12 June 2026 at 2:30 PM",
    "notes":              "All requirements met.",
    "darwin_ref":         "STUB-DRW-2026001",
    # hm req approval placeholders
    "hm_name":   "Bhaumik Patel",
    "req_title": "Senior Software Engineer",
    "reason":    "Budget not approved for this quarter.",
    # application_received_jd placeholders
    "location":       "Ahmedabad, India",
    "experience":     "3–5 years",
    "qualification":  "B.E. / B.Tech in Computer Science or equivalent",
    "jd_body":        "We are looking for a motivated engineer to join our team...",
    "about_company":  "EnternsTech Pvt. Ltd. is a leading technology company.",
}


# ── DB helpers ────────────────────────────────────────────────────────────────

def ensure_defaults() -> None:
    """
    Insert built-in default templates for any template_key not yet in the DB.
    Called once at application startup — idempotent.
    """
    for key, tmpl in DEFAULTS.items():
        existing = query_one(
            "SELECT id FROM email_template WHERE template_key = %s LIMIT 1",
            [key],
        )
        if not existing:
            try:
                query(
                    """INSERT INTO email_template
                       (name, subject, body, category, template_key, valid_placeholders, is_builtin)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb, TRUE)""",
                    [
                        tmpl["name"], tmpl["subject"], tmpl["body"],
                        tmpl.get("category", ""),
                        key,
                        json.dumps(tmpl["valid_placeholders"]),
                    ],
                    fetch=False,
                )
            except Exception as exc:
                print(f"[email_templates] Could not seed default '{key}': {exc}")
        else:
            try:
                query(
                    "UPDATE email_template SET is_builtin = TRUE WHERE template_key = %s",
                    [key],
                    fetch=False,
                )
            except Exception:
                pass


def get_template(key: str) -> dict:
    """
    Return the template for *key*.

    Priority:
      1. DB row with matching template_key (most-recently edited by admin)
      2. Built-in default (guarantees email can always be sent)
    """
    try:
        row = query_one(
            "SELECT name, subject, body, valid_placeholders, category "
            "FROM email_template WHERE template_key = %s AND is_active = TRUE LIMIT 1",
            [key],
        )
    except Exception:
        row = None  # DB schema not yet migrated — fall through to built-in default
    if row:
        vp = row["valid_placeholders"]
        if isinstance(vp, str):
            try:
                vp = json.loads(vp)
            except Exception:
                vp = []
        vp = vp or []
        return {
            "template_key":       key,
            "name":               row["name"],
            "subject":            row["subject"],
            "body":               row["body"],
            "valid_placeholders": vp,
            "category":           row.get("category", ""),
            "source":             "db",
        }

    default = DEFAULTS.get(key)
    if default:
        return {
            "template_key":       key,
            "name":               default["name"],
            "subject":            default["subject"],
            "body":               default["body"],
            "valid_placeholders": default["valid_placeholders"],
            "category":           default.get("category", ""),
            "source":             "default",
        }

    raise KeyError(f"No email template found for key '{key}'")


def _find_placeholders(text: str) -> set[str]:
    return set(_PH_RE.findall(text or ""))


def _substitute(text: str, values: dict) -> str:
    return _PH_RE.sub(lambda m: str(values[m.group(1)]), text)


def render_template(
    key: str,
    values: dict,
    req_id: str | None = None,
    actor: dict | None = None,
) -> tuple[str, str]:
    """
    Load template for *key*, substitute {{placeholders}} with *values*.

    Global placeholders (company_name, recruiter_name, recruiter_email) are
    auto-resolved via resolve_global_placeholders and merged BEFORE checking
    for missing values — callers do not need to supply them explicitly.

    Returns (rendered_subject, rendered_body).

    Raises ValueError if any placeholder in subject or body cannot be filled —
    the caller must handle this and NEVER send a message containing raw braces.
    """
    from .connectors import resolve_global_placeholders  # local import — avoids circular dep

    tmpl = get_template(key)
    subject = tmpl["subject"] or ""
    body    = tmpl["body"]    or ""

    # Merge globals first; caller-supplied values override globals
    merged = resolve_global_placeholders(req_id=req_id, actor=actor)
    merged.update(values)

    all_ph = _find_placeholders(subject) | _find_placeholders(body)
    # A placeholder counts as missing only if the key is truly absent (or
    # explicitly None) — a legitimate empty string ("") is a valid value
    # and should render as empty text, not block the whole email.
    missing = [p for p in all_ph if p not in merged or merged.get(p) is None]
    if missing:
        raise ValueError(
            f"placeholder(s) have no value: {', '.join(sorted(missing))}"
        )

    return _substitute(subject, merged), _substitute(body, merged)


def validate_placeholders(key: str, subject: str, body: str) -> list[str]:
    """
    Return a list of warning strings for placeholders in subject/body that are
    not in the valid_placeholders list for this template type.
    Empty list means everything is fine.
    """
    if key in DEFAULTS:
        valid = set(DEFAULTS[key].get("valid_placeholders", []))
    else:
        # Custom template: derive valid set from DB row
        row = query_one(
            "SELECT valid_placeholders FROM email_template WHERE template_key = %s LIMIT 1",
            [key],
        )
        vp = row["valid_placeholders"] if row else []
        if isinstance(vp, str):
            try:
                vp = json.loads(vp)
            except Exception:
                vp = []
        valid = set(vp or CUSTOM_PLACEHOLDERS)
    used    = _find_placeholders(subject) | _find_placeholders(body)
    unknown = used - valid
    return [f"Unknown placeholder: {{{{{p}}}}}" for p in sorted(unknown)]


def fix_legacy_templates() -> None:
    """
    Old seed data (pre-migration-26) has template_key = NULL.
    Assign a stable key so these templates become editable via the API.
    Idempotent — skips rows that already have a key.
    """
    try:
        legacy = query(
            "SELECT id, name FROM email_template WHERE template_key IS NULL AND is_builtin = FALSE"
        )
        for row in (legacy or []):
            slug = re.sub(r'[^a-z0-9]+', '_', (row['name'] or '').lower()).strip('_')
            key  = f"custom_{slug}"
            # Avoid key collisions
            taken = query_one(
                "SELECT id FROM email_template WHERE template_key = %s LIMIT 1", [key]
            )
            if taken:
                key = f"{key}_{row['id']}"
            try:
                query(
                    "UPDATE email_template SET template_key = %s WHERE id = %s",
                    [key, row['id']],
                    fetch=False,
                )
                print(f"[email_templates] assigned key '{key}' to legacy template '{row['name']}'")
            except Exception as upd_exc:
                print(f"[email_templates] could not fix legacy template {row['id']}: {upd_exc}")
    except Exception as exc:
        print(f"[email_templates] fix_legacy_templates: {exc}")


def get_custom_templates() -> list[dict]:
    """Return all active custom (non-builtin) templates from the DB.
    Returns [] gracefully if migration 26 hasn't been applied yet."""
    try:
        rows = query(
            """SELECT template_key, name, category, valid_placeholders
               FROM email_template
               WHERE is_builtin = FALSE AND is_active = TRUE AND template_key IS NOT NULL
               ORDER BY name""",
        )
    except Exception:
        return []
    result = []
    for r in (rows or []):
        vp = r["valid_placeholders"]
        if isinstance(vp, str):
            try:
                vp = json.loads(vp)
            except Exception:
                vp = []
        result.append({
            "template_key":       r["template_key"],
            "name":               r["name"],
            "category":           r.get("category", "custom"),
            "valid_placeholders": vp or CUSTOM_PLACEHOLDERS,
            "is_builtin":         False,
        })
    return result
