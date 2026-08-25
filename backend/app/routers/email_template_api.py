"""
Email template CRUD endpoints + manual send.

Template management (Company Admin only):
  GET    /api/email-templates              list all templates (built-in + custom)
  POST   /api/email-templates              create custom template
  GET    /api/email-templates/sendable     list sendable custom templates (any recruiter)
  GET    /api/email-templates/{key}        get one template for editing
  PUT    /api/email-templates/{key}        save/update template
  DELETE /api/email-templates/{key}        delete custom template (built-ins protected)
  POST   /api/email-templates/{key}/reset  reset built-in to default
  POST   /api/email-templates/{key}/test-send

Manual send (admin / ta_manager / recruiter):
  POST   /api/applications/{app_id}/send-email
  POST   /api/applications/bulk-send-email
"""
import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user
from ..module_access import recruiter_has_module
from ..services.connectors import send_email
from ..db import query, query_one
from ..services.email_templates import (
    DEFAULTS,
    BUILTIN_KEYS,
    CUSTOM_PLACEHOLDERS,
    SAMPLE_VALUES,
    get_template,
    get_custom_templates,
    render_template,
    validate_placeholders,
)

router = APIRouter()

_PH_RE = re.compile(r'\{\{(\w+)\}\}')


# ── Permission guards ─────────────────────────────────────────────────────────

def _require_template_access(user=Depends(get_current_user)):
    role = user["role"]
    if role in ("admin", "platform_admin", "company_admin"):
        return user
    if role == "recruiter" and recruiter_has_module(user.get("sub"), "email_templates"):
        return user
    raise HTTPException(403, "Email template management is restricted to Company Admins.")


def _require_send_access(user=Depends(get_current_user)):
    if user["role"] not in ("admin", "ta_manager", "recruiter"):
        raise HTTPException(403, "Not authorised to send emails.")
    return user


# ── Schemas ───────────────────────────────────────────────────────────────────

class TemplateSavePayload(BaseModel):
    subject: str
    body: str


class TemplateCreatePayload(BaseModel):
    name: str
    key: str
    subject: str
    body: str


class SendEmailPayload(BaseModel):
    template_key: str
    notes: Optional[str] = None


# ── Helper ────────────────────────────────────────────────────────────────────

def _key_from_name(name: str) -> str:
    """Generate a safe DB key from a template name."""
    k = re.sub(r'[^a-z0-9]+', '_', name.lower().strip()).strip('_')
    return f"custom_{k}"


# ── List (admin / ta_manager) ─────────────────────────────────────────────────

@router.get("/api/email-templates")
def list_templates(user=Depends(_require_template_access)):
    """List all known template keys with metadata (built-in + custom)."""
    result = []
    # Built-in templates
    for key, dflt in DEFAULTS.items():
        row = query_one(
            "SELECT subject, body, updated_at FROM email_template "
            "WHERE template_key = %s AND is_active = TRUE LIMIT 1",
            [key],
        )
        result.append({
            "template_key":       key,
            "name":               dflt["name"],
            "category":           dflt.get("category", ""),
            "valid_placeholders": dflt["valid_placeholders"],
            "is_builtin":         True,
            "is_customised":      row is not None,
            "updated_at":         row["updated_at"].isoformat() if row and row.get("updated_at") else None,
        })
    # Custom templates
    for t in get_custom_templates():
        result.append({
            **t,
            "is_customised": True,
        })
    return result


# ── Sendable list (any recruiter) ─────────────────────────────────────────────
# NOTE: this literal route MUST be defined before GET /{key} to avoid
# FastAPI matching "sendable" as a {key} path parameter.

