"""
Enternly -- Calendly-style hiring-manager self-scheduling for panel interviews.

Flow: a recruiter (or an auto-triggered "Panel + Auto" round, see
pipeline_api.advance_application) opens an interview_schedule_request ->
the HM proposes 3-6 slots on a 2-month grid -> the candidate confirms one via
a public tokenised link (same pattern as nexai_invite) -> both the candidate
and the HM get an ICS invite over SMTP with the candidate's CV attached.

No Google Calendar / OAuth here -- reuses connectors.schedule_meeting(), the
existing ICS-over-SMTP sender.
"""
import html
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import query, query_one, transaction, tx_exec
from ..auth_utils import get_current_user
from ..services import connectors
from ..services import google_calendar
from ..services.activity_log import log_activity
from ..services.notifications import notify, notify_tx
from ..services.email_layout import build_branded_email
from .nexai_api import _get_base_url, _is_localhost, _recruiter_owns_req, _application_req_id

router = APIRouter(prefix="/api/scheduling", tags=["scheduling"])

_ACTIVE_STATUSES = ("awaiting_hm", "awaiting_candidate")
_MIN_SLOTS = 3
_MAX_SLOTS = 6
_MAX_SLOT_DAYS_OUT = 62  # ~2 months


# ─── shared context loader ────────────────────────────────────────────────────

def _app_context(application_id: str) -> Optional[dict]:
    return query_one(
        """SELECT a.id AS application_id, a.requisition_id, a.current_round,
                  c.id AS candidate_id, c.full_name AS candidate_name, c.email AS candidate_email,
                  r.title AS job_title, r.hiring_manager_id,
                  gc.name AS company
           FROM application a
           JOIN candidate   c  ON c.id = a.candidate_id
           JOIN requisition r  ON r.id = a.requisition_id
           JOIN business_unit bu ON bu.id = r.bu_id
           JOIN group_company gc ON gc.id = bu.company_id
           WHERE a.id = %s""",
        [application_id],
    )


def _request_row(request_id: str) -> Optional[dict]:
    return query_one("SELECT * FROM interview_schedule_request WHERE id = %s", [request_id])


def _owning_recruiter_id(requisition_id: str) -> Optional[str]:
    """The primary (is_owner=true) recruiter for a requisition, if any --
    used to route notifications. requisition_recruiter has no DB-level
    uniqueness guarantee on is_owner; picking one deterministically (lowest
    assigned_at) is a reasonable degradation if that invariant is ever
    violated by legacy data."""
    row = query_one(
        """SELECT recruiter_id FROM requisition_recruiter
           WHERE requisition_id = %s AND is_owner = true
           ORDER BY assigned_at ASC LIMIT 1""",
        [requisition_id],
    )
    return row and str(row["recruiter_id"])


def _reply_to_recruiter_and_hr(requisition_id: Optional[str]) -> Optional[str]:
    """Reply-To for outbound scheduling emails: the requisition's owning
    recruiter (so a reply reaches a real person, not just "no-reply") plus
    hr@amnex.com (so the shared TA inbox always sees it too). RFC 5322 allows
    a comma-separated address list in a single Reply-To header."""
    recruiter_email = None
    recruiter_id = requisition_id and _owning_recruiter_id(requisition_id)
    if recruiter_id:
        row = query_one("SELECT email FROM app_user WHERE id = %s", [recruiter_id])
        recruiter_email = row and row.get("email")
    addrs = [a for a in (recruiter_email, "hr@amnex.com") if a]
    return ", ".join(addrs) if addrs else None


def _lazy_expire(req: dict) -> dict:
    """A request stuck at awaiting_candidate past every slot's start time is dead.
    Checked on read rather than via a cron -- see BUILD notes in the spec."""
    if req["status"] == "awaiting_candidate":
        latest = query_one(
            "SELECT MAX(start_utc) AS last_slot FROM interview_slot WHERE request_id = %s AND status = 'open'",
            [req["id"]],
        )
        last_slot = latest and latest.get("last_slot")
        if last_slot and last_slot < datetime.now(timezone.utc):
            query(
                "UPDATE interview_schedule_request SET status = 'expired' WHERE id = %s AND status = 'awaiting_candidate'",
                [req["id"]], fetch=False,
            )
            req = dict(req)
            req["status"] = "expired"
    return req


# ─── HM availability-request email ────────────────────────────────────────────

def _esc(s) -> str:
    return html.escape(str(s or ""))


def _build_hm_availability_html(
    hm_name: str, candidate_name: str, job_title: str, company: str,
    duration_min: int, link: str,
) -> str:
    hm_first = (hm_name or "").split(" ")[0] or "there"
    return build_branded_email(
        eyebrow="Application Tracking System",
        hero_title_html="Your Action<br>is Needed.",
        hero_subtitle=f"Hi {_esc(hm_first)}, a candidate has cleared screening and is ready for their panel interview.",
        hero_footer_label=job_title, hero_footer_value=company,
        detail_cells=[
            ("Candidate", candidate_name), ("Interview Type", "Panel"),
            ("Duration", f"{int(duration_min or 45)} Minutes"), ("Slot Window", "Next 2 Months"),
        ],
        steps=[
            ("Share Your Availability", "Pick 3-6 open slots over the next two months on your grid.", "current"),
            ("Candidate Books a Slot", f"{candidate_name} chooses one of your open times.", "pending"),
            ("Calendar Invite Sent", "You and the candidate both get an ICS invite with the CV attached.", "pending"),
        ],
        about_text=(
            "Sign in to open your availability grid and select the times that work for you — "
            "no back-and-forth email needed. The candidate will see only what you mark open and "
            "confirm a slot themselves."
        ),
        cta_label="Open My Availability Grid", cta_link=link,
    )


def _send_candidate_pick_email(req_row: dict, ctx: dict, candidate_link: str) -> bool:
    """Returns whether the candidate was actually emailed the slot-pick link --
    mirrors _send_hm_request_email's contract so callers surface a warning
    (not a plain success) when this is False."""
    if not (ctx and ctx.get("candidate_email")):
        return False
    subject = f"Pick your interview slot — {ctx['job_title']}"
    body_txt = (
        f"Hi {ctx['candidate_name']},\n\n"
        f"Please choose an interview slot for {ctx['job_title']} at {ctx.get('company')}:\n"
        f"{candidate_link}\n\n"
        f"— EnternsTech Talent Acquisition"
    )
    candidate_first = (ctx.get("candidate_name") or "").split(" ")[0] or "there"
    html_body = build_branded_email(
        eyebrow="Application Tracking System",
        hero_title_html="Pick Your<br>Interview Slot.",
        hero_subtitle=f"Hi {_esc(candidate_first)}, the hiring team is ready to meet you — choose a time that works.",
        hero_footer_label=ctx["job_title"], hero_footer_value=ctx.get("company"),
        detail_cells=[
            ("Candidate", ctx["candidate_name"]), ("Position", ctx["job_title"]),
            ("Duration", f"{int(req_row.get('duration_min') or 45)} Minutes"), ("Format", "Panel Interview"),
        ],
        cta_label="Choose My Slot", cta_link=candidate_link,
    )
    try:
        connectors.send_email(
            ctx["candidate_email"], subject, body_txt, html=html_body,
            reply_to=_reply_to_recruiter_and_hr(ctx.get("requisition_id")),
        )
        return True
    except Exception as exc:
        print(f"[scheduling] Failed to email candidate: {exc}")
        return False


def _send_hm_request_email(req_id: str, hm_user_id: str, ctx: dict) -> bool:
    """Returns whether the HM was actually emailed -- callers must surface a
    warning (not a success toast) when this is False; the schedule request
    row itself is never rolled back over a failed/missing email."""
    hm = query_one("SELECT email, full_name FROM app_user WHERE id = %s", [hm_user_id])
    if not hm or not hm.get("email"):
        print(f"[scheduling] HM {hm_user_id} has no email — cannot notify for request {req_id}")
        return False
    base_url, _src = _get_base_url()
    if _is_localhost(base_url):
        print(f"[scheduling] WARNING: base URL is localhost ({base_url}) — HM link will be broken in prod")
    link = f"{base_url}/?openScheduling={req_id}"
    subject = f"Please share your availability — {ctx['candidate_name']} for {ctx['job_title']}"
    req_row = _request_row(req_id) or {}
    body = (
        f"Hi {hm.get('full_name') or ''},\n\n"
        f"{ctx['candidate_name']} is ready for a panel interview for {ctx['job_title']}.\n"
        f"Please pick 3-6 open slots over the next two months so the candidate can book one.\n\n"
        f"Sign in and open your availability grid here:\n{link}\n\n"
        f"— Enternly"
    )
    html_body = _build_hm_availability_html(
        hm_name=hm.get("full_name"),
        candidate_name=ctx["candidate_name"],
        job_title=ctx["job_title"],
        company=ctx.get("company"),
        duration_min=req_row.get("duration_min"),
        link=link,
    )
    try:
        connectors.send_email(
            hm["email"], subject, body, html=html_body,
            reply_to=_reply_to_recruiter_and_hr(ctx.get("requisition_id")),
        )
        return True
    except Exception as exc:
        print(f"[scheduling] Failed to email HM {hm['email']}: {exc}")
        return False


# ─── booking-confirmation emails (candidate + HM), sent once the candidate ────
# actually confirms a slot in confirm_pick() below. These ride inside the same
# calendar-invite send as the .ics (send_calendar_invite's html_body param) so
# the branded content and the "this is a real calendar invite" MIME part are
# one email, not two competing ones.

_WHY_ENTERNSTECH_BULLETS = [
    "1,000+ innovators building technology with real world impact",
    "200+ transformative projects across critical industries",
    "Opportunities to work on AI, Geospatial, IoT and Digital Public Infrastructure",
    "A culture built on innovation, ownership and continuous learning",
]
_ENTERNSTECH_VIDEOS = [
    ("EnternsTech Intro", "https://www.youtube.com/watch?v=HD63NAQ01qU&list=PLt962kUB1I0rWjdCGly0g860TNPh25QvC&index=6"),
    ("Life at EnternsTech", "https://www.youtube.com/watch?v=7hquG1pbnG8&list=PLt962kUB1I0rWjdCGly0g860TNPh25QvC&index=6"),
    ("Innovation at EnternsTech (Sarjaan)", "https://www.youtube.com/watch?v=-R7iECCafqY"),
]


