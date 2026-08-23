"""
"Application Received — Job Description" confirmation email.

Shared by every path that turns a person into a real applicant on a
requisition (career-site apply, and CV Repository pool → requisition
mapping) so a candidate gets the same JD confirmation regardless of how
they entered the pipeline.
"""
import re
from typing import Optional

from ..db import query_one


def resolve_jd_placeholders(req_id: str) -> Optional[dict]:
    """
    The requisition-derived {{location}}/{{experience}}/{{qualification}}/
    {{about_company}}/{{jd_body}} placeholders used by the "Application
    Received — Job Description" template family. Shared by the automatic
    send below AND the manual per-candidate / bulk send endpoints in
    email_template_api.py, so a recruiter manually picking this template
    fills the same fields the automatic email does instead of hitting
    "cannot fill placeholder" for fields it never knew about.

    Returns None if the requisition doesn't exist.
    """
    req = query_one(
        """SELECT title, hiring_location, min_experience, max_experience, job_description
           FROM requisition WHERE id=%s""",
        [req_id],
    )
    if not req:
        return None

    about_row = query_one("SELECT value FROM system_settings WHERE key='about_company_text'", [])
    about_company = (about_row or {}).get("value") or ""

    min_exp, max_exp = req.get("min_experience"), req.get("max_experience")
    if min_exp is not None and max_exp is not None:
        experience = f"{int(min_exp)}–{int(max_exp)} years"
    elif min_exp is not None:
        experience = f"{int(min_exp)}+ years"
    else:
        experience = "Not specified"

    return {
        "job_title":     req["title"] or "",
        "location":      req.get("hiring_location") or "India",
        "experience":    experience,
        "qualification": "As per role requirements",
        "about_company": about_company,
        "jd_body":       (req.get("job_description") or "").strip(),
    }


def strip_empty_placeholder_lines(text: str, placeholder: str) -> str:
    """Drop any line containing an unfilled {{placeholder}} rather than
    sending it blank — used for optional multi-line fields like jd_body."""
    token = "{{" + placeholder + "}}"
    return "\n".join(ln for ln in text.splitlines() if token not in ln)


def send_application_received_jd_email(candidate_name: str, candidate_email: str, req_id: str) -> None:
    """
    Send the application_received_jd confirmation email.
    Failure is logged but never raises — must not fail the application.
    Respects the 'auto_jd_email' system setting toggle.
    """
    try:
        toggle_row = query_one(
            "SELECT value FROM system_settings WHERE key='auto_jd_email'", []
        )
        if toggle_row and (toggle_row.get("value") or "true").lower() not in ("true", "1", "yes"):
            return

        req = query_one(
            """SELECT title, hiring_location, min_experience, max_experience,
                      job_description, key_skills
               FROM requisition WHERE id=%s""",
            [req_id],
        )
        if not req:
            return

        about_row = query_one(
            "SELECT value FROM system_settings WHERE key='about_company_text'", []
        )
        about_company = (about_row or {}).get("value") or ""

        # Build human-readable experience string
        min_exp = req.get("min_experience")
        max_exp = req.get("max_experience")
        if min_exp is not None and max_exp is not None:
            experience = f"{int(min_exp)}–{int(max_exp)} years"
        elif min_exp is not None:
            experience = f"{int(min_exp)}+ years"
        else:
            experience = "Not specified"

        jd_raw = (req.get("job_description") or "").strip()

        from .email_templates import get_template
        from .connectors import send_email, resolve_global_placeholders

        globals_ = resolve_global_placeholders(req_id=req_id)
        reply_to = globals_.get("recruiter_email") or None

        tmpl = get_template("application_received_jd")
        subject = tmpl["subject"]
        body    = tmpl["body"]

        # Substitute placeholders, skipping jd_body gracefully if empty
        subs = {
            "candidate_name": candidate_name or "Candidate",
            "job_title":      req["title"] or "",
            "location":       req.get("hiring_location") or "India",
            "experience":     experience,
            "qualification":  "As per role requirements",
            "about_company":  about_company,
            **globals_,  # company_name, recruiter_name, recruiter_email
        }
        if jd_raw:
            subs["jd_body"] = jd_raw
        else:
            # Remove the jd_body line from body rather than sending a blank placeholder
            body = "\n".join(
                ln for ln in body.splitlines()
                if "{{jd_body}}" not in ln
            )
            subs["jd_body"] = ""  # won't appear after line removal

        for k, v in subs.items():
            subject = subject.replace("{{" + k + "}}", str(v))
            body    = body.replace("{{" + k + "}}", str(v))

        # Defensive sweep: strip any remaining unresolved {{placeholders}} so
        # raw template tokens never appear in a sent email.
        subject = re.sub(r'\{\{[^}]+\}\}', '', subject)
        body    = re.sub(r'\{\{[^}]+\}\}', '', body)

        import html
        from .email_layout import build_branded_email
        html_body = build_branded_email(
            eyebrow="Application Tracking System",
            hero_title_html="Application<br>Received!",
            hero_subtitle=f"Hi {html.escape(candidate_name or 'there')}, thank you for applying — your application has been submitted successfully.",
            hero_footer_label=req["title"], hero_footer_value=subs.get("company_name"),
            detail_cells=[
                ("Candidate", candidate_name or "Candidate"), ("Position", req["title"] or ""),
                ("Location", subs["location"]), ("Experience", subs["experience"]),
            ],
            about_text=body,
            about_heading=None,
            cta_label=None, cta_link=None,
        )

        send_email(candidate_email, subject, body, html=html_body, reply_to=reply_to)
    except Exception as exc:
        print(f"[jd-email] Failed to send JD confirmation to {candidate_email}: {exc}")
        try:
            from .activity_log import log_activity
            log_activity(
                "candidate", "jd_confirmation_email_failed",
                requisition_id=req_id, actor_id=None, actor_role="system",
                detail={"candidate_email": candidate_email, "error": str(exc)},
            )
        except Exception:
            pass