@router.get("/api/email-templates/sendable")
def list_sendable(user=Depends(_require_send_access)):
    """Return every candidate-facing template available for the manual send
    dialog -- built-in (customised or not) AND custom, not just custom.

    This used to call get_custom_templates() alone, which only returns
    templates with is_builtin=FALSE -- but nearly every template in the
    system (NexAI invite, interview scheduled, application rejected, etc.)
    is seeded as is_builtin=TRUE by ensure_defaults(), even after an admin
    edits its wording in Settings > Email Templates ("customised" there just
    means a DB override row exists, it doesn't flip is_builtin). So the send
    dialog was only ever showing genuinely-from-scratch custom templates,
    which in practice was usually just one leftover duplicate someone made
    as a workaround.

    Restricted to category=='candidate' -- send_email_to_candidate() only
    fills candidate/application/requisition-derived placeholders (name,
    job title, company, recruiter, location/experience/JD text etc.), so
    internal/"panel" templates (offer approvals, HM requisition
    notifications, etc.) would just fail to render here anyway."""
    result = [
        {"template_key": key, "name": dflt["name"], "category": dflt.get("category", ""), "is_builtin": True}
        for key, dflt in DEFAULTS.items()
        if dflt.get("category") == "candidate"
    ]
    result += [t for t in get_custom_templates() if t.get("category") in ("candidate", "custom", "")]
    result.sort(key=lambda t: t["name"])
    return result


# ── Create custom template ────────────────────────────────────────────────────

@router.post("/api/email-templates")
def create_template(payload: TemplateCreatePayload, user=Depends(_require_template_access)):
    """Create a new custom email template."""
    name    = payload.name.strip()
    key     = payload.key.strip()
    subject = payload.subject.strip()
    body    = payload.body.strip()

    if not name or not key or not subject or not body:
        raise HTTPException(422, "name, key, subject, and body are all required.")
    if key in BUILTIN_KEYS:
        raise HTTPException(409, f"'{key}' is a built-in template key. Choose a different key.")

    existing = query_one(
        "SELECT id FROM email_template WHERE template_key = %s LIMIT 1", [key]
    )
    if existing:
        raise HTTPException(409, f"A template with key '{key}' already exists.")

    # Validate: warn on placeholders outside the allowed custom set
    used    = set(_PH_RE.findall(subject)) | set(_PH_RE.findall(body))
    unknown = [p for p in used if p not in CUSTOM_PLACEHOLDERS]

    query(
        """INSERT INTO email_template
           (name, subject, body, category, template_key, valid_placeholders, is_builtin, created_by)
           VALUES (%s, %s, %s, 'custom', %s, %s::jsonb, FALSE, %s)""",
        [name, subject, body, key, json.dumps(CUSTOM_PLACEHOLDERS), user["sub"]],
        fetch=False,
    )

    return {
        "ok": True,
        "template_key": key,
        "warnings": [f"Unrecognised placeholder: {{{{{p}}}}} — will fail at send time if not filled" for p in unknown],
    }


# ── Get one template (built-in or custom) ─────────────────────────────────────

@router.get("/api/email-templates/{key}")
def get_one_template(key: str, user=Depends(_require_template_access)):
    """Return full template (subject + body) for editing."""
    if key not in DEFAULTS:
        # Check if it's a custom template
        row = query_one(
            "SELECT name, subject, body, valid_placeholders, category "
            "FROM email_template WHERE template_key = %s AND is_active = TRUE LIMIT 1",
            [key],
        )
        if not row:
            raise HTTPException(404, f"Unknown template key '{key}'")
        vp = row["valid_placeholders"]
        if isinstance(vp, str):
            try:
                vp = json.loads(vp)
            except Exception:
                vp = []
        tmpl = {
            "template_key":       key,
            "name":               row["name"],
            "subject":            row["subject"],
            "body":               row["body"],
            "valid_placeholders": vp or CUSTOM_PLACEHOLDERS,
            "category":           row.get("category", "custom"),
            "source":             "db",
            "is_builtin":         False,
        }
    else:
        tmpl = get_template(key)
        tmpl["is_builtin"] = True
    tmpl["sample_values"] = SAMPLE_VALUES
    return tmpl


# ── Save / update template ────────────────────────────────────────────────────