def _reschedule_link_html(reschedule_link: Optional[str]) -> str:
    """Small secondary text link shared by both confirmation emails --
    "Join Interview" stays the primary button; this is for the "I can't make
    it" case, pointing at the public self-service reschedule page."""
    if not reschedule_link:
        return ""
    return f"""
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:28px">
    <tr><td align="center" style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#6b7280">
      Can't make it? <a href="{_esc(reschedule_link)}" style="color:#1e63f2;font-weight:600;text-decoration:none">Request a reschedule &rarr;</a>
    </td></tr>
  </table>"""


def _build_candidate_confirmation_html(
    candidate_name: str, job_title: str, company: str,
    date_str: str, time_str: str, duration_min: int, meet_link: str,
    reschedule_link: Optional[str] = None,
) -> str:
    cand_first = (candidate_name or "").split(" ")[0] or "there"

    join_cta_html = ""
    if meet_link:
        join_cta_html = f"""
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:34px">
    <tr><td align="center">
      <table cellpadding="0" cellspacing="0" border="0"><tr>
        <td bgcolor="#1e63f2" style="background-color:#1e63f2;border-radius:10px">
          <a href="{_esc(meet_link)}" style="display:block;color:#ffffff;padding:15px 40px;text-decoration:none;font-size:15px;font-weight:700;font-family:Arial,Helvetica,sans-serif;letter-spacing:0.3px;border-radius:10px">Join Interview</a>
        </td>
      </tr></table>
    </td></tr>
  </table>"""

    bullets_html = "".join(
        f"""<tr>
              <td width="18" valign="top" style="padding-top:2px"><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background-color:#1e63f2;font-size:0;line-height:0">&nbsp;</span></td>
              <td style="padding-bottom:14px;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#374151">{_esc(b)}</td>
            </tr>""" for b in _WHY_ENTERNSTECH_BULLETS
    )
    videos_html = "".join(
        f"""<tr>
              <td style="padding:9px 0;border-top:1px solid #e7ebf4">
                <a href="{_esc(url)}" style="font-family:Arial,Helvetica,sans-serif;font-size:13.5px;color:#1e63f2;text-decoration:none;font-weight:600">&#9654; {_esc(label)}</a>
              </td>
            </tr>""" for label, url in _ENTERNSTECH_VIDEOS
    )

    extra_body_html = f"""
  {join_cta_html}
  {_reschedule_link_html(reschedule_link)}
  <p style="font-size:20px;font-weight:700;color:#111827;font-family:Arial,Helvetica,sans-serif;margin:0 0 20px 0">About EnternsTech</p>
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:30px">
    <tr><td style="font-size:14px;line-height:1.8;color:#4b5563;font-family:Arial,Helvetica,sans-serif">
      At EnternsTech, we don't just build technology &mdash; we build solutions that solve real world challenges at scale.
      Guided by our belief that Intelligence is Natural, we bring together data, AI, geospatial intelligence and
      connected technologies to transform how cities move, farms grow, infrastructure performs and governments
      serve their citizens. Founded in 2008 and headquartered in Ahmedabad, we have grown into a team of over
      1,000 professionals.<br><br>
      Today, we are delivering 200+ transformative projects across Mobility, Agriculture, Traffic and Highways,
      Urban Solutions, Resources and Utilities, and Governance. Every day, our technologies enable over 8.7 million
      public transport journeys, support 19.2 million farmers, and improve the lives of 39 million plus citizens
      through smarter public infrastructure and digital transformation.<br><br>
      What truly makes EnternsTech special, though, is our people. We are a team of innovators, builders and lifelong
      learners who believe meaningful work comes from solving meaningful problems. As a Great Place to Work&reg;
      Certified organisation, we take that belief seriously, building a culture rooted in collaboration, ownership
      and continuous learning.
    </td></tr>
  </table>

  <p style="font-size:20px;font-weight:700;color:#111827;font-family:Arial,Helvetica,sans-serif;margin:0 0 20px 0">Why Professionals Choose EnternsTech</p>
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:30px">
    {bullets_html}
  </table>

  <p style="font-size:20px;font-weight:700;color:#111827;font-family:Arial,Helvetica,sans-serif;margin:0 0 16px 0">A Glimpse Into EnternsTech, Before We Meet</p>
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:30px">
    {videos_html}
  </table>

  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f8faff;border-radius:12px;margin-bottom:8px">
    <tr><td style="padding:22px;font-size:14px;line-height:1.8;color:#4b5563;font-family:Arial,Helvetica,sans-serif">
      We would love to learn more about your journey and aspirations, and explore how you could contribute to
      building intelligent solutions that create lasting impact.<br><br>
      A calendar invite for this slot is attached to this email &mdash; if anything changes on your end, or you have
      any questions before we meet, just reply here and our team will help.
    </td></tr>
  </table>
"""

    cells = [("Position", job_title), ("Date", date_str), ("Time", time_str), ("Duration", f"{duration_min} Minutes")]
    cells.append(("Mode", "Virtual (Google Meet)") if meet_link else ("Mode", "To be confirmed"))

    return build_branded_email(
        eyebrow="Application Tracking System",
        hero_title_html="You're Invited<br>to Interview.",
        hero_subtitle=f"Hi {_esc(cand_first)}, thank you for your interest in joining EnternsTech — we're delighted to invite you to interview for {_esc(job_title)}.",
        hero_footer_label=job_title, hero_footer_value=company,
        detail_cells=cells,
        extra_body_html=extra_body_html,
        footer_note="Questions before we meet? Simply reply to this email and our talent acquisition team will help.",
    )


def _build_hm_confirmation_html(
    hm_name: str, candidate_name: str, job_title: str, company: str,
    date_str: str, time_str: str, duration_min: int, meet_link: str,
    reschedule_link: Optional[str] = None,
) -> str:
    hm_first = (hm_name or "").split(" ")[0] or "there"
    cells = [
        ("Candidate", candidate_name), ("Position", job_title),
        ("Date", date_str), ("Time", time_str),
        ("Duration", f"{duration_min} Minutes"),
    ]
    cells.append(("Mode", "Virtual (Google Meet)") if meet_link else ("Mode", "To be confirmed"))

    return build_branded_email(
        eyebrow="Application Tracking System",
        hero_title_html="Interview<br>Confirmed.",
        hero_subtitle=f"Hi {_esc(hm_first)}, {_esc(candidate_name)} has confirmed a time for their panel interview.",
        hero_footer_label=job_title, hero_footer_value=company,
        detail_cells=cells,
        steps=[
            ("Interview Scheduled", "Both sides have confirmed — a calendar invite with the candidate's CV is attached to this email.", "done"),
            ("Conduct the Interview", "Meet the candidate at the scheduled time above.", "current"),
            ("Submit Your Scorecard", "Share your feedback in the ATS right after the interview.", "pending"),
        ],
        about_text=(
            "The candidate's resume is attached to this email alongside the calendar invite, so you "
            "have it on hand ahead of the conversation. If you can't make it, use the reschedule link "
            "below to pick a new time yourself — everyone will be re-notified automatically."
        ),
        extra_body_html=_reschedule_link_html(reschedule_link),
        cta_label=("Join Interview" if meet_link else None),
        cta_link=(meet_link if meet_link else None),
        footer_note="Questions? Simply reply to this email and our talent acquisition team will help.",
    )


# ─── shared creator — used by the manual endpoint AND the advance-application hook ──
#
# Split in two so advance_application (pipeline_api.py) can insert the request
# on the SAME transaction cursor as its stage-move UPDATE: if the insert fails,
# the whole transaction (including the stage move) rolls back instead of
# leaving a candidate parked in an interview stage with no request. The
# standalone create_schedule_request() below (used by POST /request) just
# wraps the tx helper in its own transaction() and keeps the old signature.

def _insert_schedule_request_tx(
    cur,
    application_id: str,
    ctx: dict,
    round_config_id: Optional[str],
    hm_user_id: Optional[str],
    duration_min: int,
    meeting_link: Optional[str],
    created_by: Optional[str],
) -> dict:
    existing = tx_exec(
        cur,
        """SELECT * FROM interview_schedule_request
           WHERE application_id = %s AND status = ANY(%s)
           ORDER BY created_at DESC LIMIT 1""",
        [application_id, list(_ACTIVE_STATUSES)],
    )
    if existing:
        return {**dict(existing[0]), "reused": True}

    # interview.round_config_id is NOT NULL — resolve it now so confirm_pick()
    # never fails on the INSERT after the candidate has already committed to a slot.
    if round_config_id is None:
        rc = tx_exec(
            cur,
            "SELECT id FROM round_config WHERE requisition_id = %s ORDER BY sequence LIMIT 1",
            [ctx["requisition_id"]],
        )
        round_config_id = rc and rc[0]["id"]
    if round_config_id is None:
        raise HTTPException(400, "This requisition has no interview rounds configured")

    resolved_hm = hm_user_id
    if resolved_hm is None:
        # The designated availability-proposer is drawn from the ROUND's panel
        # roster (round_config.panelist_emails) first -- a panelist can also
        # hold the HM title, but the HM isn't auto-treated as a panelist just
        # by holding that title (see confirm_pick's interview_panel insert
        # below, which only adds this person as a real panelist if their
        # email is actually on the roster). Picks the first roster email with
        # a matching active ATS account; a recruiter can reassign to any
        # other panelist afterwards via PATCH /request/{id}/assign-hm.
        rc_panel = tx_exec(cur, "SELECT panelist_emails FROM round_config WHERE id = %s", [round_config_id])
        roster = list((rc_panel[0]["panelist_emails"] if rc_panel else None) or [])
        for email in roster:
            pu = tx_exec(
                cur, "SELECT id FROM app_user WHERE LOWER(email) = LOWER(%s) AND is_active = TRUE", [email]
            )
            if pu:
                resolved_hm = str(pu[0]["id"])
                break
    if resolved_hm is None:
        # Last resort: the requisition's fixed hiring_manager_id -- keeps
        # scheduling working for every requisition that predates a real
        # per-round panel roster, instead of hard-blocking the whole fleet
        # until each one is migrated.
        resolved_hm = ctx.get("hiring_manager_id")
    if resolved_hm is None:
        # Never invent/guess who proposes availability -- a wrong pick exposes
        # candidate data to the wrong person. Block scheduling server-side
        # (rolls back the caller's stage-move transaction too) until a
        # recruiter adds panelists to this round or assigns a Hiring Manager.
        raise HTTPException(422, "No panelist or Hiring Manager is configured for this round/requisition -- add one before scheduling interviews.")

    # A round-level default Google Meet link (set once by the recruiter in the
    # round setup, alongside panelist emails) covers the Auto flow -- which has
    # no recruiter interaction at schedule-time to grab a link from otherwise --
    # as well as Manual requests where the caller didn't pass one explicitly.
    resolved_meeting_link = meeting_link
    if not resolved_meeting_link:
        rc_row = tx_exec(cur, "SELECT meeting_link FROM round_config WHERE id = %s", [round_config_id])
        resolved_meeting_link = rc_row and rc_row[0].get("meeting_link")

    rows = tx_exec(
        cur,
        """INSERT INTO interview_schedule_request
             (application_id, round_config_id, hm_user_id, duration_min, meeting_link, created_by)
           VALUES (%s,%s,%s,%s,%s,%s)
           RETURNING *""",
        [application_id, round_config_id, resolved_hm, duration_min, resolved_meeting_link, created_by],
    )
    return {**dict(rows[0]), "reused": False, "resolved_hm": resolved_hm}