@router.put("/api/email-templates/{key}")
def save_template(key: str, payload: TemplateSavePayload, user=Depends(_require_template_access)):
    """
    Save (upsert) a template.  Warns on unknown placeholders but does NOT block.
    Works for both built-in and custom templates.
    """
    subject = payload.subject.strip()
    body    = payload.body.strip()
    if not subject or not body:
        raise HTTPException(422, "Subject and body cannot be empty.")

    if key in DEFAULTS:
        # Built-in template
        warnings = validate_placeholders(key, subject, body)
        dflt = DEFAULTS[key]
        existing = query_one(
            "SELECT id FROM email_template WHERE template_key = %s LIMIT 1", [key]
        )
        if existing:
            query(
                """UPDATE email_template
                   SET subject = %s, body = %s, updated_at = now(), updated_by = %s
                   WHERE template_key = %s""",
                [subject, body, user["sub"], key],
                fetch=False,
            )
        else:
            query(
                """INSERT INTO email_template
                   (name, subject, body, category, template_key, valid_placeholders, is_builtin, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, TRUE, %s)""",
                [
                    dflt["name"], subject, body,
                    dflt.get("category", ""), key,
                    json.dumps(dflt["valid_placeholders"]),
                    user["sub"],
                ],
                fetch=False,
            )
    else:
        # Custom template — query without is_builtin to stay resilient if migration hasn't run
        existing = query_one(
            "SELECT id, is_builtin FROM email_template WHERE template_key = %s LIMIT 1",
            [key],
        )
        if not existing:
            raise HTTPException(404, f"Custom template '{key}' not found.")
        if existing.get("is_builtin"):
            raise HTTPException(403, "Cannot edit a built-in template via this path.")
        warnings = validate_placeholders(key, subject, body)
        query(
            """UPDATE email_template
               SET subject = %s, body = %s, updated_at = now(), updated_by = %s
               WHERE template_key = %s""",
            [subject, body, user["sub"], key],
            fetch=False,
        )

    return {"ok": True, "warnings": warnings}


# ── Delete custom template ────────────────────────────────────────────────────

@router.delete("/api/email-templates/{key}")
def delete_template(key: str, user=Depends(_require_template_access)):
    """Delete a custom template. Built-in templates cannot be deleted."""
    if key in BUILTIN_KEYS:
        raise HTTPException(403, "Built-in templates cannot be deleted. You can reset or edit them.")
    existing = query_one(
        "SELECT id, is_builtin FROM email_template WHERE template_key = %s LIMIT 1",
        [key],
    )
    if not existing:
        raise HTTPException(404, f"Custom template '{key}' not found.")
    if existing.get("is_builtin"):
        raise HTTPException(403, "Built-in templates cannot be deleted.")
    query(
        "DELETE FROM email_template WHERE template_key = %s",
        [key],
        fetch=False,
    )
    return {"ok": True}


# ── Reset built-in to default ─────────────────────────────────────────────────

@router.post("/api/email-templates/{key}/reset")
def reset_template(key: str, user=Depends(_require_template_access)):
    """Reset a built-in template back to its hardcoded default."""
    if key not in DEFAULTS:
        raise HTTPException(404, f"'{key}' is not a built-in template.")

    dflt = DEFAULTS[key]
    query(
        """UPDATE email_template
           SET subject = %s, body = %s, updated_at = now(), updated_by = %s
           WHERE template_key = %s""",
        [dflt["subject"], dflt["body"], user["sub"], key],
        fetch=False,
    )
    return {"ok": True}


# ── Test-send ─────────────────────────────────────────────────────────────────