def _after_insert_side_effects(row: dict, ctx: dict, application_id: str, created_by: Optional[str]) -> bool:
    """Email + activity log — run AFTER the DB transaction that inserted `row`
    has committed, so a mail failure never rolls back a created request.
    Returns whether the HM was actually notified -- callers must surface a
    warning (not a plain success) when this is False."""
    if row["reused"]:
        return True
    resolved_hm = row.get("resolved_hm")
    # resolved_hm can no longer be None here for a fresh row -- the
    # panelist-required gate in _insert_schedule_request_tx rejects the
    # insert before this runs. The `else` print stays only as a defensive
    # trace for that invariant.
    notified = False
    if resolved_hm:
        notified = _send_hm_request_email(str(row["id"]), str(resolved_hm), ctx)
        notify(
            resolved_hm, "hm_availability_requested",
            f"Share your availability for {ctx['candidate_name']}",
            body=f"{ctx['candidate_name']} is ready for a panel interview for {ctx['job_title']}.",
            action_url=f"/?openScheduling={row['id']}",
            is_actionable=True,
            requisition_id=ctx.get("requisition_id"), application_id=application_id,
            interview_request_id=row["id"],
        )
    else:
        print(f"[scheduling] Request {row['id']} created with no panelist resolvable — "
              f"needs manual assignment (round has no panelist with an ATS account)")
    log_activity(
        "interview", "interview_schedule_requested",
        entity_id=row["id"], application_id=application_id, requisition_id=ctx.get("requisition_id"),
        actor_id=created_by, actor_role=None if created_by else "system",
        detail={"hm_user_id": resolved_hm and str(resolved_hm), "hm_notified": notified},
    )
    return notified


def create_schedule_request_tx(
    cur,
    application_id: str,
    round_config_id: Optional[str] = None,
    hm_user_id: Optional[str] = None,
    duration_min: int = 45,
    meeting_link: Optional[str] = None,
    created_by: Optional[str] = None,
) -> tuple:
    """For callers that already hold an open transaction() cursor (e.g.
    advance_application's stage-move transaction). Returns (row, ctx) — the
    caller must invoke _after_insert_side_effects(row, ctx, ...) itself AFTER
    its transaction commits."""
    ctx = _app_context(application_id)
    if not ctx:
        raise HTTPException(404, "Application not found")
    row = _insert_schedule_request_tx(
        cur, application_id, ctx, round_config_id, hm_user_id, duration_min, meeting_link, created_by,
    )
    return row, ctx


def create_schedule_request(
    application_id: str,
    round_config_id: Optional[str] = None,
    hm_user_id: Optional[str] = None,
    duration_min: int = 45,
    meeting_link: Optional[str] = None,
    created_by: Optional[str] = None,
) -> dict:
    ctx = _app_context(application_id)
    if not ctx:
        raise HTTPException(404, "Application not found")
    with transaction() as cur:
        row = _insert_schedule_request_tx(
            cur, application_id, ctx, round_config_id, hm_user_id, duration_min, meeting_link, created_by,
        )
    hm_notified = _after_insert_side_effects(row, ctx, application_id, created_by)
    # hm_notified=False means the request/booking record is real but the HM
    # never got the email -- caller must show a warning, not a plain success.
    return {**row, "hm_notified": hm_notified}


# ─── Recruiter/TA/Admin: initiate ─────────────────────────────────────────────

class RequestIn(BaseModel):
    application_id: str
    round_index: Optional[int] = None
    hm_user_id: Optional[str] = None
    duration_min: int = 45
    meeting_link: Optional[str] = None


@router.post("/request")
def create_request(body: RequestIn, user: dict = Depends(get_current_user)):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised to request interview availability")

    ctx = _app_context(body.application_id)
    if not ctx:
        raise HTTPException(404, "Application not found")
    if user["role"] == "recruiter" and not _recruiter_owns_req(user, ctx["requisition_id"]):
        raise HTTPException(404, "Application not found")

    round_config_id = None
    seq = body.round_index if body.round_index is not None else ctx.get("current_round")
    if seq:
        rc = query_one(
            "SELECT id FROM round_config WHERE requisition_id = %s AND sequence = %s",
            [ctx["requisition_id"], seq],
        )
        round_config_id = rc and rc["id"]

    result = create_schedule_request(
        application_id=body.application_id,
        round_config_id=round_config_id,
        hm_user_id=body.hm_user_id,
        duration_min=body.duration_min,
        meeting_link=body.meeting_link,
        created_by=user["sub"],
    )
    return result


# ─── Recruiter/TA/Admin: manage a round's panel roster ───────────────────────