@router.post("/api/email-templates/{key}/test-send")
def test_send_template(key: str, user=Depends(_require_template_access)):
    """
    Render template with sample data and send to the current user's email.
    Works for both built-in and custom templates.
    """
    user_row = query_one("SELECT email, full_name FROM app_user WHERE id = %s", [user["sub"]])
    if not user_row:
        raise HTTPException(404, "User not found")

    # For custom templates not in DEFAULTS, render manually
    if key not in DEFAULTS:
        row = query_one(
            "SELECT name, subject, body FROM email_template WHERE template_key = %s AND is_active = TRUE LIMIT 1",
            [key],
        )
        if not row:
            raise HTTPException(404, f"Template '{key}' not found.")
        subj = _PH_RE.sub(lambda m: str(SAMPLE_VALUES.get(m.group(1), f"{{{{{m.group(1)}}}}}")), row["subject"])
        body = _PH_RE.sub(lambda m: str(SAMPLE_VALUES.get(m.group(1), f"{{{{{m.group(1)}}}}}")), row["body"])
    else:
        try:
            subj, body = render_template(key, SAMPLE_VALUES)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    try:
        send_email(user_row["email"], f"[TEST] {subj}", body, tenant_id=user.get("tenant_id"))
    except Exception as exc:
        raise HTTPException(500, f"Email delivery failed: {exc}")

    return {"ok": True, "sent_to": user_row["email"]}


# ── Manual send to candidate ──────────────────────────────────────────────────