@router.get("/panelist-options")
def panelist_options(user: dict = Depends(get_current_user)):
    """Lightweight user directory for the panel-picker modal. Deliberately
    NOT /api/admin/users -- that's real user-management (create/deactivate/
    change role) and is admin/ta_manager only. Recruiters legitimately need
    to pick panelists too, but shouldn't get that broader admin surface, so
    this returns just enough to render a picker: id/name/email/role for
    every active account."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    return query(
        "SELECT id, full_name, email, role FROM app_user WHERE is_active = TRUE ORDER BY full_name",
        [],
    )


class RoundPanelistsIn(BaseModel):
    panelist_emails: list[str] = []


@router.patch("/round/{round_config_id}/panelists")
def set_round_panelists(round_config_id: str, body: RoundPanelistsIn, user: dict = Depends(get_current_user)):
    """Set which interviewers make up a round's panel -- the same
    round_config.panelist_emails column the requisition Edit form's round
    setup already writes (pipeline_api.py's RoundIn), just reachable from the
    scheduling flow so a recruiter isn't forced back into the full
    requisition-edit modal to configure it before scheduling. No role
    restriction on WHO can be listed here -- the requisition's Hiring
    Manager can be included like anyone else (a panelist can be the HM);
    they just aren't added automatically (see _finalize_booking_tx)."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    rc = query_one("SELECT id, requisition_id FROM round_config WHERE id = %s", [round_config_id])
    if not rc:
        raise HTTPException(404, "Round not found")
    if user["role"] == "recruiter" and not _recruiter_owns_req(user, str(rc["requisition_id"])):
        raise HTTPException(404, "Round not found")

    # Reuse the same validate+dedupe helper the requisition-edit form's round
    # save path already uses, so the two entry points can never diverge on
    # what counts as a valid panelist email. Imported lazily -- pipeline_api
    # imports FROM this module at its own top level, so a module-level import
    # here would be circular.
    from .pipeline_api import _clean_panelist_emails
    cleaned = _clean_panelist_emails(body.panelist_emails)

    query(
        "UPDATE round_config SET panelist_emails = %s WHERE id = %s",
        [cleaned, round_config_id], fetch=False,
    )
    log_activity(
        "requisition", "round_panelists_updated",
        entity_id=round_config_id, requisition_id=str(rc["requisition_id"]),
        actor_id=user["sub"], actor_role=user["role"],
        detail={"panelist_emails": cleaned},
    )
    return {"ok": True, "panelist_emails": cleaned}


# ─── Recruiter/TA/Admin: book the exact time directly ────────────────────────

class BookDirectIn(BaseModel):
    application_id: str
    round_index: Optional[int] = None
    start_utc: str  # ISO-8601
    duration_min: int = 45
    meeting_link: Optional[str] = None
    extra_emails: list[str] = []


@router.post("/book-direct")
def book_direct(body: BookDirectIn, user: dict = Depends(get_current_user)):
    """Recruiter/TA manager/admin picks the exact interview time themselves --
    skips the HM-grid + candidate-token round trip (and the "please share
    your availability" email that flow would otherwise send) entirely and
    goes straight to a confirmed booking. Reuses an existing active
    interview_schedule_request for this application if one was already
    auto-created on entering the round, so it doesn't create a duplicate --
    but unlike _insert_schedule_request_tx, does NOT require a resolvable
    proposer: the recruiter is choosing the panel (via PATCH
    /round/{id}/panelists) and time themselves, so hm_user_id may stay NULL.
    Reuses _finalize_booking_tx / _send_booking_notifications so "who gets
    invited" can never drift from confirm_pick's candidate-booking path; the
    acting user's own email (plus any freeform extra_emails) is folded into
    that same invite list so their calendar gets booked too."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised to schedule interviews directly")

    ctx = _app_context(body.application_id)
    if not ctx:
        raise HTTPException(404, "Application not found")
    if user["role"] == "recruiter" and not _recruiter_owns_req(user, ctx["requisition_id"]):
        raise HTTPException(404, "Application not found")

    try:
        start_utc = datetime.fromisoformat(body.start_utc.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"Bad datetime: {body.start_utc}")
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    if start_utc <= datetime.now(timezone.utc):
        raise HTTPException(400, "The interview time must be in the future")

    round_config_id = None
    seq = body.round_index if body.round_index is not None else ctx.get("current_round")
    if seq:
        rc = query_one(
            "SELECT id FROM round_config WHERE requisition_id = %s AND sequence = %s",
            [ctx["requisition_id"], seq],
        )
        round_config_id = rc and rc["id"]

    extra_emails = [e for e in ([user.get("email")] + list(body.extra_emails or [])) if e]

    with transaction() as cur:
        existing_rows = tx_exec(
            cur,
            """SELECT * FROM interview_schedule_request
               WHERE application_id = %s AND status = ANY(%s)
               ORDER BY created_at DESC LIMIT 1
               FOR UPDATE""",
            [body.application_id, list(_ACTIVE_STATUSES)],
        )
        if existing_rows:
            lr = existing_rows[0]
            request_id = str(lr["id"])
        else:
            if round_config_id is None:
                rc2 = tx_exec(
                    cur, "SELECT id FROM round_config WHERE requisition_id=%s ORDER BY sequence LIMIT 1",
                    [ctx["requisition_id"]],
                )
                round_config_id = rc2 and rc2[0]["id"]
            if round_config_id is None:
                raise HTTPException(400, "This requisition has no interview rounds configured")
            new_rows = tx_exec(
                cur,
                """INSERT INTO interview_schedule_request
                     (application_id, round_config_id, hm_user_id, duration_min, meeting_link, created_by)
                   VALUES (%s,%s,NULL,%s,%s,%s)
                   RETURNING *""",
                [body.application_id, round_config_id, body.duration_min, body.meeting_link, user["sub"]],
            )
            lr = new_rows[0]
            request_id = str(lr["id"])

        if lr["status"] not in ("awaiting_hm", "awaiting_candidate"):
            raise HTTPException(409, "This interview has already been booked or is no longer available.")

        duration_min = body.duration_min or lr["duration_min"]
        meeting_link = body.meeting_link or lr["meeting_link"]

        tx_exec(
            cur,
            """UPDATE interview_schedule_request
               SET status='confirmed', duration_min=%s, meeting_link=%s, confirmed_at=now()
               WHERE id=%s""",
            [duration_min, meeting_link, request_id],
        )
        # Any still-open slots from an abandoned HM grid are now moot.
        tx_exec(
            cur, "UPDATE interview_slot SET status='released' WHERE request_id=%s AND status='open'",
            [request_id],
        )

        booking = _finalize_booking_tx(
            cur, lr["application_id"], lr["round_config_id"],
            meeting_link, duration_min, lr.get("hm_user_id"), start_utc,
        )

    result = _send_booking_notifications(
        request_id=request_id,
        interview_id=booking["interview_id"],
        application_id=lr["application_id"],
        meeting_link=meeting_link,
        duration_min=duration_min,
        hm_user_id=lr.get("hm_user_id"),
        roster_emails=booking["roster_emails"],
        start_utc=start_utc,
        extra_emails=extra_emails,
        actor_role=user["role"],
        actor_label=user.get("name"),
        reschedule_token=booking["reschedule_token"],
        panel_reschedule_token=booking["panel_reschedule_token"],
        calendar_uid=booking["calendar_uid"],
    )
    log_activity(
        "interview", "interview_booked_directly",
        entity_id=booking["interview_id"], application_id=lr["application_id"],
        requisition_id=ctx.get("requisition_id"),
        actor_id=user["sub"], actor_role=user["role"],
        detail={"start_utc": start_utc.isoformat(), "extra_emails": extra_emails},
    )
    return {
        "confirmed": True,
        "start_utc": start_utc.isoformat(),
        "panel_notified": result["panel_notified"],
    }


class AssignHmIn(BaseModel):
    hm_user_id: str


@router.patch("/request/{request_id}/assign-hm")
def assign_hm(request_id: str, body: AssignHmIn, user: dict = Depends(get_current_user)):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req = _request_row(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    if user["role"] == "recruiter":
        _rid = _application_req_id(req["application_id"])
        if not _rid or not _recruiter_owns_req(user, _rid):
            raise HTTPException(404, "Request not found")
    if req["status"] != "awaiting_hm":
        raise HTTPException(400, f"Request is '{req['status']}' — cannot assign an HM now")
    query(
        "UPDATE interview_schedule_request SET hm_user_id = %s WHERE id = %s",
        [body.hm_user_id, request_id], fetch=False,
    )
    ctx = _app_context(req["application_id"])
    # The HM assignment itself is committed above regardless of the email
    # outcome -- hm_notified=False tells the caller to warn, not roll back.
    hm_notified = _send_hm_request_email(request_id, body.hm_user_id, ctx) if ctx else False
    if ctx:
        notify(
            body.hm_user_id, "hm_availability_requested",
            f"Share your availability for {ctx['candidate_name']}",
            body=f"{ctx['candidate_name']} is ready for a panel interview for {ctx['job_title']}.",
            action_url=f"/?openScheduling={request_id}",
            is_actionable=True,
            requisition_id=ctx.get("requisition_id"), application_id=req["application_id"],
            interview_request_id=request_id,
        )
    return {"ok": True, "hm_notified": hm_notified}


@router.post("/request/{request_id}/resend-hm")
def resend_hm_notification(request_id: str, user: dict = Depends(get_current_user)):
    """Re-sends the 'please share your availability' email to the HM already
    assigned to this request -- for the case where the request is genuinely
    stuck at awaiting_hm because the original email never reached them
    (missed, spam-filtered, or silently failed to send)."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req = _request_row(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    if user["role"] == "recruiter":
        _rid = _application_req_id(req["application_id"])
        if not _rid or not _recruiter_owns_req(user, _rid):
            raise HTTPException(404, "Request not found")
    if req["status"] != "awaiting_hm":
        raise HTTPException(400, f"Request is '{req['status']}' — nothing to resend")
    if not req.get("hm_user_id"):
        raise HTTPException(400, "No hiring manager assigned yet — use Assign HM instead")

    ctx = _app_context(req["application_id"])
    hm_notified = _send_hm_request_email(request_id, str(req["hm_user_id"]), ctx) if ctx else False
    # Resend must ALSO refresh the HM's in-portal notification, not just the
    # email -- upsert (bump created_at/unread) rather than insert, so a
    # repeatedly-resent request doesn't pile up duplicate bell entries.
    refreshed = query(
        """UPDATE notification SET created_at = now(), is_read = false
           WHERE interview_request_id = %s AND recipient_user_id = %s
             AND type = 'hm_availability_requested'
           RETURNING id""",
        [request_id, str(req["hm_user_id"])],
    )
    if not refreshed and ctx:
        notify(
            str(req["hm_user_id"]), "hm_availability_requested",
            f"Share your availability for {ctx['candidate_name']}",
            body=f"{ctx['candidate_name']} is ready for a panel interview for {ctx['job_title']}.",
            action_url=f"/?openScheduling={request_id}",
            is_actionable=True,
            requisition_id=ctx.get("requisition_id"), application_id=req["application_id"],
            interview_request_id=request_id,
        )
    log_activity(
        "interview", "interview_schedule_hm_reminder_sent",
        entity_id=request_id, application_id=req["application_id"],
        requisition_id=ctx and ctx.get("requisition_id"),
        actor_id=user["sub"], actor_role=user["role"],
        detail={"hm_user_id": str(req["hm_user_id"]), "hm_notified": hm_notified},
    )
    return {"ok": True, "hm_notified": hm_notified}


@router.post("/request/{request_id}/resend-candidate")
def resend_candidate_link(request_id: str, user: dict = Depends(get_current_user)):
    """Re-sends the 'pick your interview slot' email to the candidate --
    for the case where the HM has already submitted availability but the
    candidate never got (or never received) the booking link."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    req = _request_row(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    if user["role"] == "recruiter":
        _rid = _application_req_id(req["application_id"])
        if not _rid or not _recruiter_owns_req(user, _rid):
            raise HTTPException(404, "Request not found")
    if req["status"] != "awaiting_candidate":
        raise HTTPException(400, f"Request is '{req['status']}' — nothing to resend")
    if not req.get("candidate_token"):
        raise HTTPException(400, "No booking link has been generated for this request yet")

    ctx = _app_context(req["application_id"])
    base_url, _src = _get_base_url()
    candidate_link = f"{base_url}/interview-schedule?token={req['candidate_token']}"
    candidate_notified = _send_candidate_pick_email(req, ctx, candidate_link)
    log_activity(
        "interview", "interview_schedule_candidate_reminder_sent",
        entity_id=request_id, application_id=req["application_id"],
        requisition_id=ctx and ctx.get("requisition_id"),
        actor_id=user["sub"], actor_role=user["role"],
        detail={"candidate_notified": candidate_notified},
    )
    return {"ok": True, "candidate_notified": candidate_notified}


# ─── Recruiter/TA/Admin + HM: read/list ───────────────────────────────────────

@router.get("/requests")
def list_requests(user: dict = Depends(get_current_user)):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    scope_sql, params = "", []
    if user["role"] == "recruiter":
        scope_sql = """AND a.requisition_id IN (
            SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
        )"""
        params.append(user["sub"])
    return query(
        f"""SELECT isr.id, isr.status, isr.duration_min, isr.created_at,
                   isr.hm_submitted_at, isr.confirmed_at, isr.hm_user_id,
                   c.full_name AS candidate_name, r.title AS job_title,
                   hm.full_name AS hm_name,
                   (SELECT COUNT(*) FROM interview_slot s WHERE s.request_id = isr.id) AS slot_count
            FROM interview_schedule_request isr
            JOIN application  a  ON a.id = isr.application_id
            JOIN candidate    c  ON c.id = a.candidate_id
            JOIN requisition  r  ON r.id = a.requisition_id
            LEFT JOIN app_user hm ON hm.id = isr.hm_user_id
            WHERE 1=1 {scope_sql}
            ORDER BY isr.created_at DESC
            LIMIT 200""",
        params,
    )


def _my_pending_query(uid: str) -> list:
    """Requests waiting on this HM's availability -- shared by GET /my-pending
    and hm_api.hm_dashboard()'s action_queue so the SQL lives once."""
    return query(
        """SELECT isr.id, isr.duration_min, isr.created_at,
                  c.full_name AS candidate_name, r.title AS job_title, r.id AS req_id
           FROM interview_schedule_request isr
           JOIN application  a ON a.id = isr.application_id
           JOIN candidate    c ON c.id = a.candidate_id
           JOIN requisition  r ON r.id = a.requisition_id
           WHERE isr.hm_user_id = %s AND isr.status = 'awaiting_hm'
           ORDER BY isr.created_at ASC""",
        [uid],
    ) or []


@router.get("/my-pending")
def my_pending(user: dict = Depends(get_current_user)):
    """Hiring manager's own queue of requests waiting on their availability."""
    if user["role"] not in ("hiring_manager", "admin"):
        raise HTTPException(403, "Hiring Manager access required")
    return _my_pending_query(user["sub"])


@router.get("/request/{request_id}")
def get_request(request_id: str, user: dict = Depends(get_current_user)):
    req = _request_row(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    is_owner_hm = req.get("hm_user_id") and str(req["hm_user_id"]) == user["sub"]
    if user["role"] not in ("admin", "ta_manager", "recruiter") and not is_owner_hm:
        raise HTTPException(403, "Not authorised")
    if user["role"] == "recruiter":
        _rid = _application_req_id(req["application_id"])
        if not _rid or not _recruiter_owns_req(user, _rid):
            raise HTTPException(404, "Request not found")
    req = _lazy_expire(req)
    slots = query(
        "SELECT id, start_utc, status FROM interview_slot WHERE request_id = %s ORDER BY start_utc",
        [request_id],
    )
    ctx = _app_context(req["application_id"])
    return {
        "id":                 req["id"],
        "application_id":     req["application_id"],
        "status":             req["status"],
        "duration_min":       req["duration_min"],
        "meeting_link":       req["meeting_link"],
        "round_config_id":    req["round_config_id"],
        "hm_user_id":         req["hm_user_id"],
        "created_at":         req["created_at"],
        "hm_submitted_at":    req["hm_submitted_at"],
        "confirmed_at":       req["confirmed_at"],
        "confirmed_slot_id":  req["confirmed_slot_id"],
        "slots":              slots or [],
        "candidate_name":     ctx and ctx.get("candidate_name"),
        "job_title":          ctx and ctx.get("job_title"),
    }


# ─── HM: submit availability ──────────────────────────────────────────────────

class SlotsIn(BaseModel):
    slots: list[str]  # ISO-8601 UTC datetimes


def _validate_slot_list(raw_slots: list[str]) -> list[datetime]:
    """Shared by submit_slots (initial) and edit_slots (re-submit) so both
    stay in sync on min/max count and the future-only/2-month-horizon rules."""
    if not (_MIN_SLOTS <= len(raw_slots) <= _MAX_SLOTS):
        raise HTTPException(400, f"Pick between {_MIN_SLOTS} and {_MAX_SLOTS} slots")

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=_MAX_SLOT_DAYS_OUT)
    parsed = []
    for raw in raw_slots:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, f"Bad datetime: {raw}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt <= now:
            raise HTTPException(400, "All slots must be in the future")
        if dt > horizon:
            raise HTTPException(400, "Slots must be within the next 2 months")
        parsed.append(dt)
    return parsed


@router.post("/request/{request_id}/slots")
def submit_slots(request_id: str, body: SlotsIn, user: dict = Depends(get_current_user)):
    req = _request_row(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    is_owner_hm = req.get("hm_user_id") and str(req["hm_user_id"]) == user["sub"]
    if user["role"] != "admin" and not is_owner_hm:
        raise HTTPException(403, "Not authorised to submit availability for this request")
    if req["status"] != "awaiting_hm":
        raise HTTPException(400, f"Request is '{req['status']}' — availability already submitted")

    parsed = _validate_slot_list(body.slots)
    ctx = _app_context(req["application_id"])
    recruiter_id = ctx and _owning_recruiter_id(ctx["requisition_id"])
    token = secrets.token_urlsafe(32)

    # Slot inserts + status flip + recruiter notification commit atomically --
    # a mid-way DB failure must never leave slots inserted with the request
    # still 'awaiting_hm', or the flip committed with no one ever told.
    with transaction() as cur:
        locked = tx_exec(
            cur, "SELECT status FROM interview_schedule_request WHERE id=%s FOR UPDATE",
            [request_id],
        )
        lr = locked[0] if locked else None
        if not lr or lr["status"] != "awaiting_hm":
            raise HTTPException(400, "Availability already submitted for this request")
        for dt in parsed:
            tx_exec(cur, "INSERT INTO interview_slot (request_id, start_utc) VALUES (%s, %s)", [request_id, dt])
        tx_exec(
            cur,
            """UPDATE interview_schedule_request
               SET status = 'awaiting_candidate', candidate_token = %s, hm_submitted_at = now()
               WHERE id = %s""",
            [token, request_id],
        )
        if recruiter_id and ctx:
            notify_tx(
                cur, recruiter_id, "candidate_slots_submitted",
                f"{ctx['candidate_name']} can now pick an interview slot",
                body=f"Availability was shared for {ctx['job_title']} — waiting on {ctx['candidate_name']} to book.",
                action_url=f"/?schedRequest={request_id}#interviews",
                is_actionable=False,
                requisition_id=ctx.get("requisition_id"), application_id=req["application_id"],
                interview_request_id=request_id,
            )

    base_url, _src = _get_base_url()
    candidate_link = f"{base_url}/interview-schedule?token={token}"
    candidate_notified = _send_candidate_pick_email(req, ctx, candidate_link)

    log_activity(
        "interview", "interview_slots_submitted",
        entity_id=request_id, application_id=req["application_id"],
        requisition_id=ctx and ctx.get("requisition_id"),
        actor_id=user["sub"], actor_role=user["role"],
        detail={"slot_count": len(parsed), "candidate_notified": candidate_notified},
    )

    # candidate_notified=False means the request is real (status is already
    # awaiting_candidate) but the candidate never got the link -- caller must
    # warn, not show a plain success, and can retry via resend-candidate-link.
    return {"ok": True, "candidate_link": candidate_link, "candidate_notified": candidate_notified}


@router.patch("/request/{request_id}/slots")
def edit_slots(request_id: str, body: SlotsIn, user: dict = Depends(get_current_user)):
    """Re-open editing on a request whose slots haven't been booked yet --
    fixes the HM-facing bug where submitted availability could never be
    revisited once the request moved to awaiting_candidate."""
    req = _request_row(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    is_owner_hm = req.get("hm_user_id") and str(req["hm_user_id"]) == user["sub"]
    if user["role"] != "admin" and not is_owner_hm:
        raise HTTPException(403, "Not authorised to edit availability for this request")
    if req["status"] != "awaiting_candidate":
        raise HTTPException(400, f"Request is '{req['status']}' — nothing to edit")
    if req.get("confirmed_slot_id"):
        raise HTTPException(409, "The candidate has already booked a slot — this can no longer be edited")

    pre_check_taken = query_one(
        "SELECT 1 FROM interview_slot WHERE request_id=%s AND status != 'open'", [request_id]
    )
    if pre_check_taken:
        raise HTTPException(409, "A slot on this request is no longer open — it can no longer be edited")

    parsed = _validate_slot_list(body.slots)

    # Re-check under a row lock on the request -- closes the TOCTOU window
    # between the pre-checks above and this write, against a candidate
    # confirming via POST /pick/confirm in between (that endpoint takes the
    # same FOR UPDATE lock, so the two are correctly serialized).
    with transaction() as cur:
        locked = tx_exec(
            cur, "SELECT status, confirmed_slot_id FROM interview_schedule_request WHERE id=%s FOR UPDATE",
            [request_id],
        )
        lr = locked[0] if locked else None
        if not lr or lr["status"] != "awaiting_candidate" or lr["confirmed_slot_id"]:
            raise HTTPException(409, "This request can no longer be edited")
        still_taken = tx_exec(
            cur, "SELECT 1 FROM interview_slot WHERE request_id=%s AND status != 'open'", [request_id]
        )
        if still_taken:
            raise HTTPException(409, "A slot on this request is no longer open — it can no longer be edited")
        tx_exec(cur, "DELETE FROM interview_slot WHERE request_id=%s AND status='open'", [request_id])
        for dt in parsed:
            tx_exec(cur, "INSERT INTO interview_slot (request_id, start_utc) VALUES (%s,%s)", [request_id, dt])

    log_activity(
        "interview", "interview_slots_edited",
        entity_id=request_id, application_id=req["application_id"],
        actor_id=user["sub"], actor_role=user["role"],
        detail={"slot_count": len(parsed)},
    )
    return {"ok": True, "slot_count": len(parsed)}


# ─── Candidate (PUBLIC — token only) ──────────────────────────────────────────

@router.get("/pick/validate")
def validate_pick(token: str):
    req = query_one("SELECT * FROM interview_schedule_request WHERE candidate_token = %s", [token])
    if not req:
        return {"valid": False, "reason": "not_found"}
    req = _lazy_expire(req)
    if req["status"] == "confirmed":
        return {"valid": False, "reason": "already_confirmed"}
    if req["status"] != "awaiting_candidate":
        return {"valid": False, "reason": req["status"]}

    ctx = _app_context(req["application_id"])
    slots = query(
        "SELECT id, start_utc FROM interview_slot WHERE request_id = %s AND status = 'open' ORDER BY start_utc",
        [req["id"]],
    )
    return {
        "valid": True,
        "candidate_name": ctx and ctx.get("candidate_name"),
        "job_title":      ctx and ctx.get("job_title"),
        "company":        ctx and ctx.get("company"),
        "duration_min":   req["duration_min"],
        "slots":          slots or [],
    }


def _finalize_booking_tx(
    cur,
    application_id: str,
    round_config_id: str,
    meeting_link: Optional[str],
    duration_min: int,
    hm_user_id: Optional[str],
    start_utc: datetime,
) -> dict:
    """DB half of locking in a confirmed interview time: creates the
    `interview` row and populates `interview_panel` from the round's
    configured roster. Must run inside the caller's open transaction so it
    rolls back together with whatever status change accompanies it.

    NOTE: whoever proposed availability (hm_user_id) is deliberately NOT
    added to interview_panel just for holding that role -- they only become
    a scorecard-eligible panelist if their own email is also on the round's
    roster below. A panelist CAN also be the HM; the HM isn't automatically
    a panelist just by holding that title.

    Shared by confirm_pick (candidate books from an HM's slot grid) and
    book_direct (recruiter picks the exact time themselves) so the two flows
    can never drift on what "being a real panelist" means.
    """
    iv_rows = tx_exec(
        cur,
        """INSERT INTO interview
             (application_id, round_config_id, scheduled_at, meet_link, mode, duration_min)
           VALUES (%s, %s, %s, %s, 'virtual', %s)
           RETURNING id""",
        [application_id, round_config_id, start_utc, meeting_link, duration_min],
    )
    interview_id = iv_rows[0]["id"]

    # A stable per-interview reschedule link + calendar UID, set once here at
    # first booking. Candidate and panel/HM get SEPARATE tokens (embedded in
    # their respective confirmation emails, see _send_booking_notifications)
    # so a reschedule can be attributed to 'candidate' vs 'panel' in
    # interview_reschedule (see the public /reschedule/* endpoints below) --
    # useful for reporting on who's rescheduling. calendar_uid lets a later
    # reschedule re-send the SAME calendar event (bumped SEQUENCE) instead of
    # creating a duplicate one.
    reschedule_token = secrets.token_urlsafe(32)
    panel_reschedule_token = secrets.token_urlsafe(32)
    calendar_uid = f"{interview_id}@enternly-ats"
    tx_exec(
        cur,
        "UPDATE interview SET reschedule_token=%s, panel_reschedule_token=%s, calendar_uid=%s WHERE id=%s",
        [reschedule_token, panel_reschedule_token, calendar_uid, str(interview_id)],
    )

    rc_rows = tx_exec(cur, "SELECT panelist_emails FROM round_config WHERE id = %s", [round_config_id])
    roster_emails = list((rc_rows[0]["panelist_emails"] if rc_rows else None) or [])
    for email in roster_emails:
        pu_rows = tx_exec(
            cur, "SELECT id FROM app_user WHERE LOWER(email) = LOWER(%s) AND is_active = TRUE", [email]
        )
        if pu_rows:
            tx_exec(
                cur,
                "INSERT INTO interview_panel (interview_id, interviewer_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                [str(interview_id), str(pu_rows[0]["id"])],
            )

    return {
        "interview_id": interview_id,
        "roster_emails": roster_emails,
        "reschedule_token": reschedule_token,
        "panel_reschedule_token": panel_reschedule_token,
        "calendar_uid": calendar_uid,
    }


def _send_booking_notifications(
    request_id: str,
    interview_id: str,
    application_id: str,
    meeting_link: Optional[str],
    duration_min: int,
    hm_user_id: Optional[str],
    roster_emails: list[str],
    start_utc: datetime,
    extra_emails: Optional[list[str]] = None,
    actor_role: str = "candidate",
    actor_label: Optional[str] = None,
    reschedule_token: Optional[str] = None,
    panel_reschedule_token: Optional[str] = None,
    calendar_uid: Optional[str] = None,
    ics_sequence: int = 0,
) -> dict:
    """Post-commit half of locking in a confirmed interview: Google Calendar
    event + ICS/branded emails to the candidate, every roster panelist,
    whoever proposed the slots, and any extra_emails (e.g. the acting
    recruiter's own address on a directly-booked interview, so their
    calendar gets the meeting too). Never call this inside an open
    transaction -- a mail/API failure here must never roll back an
    already-confirmed interview row.

    reschedule_token / panel_reschedule_token: embedded as a "Can't make it?"
    link in the candidate's and the panel/HM's copy of the email
    respectively -- SEPARATE tokens so a later reschedule can be attributed
    to 'candidate' vs 'panel' for reporting (see /reschedule/* endpoints and
    interview_reschedule).
    calendar_uid/ics_sequence: passed straight through to
    connectors.schedule_meeting so a reschedule re-sends the SAME calendar
    event (bumped sequence) instead of creating a duplicate."""
    ctx = _app_context(application_id)
    base_url, _src = _get_base_url()
    candidate_reschedule_link = f"{base_url}/reschedule?token={reschedule_token}" if reschedule_token else None
    panel_reschedule_link = f"{base_url}/reschedule?token={panel_reschedule_token}" if panel_reschedule_token else None

    hm_email = hm_name = None
    if hm_user_id:
        hm_row = query_one("SELECT email, full_name FROM app_user WHERE id = %s", [hm_user_id])
        hm_email = hm_row and hm_row.get("email")
        hm_name  = hm_row and hm_row.get("full_name")

    # Full invite list = round roster + whoever proposed slots + any extra
    # emails (recruiter/CC on a direct booking), deduped case-insensitively.
    seen_lower, panel_emails = set(), []
    for e in ([hm_email] if hm_email else []) + roster_emails + list(extra_emails or []):
        if e and e.lower() not in seen_lower:
            seen_lower.add(e.lower())
            panel_emails.append(e)

    panel_attachments = []
    if ctx and ctx.get("candidate_id"):
        # Guarded independently: a corrupt/unreadable CV file must not crash
        # this response -- the interview row is already committed, so an
        # unhandled exception here used to surface as an unhandled 500 for a
        # booking that actually succeeded, with no email/ICS sent and no trace.
        cv_load_failed = False
        try:
            cv = connectors.load_candidate_cv_attachment(ctx["candidate_id"])
        except Exception as exc:
            cv = None
            cv_load_failed = True
            log_activity(
                "interview", "cv_attachment_load_failed",
                entity_id=interview_id, application_id=application_id,
                requisition_id=ctx.get("requisition_id"),
                actor_id=None, actor_role="system",
                detail={"candidate_id": str(ctx["candidate_id"]), "error": str(exc)},
            )
        if cv:
            panel_attachments.append(cv)
        elif not cv_load_failed:
            log_activity(
                "interview", "cv_missing",
                entity_id=interview_id, application_id=application_id,
                requisition_id=ctx.get("requisition_id"),
                actor_id=None, actor_role="system",
                detail={"candidate_id": str(ctx["candidate_id"])},
            )

    meeting_result = None
    if ctx:
        meet_link = meeting_link or ""

        # If a TA admin has connected a Google account, auto-generate a real
        # Meet link tied to THIS specific interview's confirmed time --
        # overriding the round's static default. Never blocks booking: any
        # failure here just falls back to the static link.
        try:
            gcal_result = google_calendar.create_event_with_meet(
                summary=f"Interview – {ctx['candidate_name']} – {ctx['job_title']}",
                description=f"Panel interview for {ctx['job_title']} at {ctx.get('company') or ''}.",
                start_dt_utc=start_utc,
                duration_min=duration_min,
                attendee_emails=[e for e in ([ctx["candidate_email"]] + panel_emails) if e],
            )
        except Exception as exc:
            gcal_result = None
            print(f"[scheduling] Google Calendar event creation failed: {exc}")
        if gcal_result and gcal_result.get("event_id"):
            # Persisted so a later cancellation can delete this exact event --
            # previously only ever logged to the Activity Timeline detail blob
            # and never written to a real column, so cancelling could never
            # find it again to remove it from Google Calendar.
            query(
                "UPDATE interview SET gcal_event_id = %s WHERE id = %s",
                [gcal_result["event_id"], interview_id], fetch=False,
            )
        if gcal_result and gcal_result.get("meet_link"):
            meet_link = gcal_result["meet_link"]
            query(
                "UPDATE interview_schedule_request SET meeting_link = %s WHERE id = %s",
                [meet_link, request_id], fetch=False,
            )
            log_activity(
                "interview", "gcal_event_created",
                entity_id=interview_id, application_id=application_id,
                requisition_id=ctx.get("requisition_id"),
                actor_id=None, actor_role="system",
                detail={"event_id": gcal_result.get("event_id"), "meet_link": meet_link},
            )

        start_ist = connectors.to_ist(start_utc)
        date_str  = start_ist.strftime("%A, %d %B %Y")
        time_str  = start_ist.strftime("%I:%M %p IST")
        candidate_html = _build_candidate_confirmation_html(
            candidate_name=ctx["candidate_name"], job_title=ctx["job_title"], company=ctx.get("company"),
            date_str=date_str, time_str=time_str, duration_min=duration_min, meet_link=meet_link,
            reschedule_link=candidate_reschedule_link,
        )
        hm_html = _build_hm_confirmation_html(
            hm_name=hm_name, candidate_name=ctx["candidate_name"], job_title=ctx["job_title"], company=ctx.get("company"),
            date_str=date_str, time_str=time_str, duration_min=duration_min, meet_link=meet_link,
            reschedule_link=panel_reschedule_link,
        )
        meeting_result = connectors.schedule_meeting(
            organizer_email=hm_email or "",
            candidate_email=ctx["candidate_email"],
            panel_emails=panel_emails,
            start_time=start_utc,
            duration_min=duration_min,
            meet_link=meet_link,
            candidate_name=ctx["candidate_name"],
            job_title=ctx["job_title"],
            candidate_html=candidate_html,
            panel_html=hm_html,
            candidate_attachments=[],
            panel_attachments=panel_attachments,
            uid=calendar_uid,
            sequence=ics_sequence,
        )

    log_activity(
        "interview", "interview_scheduled",
        entity_id=interview_id, application_id=application_id,
        requisition_id=ctx and ctx.get("requisition_id"),
        actor_id=None, actor_role=actor_role, actor_label=actor_label or (ctx and ctx.get("candidate_name")),
        to_value=start_utc.isoformat(),
        detail={"duration_min": duration_min, "mode": "virtual"},
    )

    # Auto panel interviews still book automatically even if a panelist's
    # invite didn't go out -- but this must not fail silently. Surface it as
    # a discoverable warning in the Activity Timeline (same log a recruiter
    # already reviews) rather than a print-only trace, so the recruiter can
    # notice and resend the panel invite manually.
    panel_notified = bool(meeting_result and meeting_result.get("invite_sent"))
    if panel_emails and not panel_notified:
        log_activity(
            "interview", "panel_invite_incomplete",
            entity_id=interview_id, application_id=application_id,
            requisition_id=ctx and ctx.get("requisition_id"),
            actor_id=None, actor_role="system",
            detail={
                "expected": panel_emails,
                "sent_to": (meeting_result or {}).get("invite_sent_to") or [],
                "missing": (meeting_result or {}).get("invite_missing") or panel_emails,
                "stub": bool((meeting_result or {}).get("invite_stub")),
            },
        )
    return {"panel_notified": panel_notified, "panel_emails": panel_emails}


class ConfirmIn(BaseModel):
    slot_id: str


@router.post("/pick/confirm")
def confirm_pick(token: str, body: ConfirmIn):
    req = query_one("SELECT * FROM interview_schedule_request WHERE candidate_token = %s", [token])
    if not req:
        raise HTTPException(404, "Invalid link")
    req = _lazy_expire(req)

    slot = query_one(
        "SELECT id, status FROM interview_slot WHERE id = %s AND request_id = %s",
        [body.slot_id, req["id"]],
    )
    if not slot:
        raise HTTPException(404, "Slot not found")
    if slot["status"] != "open":
        raise HTTPException(409, "That slot was just taken — please pick another.")

    # Row-level lock on the request: the second concurrent confirm blocks here
    # until the first commits, then re-reads status='confirmed' and bails —
    # this is what actually prevents double-booking (the old conditional-UPDATE
    # "race gate" only worked because each query() committed independently;
    # under a shared transaction it would let both racers pass).
    with transaction() as cur:
        locked = tx_exec(
            cur,
            "SELECT id, status, application_id, round_config_id, meeting_link, duration_min, hm_user_id "
            "FROM interview_schedule_request WHERE id=%s FOR UPDATE",
            [req["id"]],
        )
        lr = locked[0] if locked else None
        if not lr or lr["status"] != "awaiting_candidate":
            raise HTTPException(409, "This interview has already been booked or is no longer available.")

        booked_ctx_rows = tx_exec(
            cur,
            """SELECT a.requisition_id, c.full_name AS candidate_name, r.title AS job_title
               FROM application a
               JOIN candidate   c ON c.id = a.candidate_id
               JOIN requisition r ON r.id = a.requisition_id
               WHERE a.id = %s""",
            [lr["application_id"]],
        )
        booked_ctx = booked_ctx_rows[0] if booked_ctx_rows else {}
        booked_requisition_id = booked_ctx.get("requisition_id")

        # Re-check the slot under the same lock (the request lock serializes
        # confirms for THIS request, so this read is now authoritative).
        slot_rows = tx_exec(
            cur, "SELECT status, start_utc FROM interview_slot WHERE id=%s AND request_id=%s",
            [body.slot_id, req["id"]],
        )
        sr = slot_rows[0] if slot_rows else None
        if not sr:
            raise HTTPException(404, "Slot not found")
        if sr["status"] != "open":
            raise HTTPException(409, "That slot was just taken — please pick another.")
        start_utc = sr["start_utc"]

        tx_exec(
            cur,
            """UPDATE interview_schedule_request
               SET status = 'confirmed', confirmed_slot_id = %s, confirmed_at = now()
               WHERE id = %s""",
            [body.slot_id, req["id"]],
        )
        tx_exec(cur, "UPDATE interview_slot SET status = 'taken' WHERE id = %s", [body.slot_id])
        tx_exec(
            cur,
            "UPDATE interview_slot SET status = 'released' WHERE request_id = %s AND id != %s AND status = 'open'",
            [req["id"], body.slot_id],
        )

        if lr.get("hm_user_id"):
            notify_tx(
                cur, str(lr["hm_user_id"]), "interview_confirmed",
                f"{booked_ctx.get('candidate_name')} booked a slot",
                body=f"{booked_ctx.get('candidate_name')} confirmed an interview time for {booked_ctx.get('job_title')}.",
                action_url="/#hm_dashboard", is_actionable=False,
                requisition_id=booked_requisition_id, application_id=lr["application_id"],
                interview_request_id=req["id"],
            )
        recruiter_id = booked_requisition_id and _owning_recruiter_id(str(booked_requisition_id))
        if recruiter_id:
            notify_tx(
                cur, recruiter_id, "interview_confirmed",
                f"{booked_ctx.get('candidate_name')} booked a slot",
                body=f"{booked_ctx.get('candidate_name')} confirmed an interview time for {booked_ctx.get('job_title')}.",
                action_url=f"/?schedRequest={req['id']}#interviews", is_actionable=False,
                requisition_id=booked_requisition_id, application_id=lr["application_id"],
                interview_request_id=req["id"],
            )

        booking = _finalize_booking_tx(
            cur, lr["application_id"], lr["round_config_id"],
            lr["meeting_link"], lr["duration_min"], lr.get("hm_user_id"), start_utc,
        )

    # Everything below is an external side-effect (email/ICS) — it runs AFTER
    # the transaction has committed, so a mail failure never rolls back a
    # confirmed booking.
    result = _send_booking_notifications(
        request_id=req["id"],
        interview_id=booking["interview_id"],
        application_id=lr["application_id"],
        meeting_link=lr["meeting_link"],
        duration_min=lr["duration_min"],
        hm_user_id=lr.get("hm_user_id"),
        roster_emails=booking["roster_emails"],
        start_utc=start_utc,
        actor_role="candidate",
        reschedule_token=booking["reschedule_token"],
        panel_reschedule_token=booking["panel_reschedule_token"],
        calendar_uid=booking["calendar_uid"],
    )

    return {
        "confirmed": True,
        "start_utc": start_utc.isoformat(),
        "panel_notified": result["panel_notified"],
    }


# ─── Public (token only): self-service reschedule ────────────────────────────
# Reached from the "Can't make it? Request a reschedule" link embedded in
# every confirmation email (candidate's and each panelist's, see
# _reschedule_link_html above) -- whoever clicks it picks a new time
# themselves, no login and no approval step. Works identically regardless of
# whether the interview was originally booked via confirm_pick (auto-
# triggered or manual "Let candidate pick") or book_direct (manual "Schedule
# now"), since both already funnel through _finalize_booking_tx, which is
# where reschedule_token/calendar_uid get set on every interview row.

def _lookup_interview_by_reschedule_token(token: str) -> Optional[tuple]:
    """Matches either the candidate's or the panel/HM's reschedule token
    (see _finalize_booking_tx) and reports which category it was -- that
    category becomes interview_reschedule.requested_by, so reports can break
    down reschedules by 'candidate' vs 'panel'. Returns (row, requested_by)
    or (None, None)."""
    iv = query_one(
        """SELECT id, status, scheduled_at, duration_min, application_id, round_config_id,
                  meet_link, calendar_uid, ics_sequence, reschedule_token, panel_reschedule_token
           FROM interview WHERE reschedule_token = %s OR panel_reschedule_token = %s""",
        [token, token],
    )
    if not iv:
        return None, None
    requested_by = "candidate" if iv.get("reschedule_token") == token else "panel"
    return iv, requested_by


@router.get("/reschedule/validate")
def validate_reschedule(token: str):
    iv, _requested_by = _lookup_interview_by_reschedule_token(token)
    if not iv:
        return {"valid": False, "reason": "not_found"}
    if iv["status"] in ("cancelled", "completed"):
        return {"valid": False, "reason": iv["status"]}

    ctx = _app_context(iv["application_id"])
    return {
        "valid": True,
        "candidate_name":         ctx and ctx.get("candidate_name"),
        "job_title":              ctx and ctx.get("job_title"),
        "company":                ctx and ctx.get("company"),
        "current_scheduled_at":   iv["scheduled_at"].isoformat() if iv["scheduled_at"] else None,
        "duration_min":           iv["duration_min"],
    }


class RescheduleConfirmIn(BaseModel):
    start_utc: str  # ISO-8601


def _parse_and_validate_new_start(start_utc: str) -> datetime:
    try:
        new_start = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"Bad datetime: {start_utc}")
    if new_start.tzinfo is None:
        new_start = new_start.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if new_start <= now:
        raise HTTPException(400, "The new time must be in the future")
    if new_start > now + timedelta(days=_MAX_SLOT_DAYS_OUT):
        raise HTTPException(400, f"The new time must be within the next {_MAX_SLOT_DAYS_OUT} days")
    return new_start


def _reschedule_interview(
    iv: dict, new_start: datetime, *,
    actor_role: str, actor_label: str, requested_by_label: str,
    actor_id: Optional[str] = None,
) -> dict:
    """Shared core for both the public token-based self-service reschedule
    and the authenticated in-app staff reschedule: updates
    interview.scheduled_at, records an interview_reschedule audit row,
    re-sends calendar invites/branded emails to everyone, logs the activity,
    and notifies the owning recruiter. Callers must have already validated
    the interview is reschedulable (not cancelled/completed) before calling."""
    interview_id = str(iv["id"])
    old_scheduled_at = iv["scheduled_at"]
    new_sequence = int(iv["ics_sequence"] or 0) + 1

    with transaction() as cur:
        locked = tx_exec(cur, "SELECT status FROM interview WHERE id=%s FOR UPDATE", [interview_id])
        lr = locked[0] if locked else None
        if not lr or lr["status"] in ("cancelled", "completed"):
            raise HTTPException(409, "This interview can no longer be rescheduled")
        tx_exec(
            cur,
            "UPDATE interview SET scheduled_at=%s, ics_sequence=%s WHERE id=%s",
            [new_start, new_sequence, interview_id],
        )
        # Timestamped, reportable history row -- see report_catalog.py's
        # "interview_reschedule" explore for the Custom Reports side of this.
        tx_exec(
            cur,
            """INSERT INTO interview_reschedule
                 (interview_id, application_id, requested_by, old_scheduled_at, new_scheduled_at)
               VALUES (%s, %s, %s, %s, %s)""",
            [interview_id, iv["application_id"], requested_by_label, old_scheduled_at, new_start],
        )

    # Who to (re-)notify: every real panelist already recorded on THIS
    # interview (interview_panel -- the authoritative roster, resolved once
    # at booking time) + the candidate + the requisition's owning recruiter.
    panel_rows = query(
        """SELECT u.email FROM interview_panel ip JOIN app_user u ON u.id = ip.interviewer_id
           WHERE ip.interview_id = %s""",
        [interview_id],
    ) or []
    roster_emails = [r["email"] for r in panel_rows if r.get("email")]

    ctx = _app_context(iv["application_id"])
    recruiter_id = ctx and _owning_recruiter_id(str(ctx["requisition_id"]))

    # Reuse the interview_schedule_request row from the original booking
    # purely so _send_booking_notifications' gcal-meeting-link update has
    # somewhere to write -- a reschedule never creates a fresh request.
    isr = query_one(
        """SELECT id FROM interview_schedule_request
           WHERE application_id=%s AND round_config_id=%s
           ORDER BY created_at DESC LIMIT 1""",
        [iv["application_id"], iv["round_config_id"]],
    )

    result = _send_booking_notifications(
        request_id=str(isr["id"]) if isr else interview_id,
        interview_id=interview_id,
        application_id=iv["application_id"],
        meeting_link=iv["meet_link"],
        duration_min=iv["duration_min"],
        hm_user_id=None,
        roster_emails=roster_emails,
        start_utc=new_start,
        actor_role=actor_role,
        actor_label=actor_label,
        reschedule_token=iv["reschedule_token"],
        panel_reschedule_token=iv["panel_reschedule_token"],
        calendar_uid=iv["calendar_uid"],
        ics_sequence=new_sequence,
    )

    log_activity(
        "interview", "interview_rescheduled",
        entity_id=interview_id, application_id=iv["application_id"],
        requisition_id=ctx and ctx.get("requisition_id"),
        actor_id=actor_id, actor_role=actor_role,
        detail={
            "requested_by": requested_by_label,
            "old_start_utc": old_scheduled_at.isoformat() if old_scheduled_at else None,
            "new_start_utc": new_start.isoformat(),
            "ics_sequence": new_sequence,
        },
    )
    if recruiter_id and ctx and recruiter_id != actor_id:
        notify(
            recruiter_id, "interview_rescheduled",
            f"{ctx.get('candidate_name')}'s interview was rescheduled",
            body=f"New time: {connectors.to_ist(new_start).strftime('%A, %d %B %Y at %I:%M %p IST')} for {ctx.get('job_title')}.",
            action_url=(f"/?schedRequest={isr['id']}#interviews" if isr else None),
            is_actionable=False,
            requisition_id=ctx.get("requisition_id"), application_id=iv["application_id"],
            interview_request_id=(str(isr["id"]) if isr else None),
        )

    return {
        "confirmed": True,
        "start_utc": new_start.isoformat(),
        "panel_notified": result["panel_notified"],
    }


@router.post("/reschedule/confirm")
def confirm_reschedule(token: str, body: RescheduleConfirmIn):
    iv, requested_by = _lookup_interview_by_reschedule_token(token)
    if not iv:
        raise HTTPException(404, "Invalid link")
    if iv["status"] in ("cancelled", "completed"):
        raise HTTPException(400, f"This interview is '{iv['status']}' and can no longer be rescheduled")
    new_start = _parse_and_validate_new_start(body.start_utc)
    return _reschedule_interview(
        iv, new_start,
        actor_role="system", actor_label=f"Rescheduled by {requested_by}",
        requested_by_label=requested_by,
    )


# ─── Recruiter/TA/Admin: reschedule an already-booked interview in-app ───────
# The self-service link above is for the candidate/panel; a recruiter or TA
# manager working the Kanban/Pipeline view needs to do the same thing
# themselves without waiting on an email round-trip -- e.g. correcting a time
# that was mistakenly booked. Same underlying update + renotify path.

@router.post("/interviews/{interview_id}/reschedule")
def reschedule_interview_staff(
    interview_id: str, body: RescheduleConfirmIn, user: dict = Depends(get_current_user)
):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised to reschedule interviews")

    iv = query_one(
        """SELECT id, status, scheduled_at, duration_min, application_id, round_config_id,
                  meet_link, calendar_uid, ics_sequence, reschedule_token, panel_reschedule_token
           FROM interview WHERE id = %s""",
        [interview_id],
    )
    if not iv:
        raise HTTPException(404, "Interview not found")
    if iv["status"] in ("cancelled", "completed"):
        raise HTTPException(400, f"This interview is '{iv['status']}' and can no longer be rescheduled")

    ctx = _app_context(iv["application_id"])
    if not ctx:
        raise HTTPException(404, "Application not found")
    if user["role"] == "recruiter" and not _recruiter_owns_req(user, str(ctx["requisition_id"])):
        raise HTTPException(404, "Interview not found")

    new_start = _parse_and_validate_new_start(body.start_utc)
    return _reschedule_interview(
        iv, new_start,
        actor_role=user["role"], actor_label=user.get("name") or user["role"],
        requested_by_label="staff", actor_id=user["sub"],
    )


# ─── Cancellation ──────────────────────────────────────────────────────────
# Recruiters sometimes double-book a round by mistake (e.g. both "Let
# candidate pick" and "Schedule now" for the same candidate+round). Cancelling
# a genuine duplicate is a one-click action; cancelling the ONLY booked slot
# for a round is allowed too but requires an explicit second confirmation
# (force=true) since that's more often "I meant to reschedule" than a real
# do-over -- the first call without force just hands that choice back to the
# caller instead of silently cancelling a live invite.

class CancelInterviewIn(BaseModel):
    force: bool = False
    reason: Optional[str] = None


def _build_cancellation_html(
    *, candidate_name: str, job_title: str, company: Optional[str],
    round_name: Optional[str], scheduled_str: str, reason: Optional[str],
) -> str:
    detail_cells = [
        ("Candidate", candidate_name),
        ("Position", f"{job_title} – {company}" if company else job_title),
        ("Round", round_name or "Interview"),
        ("Was Scheduled For", scheduled_str),
    ]
    extra = None
    if reason:
        extra = (
            '<p style="font-size:14px;color:#4a4744;font-family:Arial,Helvetica,'
            f'sans-serif;margin:0 0 20px 0"><strong>Reason:</strong> {_esc(reason)}</p>'
        )
    return build_branded_email(
        eyebrow="Interview Update",
        hero_title_html="Interview<br>Cancelled.",
        hero_subtitle=f"The {_esc(round_name or 'interview')} for {_esc(job_title)} has been cancelled.",
        detail_cells=detail_cells,
        extra_body_html=extra,
        footer_note="If this was a mistake or you'd like a new time, please reach out to your recruiter.",
    )


@router.post("/interviews/{interview_id}/cancel")
def cancel_interview(interview_id: str, body: CancelInterviewIn, user: dict = Depends(get_current_user)):
    if user["role"] not in ("recruiter", "ta_manager", "admin", "hiring_manager", "hrbp"):
        raise HTTPException(403, "Not authorised to cancel interviews")

    iv = query_one(
        """SELECT id, status, application_id, round_config_id, scheduled_at,
                  duration_min, gcal_event_id, calendar_uid, ics_sequence
           FROM interview WHERE id = %s""",
        [interview_id],
    )
    if not iv:
        raise HTTPException(404, "Interview not found")
    if iv["status"] in ("cancelled", "completed"):
        raise HTTPException(400, f"This interview is already '{iv['status']}'")

    ctx = _app_context(iv["application_id"])
    if not ctx:
        raise HTTPException(404, "Application not found")
    req_id = str(ctx["requisition_id"])

    # Scope: same ownership rules as everywhere else an interview is touched.
    if user["role"] == "recruiter" and not _recruiter_owns_req(user, req_id):
        raise HTTPException(404, "Interview not found")
    if user["role"] == "hiring_manager" and not query_one(
        "SELECT 1 FROM requisition WHERE id = %s AND hiring_manager_id = %s", [req_id, user["sub"]]
    ):
        raise HTTPException(404, "Interview not found")
    if user["role"] == "hrbp":
        from .hrbp_api import scope_requisitions_for_hrbp
        where, params = scope_requisitions_for_hrbp(user)
        if not query_one(f"SELECT 1 FROM requisition r WHERE r.id = %s AND {where}", [req_id, *params]):
            raise HTTPException(404, "Interview not found")

    dup = query_one(
        """SELECT 1 FROM interview
           WHERE application_id = %s AND round_config_id = %s
             AND status = 'scheduled' AND id != %s""",
        [iv["application_id"], iv["round_config_id"], interview_id],
    )
    is_duplicate = bool(dup)
    if not is_duplicate and not body.force:
        return {
            "cancelled": False,
            "needs_confirmation": True,
            "message": (
                "This interview invite has already been shared with the candidate and panel. "
                "Would you like to revise the timing instead, or cancel it anyway?"
            ),
        }

    with transaction() as cur:
        locked = tx_exec(cur, "SELECT status, ics_sequence FROM interview WHERE id = %s FOR UPDATE", [interview_id])
        lr = locked[0] if locked else None
        if not lr or lr["status"] in ("cancelled", "completed"):
            raise HTTPException(409, "This interview can no longer be cancelled")
        new_sequence = int(lr["ics_sequence"] or 0) + 1
        tx_exec(
            cur, "UPDATE interview SET status = 'cancelled', ics_sequence = %s WHERE id = %s",
            [new_sequence, interview_id],
        )

    panel_rows = query(
        """SELECT u.email FROM interview_panel ip JOIN app_user u ON u.id = ip.interviewer_id
           WHERE ip.interview_id = %s""",
        [interview_id],
    ) or []
    recruiter_id = _owning_recruiter_id(req_id)
    recruiter_row = recruiter_id and query_one("SELECT email FROM app_user WHERE id = %s", [recruiter_id])
    hm_row = ctx.get("hiring_manager_id") and query_one(
        "SELECT email FROM app_user WHERE id = %s", [ctx["hiring_manager_id"]]
    )
    hrbp_row = query_one(
        """SELECT h.email FROM requisition r
           JOIN hrbp h ON LOWER(h.email) = LOWER(r.hrbp_email) AND h.is_active = true
           WHERE r.id = %s""",
        [req_id],
    )

    seen_lower, cc_emails = set(), []
    for e in (
        [r["email"] for r in panel_rows if r.get("email")]
        + [x["email"] for x in (recruiter_row, hm_row, hrbp_row) if x and x.get("email")]
    ):
        if e and e.lower() not in seen_lower:
            seen_lower.add(e.lower())
            cc_emails.append(e)

    round_row = query_one("SELECT name FROM round_config WHERE id = %s", [iv["round_config_id"]])
    scheduled_str = (
        connectors.to_ist(iv["scheduled_at"]).strftime("%A, %d %B %Y at %I:%M %p IST")
        if iv["scheduled_at"] else "—"
    )
    email_html = _build_cancellation_html(
        candidate_name=ctx["candidate_name"], job_title=ctx["job_title"], company=ctx.get("company"),
        round_name=round_row and round_row.get("name"), scheduled_str=scheduled_str, reason=body.reason,
    )
    subject = f"Interview Cancelled – {ctx['job_title']}"

    notified = []
    for e in ([ctx["candidate_email"]] if ctx.get("candidate_email") else []) + cc_emails:
        try:
            connectors.send_email(e, subject, "Your interview has been cancelled.", html=email_html)
            notified.append(e)
        except Exception as exc:
            print(f"[scheduling] cancellation email to {e} failed: {exc}")

    # Actually remove the meeting from calendars, not just the DB status --
    # otherwise it sits on everyone's calendar looking live forever. Two
    # independent, best-effort paths since an interview may have landed on a
    # calendar either way: (1) delete the real Google Calendar event if one
    # was created (interview.gcal_event_id), and (2) email a METHOD:CANCEL
    # .ics on the same UID so any client that added the event straight from
    # the original invite email (Gmail's "Events from Gmail", Outlook, etc.)
    # removes it too. Neither failure should block the cancellation itself.
    if iv.get("gcal_event_id"):
        try:
            google_calendar.delete_event(iv["gcal_event_id"])
        except Exception as exc:
            print(f"[scheduling] gcal event delete failed: {exc}")
    if iv["scheduled_at"]:
        try:
            connectors.send_calendar_cancellation(
                candidate_email=ctx.get("candidate_email") or "",
                panel_emails=cc_emails,
                start_time=iv["scheduled_at"],
                duration_min=iv.get("duration_min") or 45,
                candidate_name=ctx["candidate_name"], job_title=ctx["job_title"],
                uid=iv.get("calendar_uid"), sequence=new_sequence,
            )
        except Exception as exc:
            print(f"[scheduling] calendar cancellation send failed: {exc}")

    log_activity(
        "interview", "interview_cancelled",
        entity_id=interview_id, application_id=iv["application_id"], requisition_id=req_id,
        actor_id=user["sub"], actor_role=user["role"],
        detail={"reason": body.reason, "was_duplicate": is_duplicate, "notified": notified},
    )
    if recruiter_id and recruiter_id != user["sub"]:
        notify(
            recruiter_id, "interview_cancelled",
            f"{ctx.get('candidate_name')}'s interview was cancelled",
            body=f"{(round_row and round_row.get('name')) or 'Interview'} for {ctx.get('job_title')} was cancelled.",
            is_actionable=False, requisition_id=req_id, application_id=iv["application_id"],
        )

    return {"cancelled": True, "notified": notified}