def _send_template_email_to_application(app_id: str, key: str, sender_user_id: str, notes: Optional[str] = None) -> dict:
    """
    Shared by the single-candidate and bulk send endpoints: fills
    {{candidate_name}}, {{job_title}}, {{company_name}}, {{recruiter_name}}
    plus the requisition-derived JD-template fields ({{location}},
    {{experience}}, {{qualification}}, {{about_company}}, {{jd_body}}) from
    the application record, blocks if any other placeholder can't be
    filled, sends, and logs to sent_email_log. Raises HTTPException on any
    failure — callers doing a bulk send catch per-id so one bad id doesn't
    abort the rest of the batch.
    """
    # Fetch application + candidate + job + recruiter data
    app_row = query_one(
        """SELECT
               a.id,
               a.requisition_id,
               c.full_name  AS candidate_name,
               c.email      AS candidate_email,
               r.title      AS job_title,
               r.tenant_id  AS tenant_id,
               gc.name      AS company_name,
               u.full_name  AS recruiter_name
           FROM application a
           JOIN candidate   c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           LEFT JOIN business_unit bu ON bu.id = r.bu_id
           LEFT JOIN group_company gc ON gc.id = bu.company_id
           LEFT JOIN LATERAL (
               SELECT rr.recruiter_id
               FROM requisition_recruiter rr
               WHERE rr.requisition_id = r.id
               ORDER BY rr.is_owner DESC NULLS LAST, rr.assigned_at
               LIMIT 1
           ) rec ON true
           LEFT JOIN app_user u ON u.id = rec.recruiter_id
           WHERE a.id = %s""",
        [app_id],
    )
    if not app_row:
        raise HTTPException(404, "Application not found.")

    candidate_email = app_row["candidate_email"]
    if not candidate_email:
        raise HTTPException(422, "Candidate has no email address on file.")

    values = {
        "candidate_name": app_row["candidate_name"] or "Candidate",
        "job_title":      app_row["job_title"]      or "the role",
        "company_name":   app_row["company_name"]   or "our company",
        "recruiter_name": app_row["recruiter_name"] or "the recruiting team",
    }
    # Templates like "Application Received — Job Description" also use
    # requisition-derived fields the base application/candidate query above
    # doesn't cover — resolve them the same way the automatic JD email does
    # so picking that template manually doesn't fail with "cannot fill
    # placeholder" for fields this endpoint simply never populated.
    from ..services.jd_email import resolve_jd_placeholders, strip_empty_placeholder_lines
    jd_values = resolve_jd_placeholders(app_row["requisition_id"]) or {}
    jd_values.pop("job_title", None)  # already set above from the same requisition
    values.update(jd_values)

    # Fetch and render the template
    tmpl_row = query_one(
        "SELECT name, subject, body FROM email_template WHERE template_key = %s AND is_active = TRUE LIMIT 1",
        [key],
    )
    if not tmpl_row:
        raise HTTPException(404, f"Template '{key}' not found.")

    subject = tmpl_row["subject"] or ""
    body    = tmpl_row["body"]    or ""

    # An empty jd_body (requisition has no JD text yet) shouldn't block the
    # send — drop that line rather than requiring a value for it.
    if not values.get("jd_body"):
        body = strip_empty_placeholder_lines(body, "jd_body")
        subject = strip_empty_placeholder_lines(subject, "jd_body")
        values["jd_body"] = ""

    # Check for unfillable placeholders
    all_ph = set(_PH_RE.findall(subject)) | set(_PH_RE.findall(body))
    missing = [p for p in all_ph if not values.get(p)]
    if missing:
        raise HTTPException(
            422,
            f"Cannot fill placeholder(s): {', '.join(sorted(missing))}. "
            "Update the template to use only: candidate_name, job_title, company_name, "
            "recruiter_name, location, experience, qualification, about_company, jd_body."
        )

    rendered_subject = _PH_RE.sub(lambda m: str(values[m.group(1)]), subject)
    rendered_body    = _PH_RE.sub(lambda m: str(values[m.group(1)]), body)

    import html as _html
    from ..services.email_layout import build_branded_email
    html_body = build_branded_email(
        eyebrow="Application Tracking System",
        hero_title_html="A Message<br>For You.",
        hero_subtitle=f"Hi {_html.escape(values['candidate_name'])}, here's an update from {_html.escape(values['company_name'])}.",
        hero_footer_label=values["job_title"], hero_footer_value=values["company_name"],
        detail_cells=[
            ("Candidate", values["candidate_name"]), ("Position", values["job_title"]),
            ("Company", values["company_name"]), ("Recruiter", values["recruiter_name"]),
        ],
        about_text=rendered_body,
        about_heading=None,
        cta_label=None, cta_link=None,
    )

    # Send
    try:
        send_email(candidate_email, rendered_subject, rendered_body, html=html_body, tenant_id=app_row.get("tenant_id"))
    except Exception as exc:
        raise HTTPException(500, f"Email delivery failed: {exc}")

    # Log
    try:
        query(
            """INSERT INTO sent_email_log
               (application_id, template_key, template_name, sent_to_email, sent_by, subject, notes)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [app_id, key, tmpl_row["name"], candidate_email, sender_user_id,
             rendered_subject, notes or None],
            fetch=False,
        )
    except Exception as exc:
        print(f"[email_template_api] send log failed (non-fatal): {exc}")

    return {
        "ok":       True,
        "sent_to":  candidate_email,
        "subject":  rendered_subject,
    }


@router.post("/api/applications/{app_id}/send-email")
def send_email_to_candidate(app_id: str, payload: SendEmailPayload, user=Depends(_require_send_access)):
    """Send a custom email template to the candidate on this application."""
    return _send_template_email_to_application(app_id, payload.template_key, user["sub"], payload.notes)


class BulkSendEmailPayload(BaseModel):
    app_ids: list[str]
    template_key: str
    notes: Optional[str] = None


@router.post("/api/applications/bulk-send-email")
def bulk_send_email_to_candidates(payload: BulkSendEmailPayload, user=Depends(_require_send_access)):
    """
    Send the same template to many applications' candidates in one call —
    e.g. mailing an entire selected batch from the Candidates list. Each id
    runs through the same per-application render/send as the single-send
    endpoint; one bad id (no email on file, unfillable placeholder, etc.)
    doesn't abort the rest of the batch.
    """
    if not payload.app_ids:
        raise HTTPException(400, "No app_ids provided")
    if len(payload.app_ids) > 500:
        raise HTTPException(400, "Too many recipients in one batch (max 500)")

    sent, failed = [], []
    for app_id in payload.app_ids:
        try:
            result = _send_template_email_to_application(app_id, payload.template_key, user["sub"], payload.notes)
            sent.append({"app_id": app_id, **result})
        except HTTPException as exc:
            failed.append({"app_id": app_id, "error": exc.detail})

    return {"sent": len(sent), "failed": failed}
