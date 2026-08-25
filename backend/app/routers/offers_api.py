"""
Offers & Approvals API — Feature 5.

Approval flow (sequential per requisition chain)
-------------------------------------------------
 Create offer   → status=pending_approval, current_step=1
                  → email step-1 approver ("action required")

 Approve step N → if N < total: current_step=N+1
                               → email next approver
                  if N = total: status=approved
                               → push_offer_to_darwin()
                               → status=sent_to_darwinbox
                               → application→offered
                  → audit email (recruiter + all TA managers) on EACH step

 Reject step N  → status=revising, revise_note=reason
                  application stays at offer_approval
                  → email recruiter + TA managers

 Resubmit       → restart chain (delete + recreate pending steps)
                  current_step=1, status=pending_approval
                  → email step-1 approver again

 On Hold        → offer status=on_hold, application→on_hold
 Cancel         → offer status=cancelled, application→rejected

Role-based visibility (server-enforced)
---------------------------------------
 recruiter           — offers for their requisitions only
 ta_manager / admin  — all offers + full history
 hiring_manager      — offers for reqs they are HM of
 any authenticated   — if they are in a pending step, they see that offer
"""
import json
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from ..db import query, query_one, transaction, tx_exec
from ..auth_utils import get_current_user
from ..services.connectors import send_email, push_offer_to_darwin, to_ist
from ..services.email_templates import render_template
from ..services.activity_log import log_activity

router = APIRouter(prefix="/api", tags=["offers"])

# ── Pydantic models ────────────────────────────────────────────────────────────

class ApproverStepIn(BaseModel):
    """Single step in an approval chain — used by both templates and per-req chains."""
    approver_id: str
    sla_days: int = 2


class ApproverChainIn(BaseModel):
    """Accepts either the legacy flat list (approvers) or the new per-step form (steps)."""
    approvers: list[str] = []          # legacy: ordered UUIDs, default sla_days=2
    steps: list[ApproverStepIn] = []   # new: UUIDs + per-step SLA

    def effective_steps(self) -> list[ApproverStepIn]:
        """Return a normalised list of ApproverStepIn regardless of input form."""
        if self.steps:
            return self.steps
        return [ApproverStepIn(approver_id=uid, sla_days=2) for uid in self.approvers]


class CreateOfferIn(BaseModel):
    application_id: str
    designation: str
    joining_date: str           # ISO date  "YYYY-MM-DD"
    fixed_ctc: Optional[float]   = None
    variable_ctc: Optional[float] = None
    bonus_ctc: Optional[float]   = None
    notes: Optional[str]         = None


class EditOfferIn(BaseModel):
    designation: Optional[str]   = None
    joining_date: Optional[str]  = None
    fixed_ctc: Optional[float]   = None
    variable_ctc: Optional[float] = None
    bonus_ctc: Optional[float]   = None
    notes: Optional[str]         = None


class ApproveIn(BaseModel):
    notes: Optional[str] = None


class RejectIn(BaseModel):
    notes: str    # mandatory — approver must state a reason


class OfferStatusIn(BaseModel):
    status: str   # "on_hold" or "cancelled"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _assert_offer_role(user: dict, offer_row: dict):
    """
    Raise 403 if the calling user cannot see this offer at all.
    Allowed:
      - admin / ta_manager → always
      - recruiter           → must own the requisition
      - hiring_manager      → must be HM for the requisition
      - any                 → if they have any approval step on this offer
    """
    role = user["role"]
    uid  = user["sub"]

    if role in ("admin", "ta_manager"):
        return

    if role == "recruiter":
        ok = query_one(
            "SELECT 1 FROM requisition_recruiter WHERE requisition_id=%s AND recruiter_id=%s",
            [str(offer_row["requisition_id"]), uid],
        )
        if ok:
            return

    if role == "hiring_manager":
        ok = query_one(
            "SELECT 1 FROM requisition WHERE id=%s AND hiring_manager_id=%s",
            [str(offer_row["requisition_id"]), uid],
        )
        if ok:
            return

    # Any role: check if they are in the approval chain for this offer
    step_ok = query_one(
        "SELECT 1 FROM offer_approval_step WHERE offer_id=%s AND approver_id=%s",
        [str(offer_row["id"]), uid],
    )
    if step_ok:
        return

    raise HTTPException(403, "Not authorised to view this offer")


def _assert_recruiter_owns_req(user: dict, requisition_id) -> None:
    """
    Mutation endpoints (create/edit/resubmit) are already restricted to
    recruiter/ta_manager/admin — this narrows the recruiter case further to
    requisitions they're actually assigned to, matching the read-side
    ownership check in _assert_offer_role. ta_manager/admin are unrestricted,
    same as before.
    """
    if user["role"] != "recruiter":
        return
    if not query_one(
        "SELECT 1 FROM requisition_recruiter WHERE requisition_id=%s AND recruiter_id=%s",
        [str(requisition_id), user["sub"]],
    ):
        raise HTTPException(403, "Not authorised to act on offers for this requisition")


def _total_ctc(fixed, variable, bonus):
    return round((fixed or 0) + (variable or 0) + (bonus or 0), 2)


def _fmt_inr(val) -> str:
    if val is None:
        return "—"
    try:
        return f"₹{int(val):,}"
    except Exception:
        return str(val)


_OFFER_TEMPLATE_META = {
    "offer_awaiting_approval":  ("Offer Approval<br>Needed.", "An offer is awaiting your approval."),
    "offer_step_approved":      ("Offer Step<br>Approved.", "An offer has moved forward in the approval chain — no action needed."),
    "offer_rejected":           ("Offer<br>Rejected.", "An offer was rejected and needs your attention."),
    "offer_approved_darwinbox": ("Offer Fully<br>Approved.", "The offer has cleared every approval step and been sent to Darwinbox."),
}


def _offer_detail_cells(values: dict) -> list[tuple[str, str]]:
    """Best-effort 2x2 card from whatever fields this particular offer
    template's values dict actually carries -- the 4 built-in offer
    templates each populate a different subset (see email_templates.py)."""
    cells = [
        ("Candidate", values.get("candidate_name") or ""),
        ("Role", values.get("job_title") or ""),
    ]
    if values.get("designation"):
        cells.append(("Designation", values["designation"]))
    if values.get("total_ctc"):
        cells.append(("Total CTC", values["total_ctc"]))
    if len(cells) < 4 and values.get("step_num") and values.get("total_steps"):
        cells.append(("Step", f"{values['step_num']} of {values['total_steps']}"))
    return cells[:4]


def _send_offer_email(
    template_key: str,
    values: dict,
    to_emails: list[str],
    req_id: str | None = None,
    actor: dict | None = None,
) -> bool:
    """Never crashes the approval workflow -- but unlike the old fire-and-forget
    version, returns whether every recipient was actually emailed, so callers
    can return/log a real notified flag instead of a blind success."""
    if not to_emails:
        return False
    try:
        from ..services.connectors import resolve_global_placeholders
        from ..services.email_layout import build_branded_email
        globals_ = resolve_global_placeholders(req_id=req_id, actor=actor)
        reply_to = globals_.get("recruiter_email") or None
        subject, body = render_template(template_key, values, req_id=req_id, actor=actor)
        tenant_id = (actor or {}).get("tenant_id")
        if not tenant_id and req_id:
            req_row = query_one("SELECT tenant_id FROM requisition WHERE id=%s", [req_id])
            tenant_id = (req_row or {}).get("tenant_id")

        hero_title_html, hero_subtitle = _OFFER_TEMPLATE_META.get(
            template_key, ("Offer<br>Update.", "There's an update on an offer."),
        )
        html_body = build_branded_email(
            eyebrow="Application Tracking System",
            hero_title_html=hero_title_html,
            hero_subtitle=hero_subtitle,
            detail_cells=_offer_detail_cells(values),
            about_text=body,
            about_heading=None,
            cta_label=None, cta_link=None,
        )

        all_sent = True
        for addr in to_emails:
            try:
                send_email(addr, subject, body, html=html_body, reply_to=reply_to, tenant_id=tenant_id)
            except Exception as exc:
                all_sent = False
                print(f"[offers] WARNING: email to {addr} failed ({template_key}): {exc}")
        return all_sent
    except Exception as exc:
        print(f"[offers] WARNING: render_template({template_key}) failed — notification NOT sent: {exc}")
        return False


def _ta_manager_emails() -> list[str]:
    rows = query(
        "SELECT email FROM app_user WHERE role='ta_manager' AND is_active=TRUE",
        [],
    )
    return [r["email"] for r in (rows or []) if r.get("email")]


def _recruiter_email(submitted_by: str) -> Optional[str]:
    row = query_one("SELECT email FROM app_user WHERE id=%s", [submitted_by])
    return row["email"] if row else None


def _approver_email(approver_id: str) -> Optional[str]:
    row = query_one("SELECT email, full_name FROM app_user WHERE id=%s", [approver_id])
    return (row["email"], row["full_name"]) if row else (None, "—")


def _create_pending_steps(cur, offer_id: str, requisition_id: str) -> int:
    """
    Delete ALL existing steps for this offer and recreate them from the
    current req_offer_approver chain.  Returns total step count.
    Per-step sla_days is copied from req_offer_approver (default 2 if NULL).

    Called on initial create (no prior steps) and on resubmit (need clean slate so
    previously-approved/rejected history rows don't collide with new pending rows).

    Runs on the caller's transaction cursor -- this used to auto-commit each
    query() independently, so a failure partway through recreating the chain
    (e.g. after 2 of 3 approver INSERTs) could leave an offer either mid-way
    between old and new steps, or with no chain at all after the DELETE
    while still being 'pending_approval'. Now atomic with the offer/
    application writes around it.
    """
    tx_exec(cur, "DELETE FROM offer_approval_step WHERE offer_id=%s", [offer_id])
    chain = tx_exec(
        cur,
        """SELECT approver_id, sequence, COALESCE(sla_days, 2) AS sla_days
           FROM req_offer_approver
           WHERE requisition_id=%s ORDER BY sequence""",
        [requisition_id],
    )
    for step in (chain or []):
        tx_exec(
            cur,
            """INSERT INTO offer_approval_step
                   (offer_id, approver_id, sequence, status, sla_days)
               VALUES (%s, %s, %s, 'pending', %s)""",
            [offer_id, str(step["approver_id"]), step["sequence"],
             int(step.get("sla_days") or 2)],
        )
    return len(chain or [])


# ── Approval chain management ──────────────────────────────────────────────────

@router.get("/requisitions/{req_id}/offer-approvers")
def get_offer_approvers(req_id: str, user: dict = Depends(get_current_user)):
    """Return the ordered approval chain for a requisition (with user names)."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    if not query_one("SELECT id FROM requisition WHERE id=%s", [req_id]):
        raise HTTPException(404, "Requisition not found")
    rows = query(
        """SELECT roa.sequence, roa.approver_id,
                  COALESCE(roa.sla_days, 2) AS sla_days,
                  u.full_name, u.email, u.role
           FROM req_offer_approver roa
           JOIN app_user u ON u.id = roa.approver_id
           WHERE roa.requisition_id = %s
           ORDER BY roa.sequence""",
        [req_id],
    )
    return rows or []


@router.put("/requisitions/{req_id}/offer-approvers")
def set_offer_approvers(
    req_id: str,
    body: ApproverChainIn,
    user: dict = Depends(get_current_user),
):
    """
    Replace the entire approval chain for a requisition.
    Sending an empty list removes the chain.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    if not query_one("SELECT id FROM requisition WHERE id=%s", [req_id]):
        raise HTTPException(404, "Requisition not found")

    steps = body.effective_steps()
    if not steps:
        # Clearing the chain is allowed (empty list)
        query("DELETE FROM req_offer_approver WHERE requisition_id=%s", [req_id], fetch=False)
        return {"ok": True, "total_steps": 0}

    # Validate all approver IDs exist
    for s in steps:
        if not query_one("SELECT id FROM app_user WHERE id=%s AND is_active=TRUE", [s.approver_id]):
            raise HTTPException(400, f"User {s.approver_id} not found or inactive")

    # Replace chain (delete + re-insert to preserve ordering cleanly)
    query("DELETE FROM req_offer_approver WHERE requisition_id=%s", [req_id], fetch=False)
    for i, s in enumerate(steps, start=1):
        query(
            """INSERT INTO req_offer_approver (requisition_id, approver_id, sequence, sla_days)
               VALUES (%s, %s, %s, %s)""",
            [req_id, s.approver_id, i, max(1, s.sla_days)],
            fetch=False,
        )
    return {"ok": True, "total_steps": len(steps)}


# ── Create offer ───────────────────────────────────────────────────────────────

@router.post("/offers", status_code=201)
def create_offer(body: CreateOfferIn, user: dict = Depends(get_current_user)):
    """
    Create an offer for a selected candidate and kick off the approval chain.
    Application must be at status='documentation' (or 'interview' for direct offers).
    Requisition must have at least one approver in its chain.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    app_row = query_one(
        """SELECT a.id, a.status, a.requisition_id,
                  c.full_name AS candidate_name, c.email AS candidate_email,
                  r.title AS job_title
           FROM application a
           JOIN candidate   c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE a.id = %s""",
        [body.application_id],
    )
    if not app_row:
        raise HTTPException(404, "Application not found")
    _assert_recruiter_owns_req(user, app_row["requisition_id"])
    if app_row["status"] not in (
        "documentation", "interview",                           # current stage names
        "hr_round", "offer_approval", "hm_screening", "selected",  # legacy
    ):
        raise HTTPException(400, f"Application must be at documentation or interview stage (current: {app_row['status']})")

    # Check req has a chain
    chain = query(
        "SELECT approver_id, sequence FROM req_offer_approver WHERE requisition_id=%s ORDER BY sequence",
        [str(app_row["requisition_id"])],
    )
    if not chain:
        raise HTTPException(400, "This requisition has no offer approval chain. Add approvers first.")

    # Prevent duplicate offers
    existing = query_one(
        "SELECT id, status FROM offer WHERE application_id=%s", [body.application_id]
    )
    if existing and existing["status"] not in ("cancelled",):
        raise HTTPException(409, f"An active offer already exists for this application (status: {existing['status']})")

    total = _total_ctc(body.fixed_ctc, body.variable_ctc, body.bonus_ctc)

    # Offer INSERT, notes, approval-step creation, application status, and
    # stage_event now commit atomically -- previously five independently
    # auto-committing statements, so a failure partway (most plausibly inside
    # _create_pending_steps' per-step INSERT loop) could leave an offer
    # sitting at 'pending_approval' with no approval chain to ever act on.
    with transaction() as cur:
        offer_row = tx_exec(
            cur,
            """INSERT INTO offer
               (application_id, designation, joining_date, fixed_ctc, variable_ctc,
                bonus_ctc, total_ctc, status, current_step, submitted_by, submitted_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending_approval', 1, %s, now(), now())
               RETURNING id""",
            [body.application_id, body.designation, body.joining_date,
             body.fixed_ctc, body.variable_ctc, body.bonus_ctc, total,
             user["sub"]],
        )
        offer_id = str(offer_row[0]["id"])

        if body.notes:
            tx_exec(cur, "UPDATE offer SET notes=%s WHERE id=%s", [body.notes, offer_id])

        _create_pending_steps(cur, offer_id, str(app_row["requisition_id"]))

        old_status = app_row["status"]
        if old_status != "documentation":
            tx_exec(
                cur,
                "UPDATE application SET status='documentation' WHERE id=%s",
                [body.application_id],
            )
            tx_exec(
                cur,
                "INSERT INTO stage_event (application_id, from_status, to_status, actor_id, note) VALUES (%s,%s,'documentation',%s,'Offer created')",
                [body.application_id, old_status, user["sub"]],
            )

    log_activity(
        "offer", "offer_created",
        entity_id=offer_id, application_id=body.application_id, requisition_id=app_row["requisition_id"],
        actor_id=user["sub"], actor_role=user["role"],
        detail={"designation": body.designation, "total_ctc": float(total) if total is not None else None},
    )

    # Notify step-1 approver
    step1_user_id = str(chain[0]["approver_id"])
    approver_email, approver_name = _approver_email(step1_user_id)
    approver_notified = False
    if approver_email:
        approver_notified = _send_offer_email("offer_awaiting_approval", {
            "candidate_name": app_row["candidate_name"],
            "job_title":      app_row["job_title"],
            "designation":    body.designation,
            "approver_name":  approver_name,
            "total_ctc":      _fmt_inr(total),
            "joining_date":   body.joining_date,
            "step_num":       "1",
            "total_steps":    str(len(chain)),
        }, [approver_email], req_id=str(app_row["requisition_id"]), actor=user)
    log_activity(
        "offer", "offer_approver_notified",
        entity_id=offer_id, application_id=body.application_id, requisition_id=app_row["requisition_id"],
        actor_id=user["sub"], actor_role=user["role"],
        detail={"step_num": 1, "approver_id": step1_user_id, "notified": approver_notified},
    )

    return {"offer_id": offer_id, "status": "pending_approval", "total_steps": len(chain),
            "approver_notified": approver_notified}


# ── List offers (role-scoped) ──────────────────────────────────────────────────

@router.get("/offers")
def list_offers(
    response: Response,
    status: Optional[str] = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """
    Return offers the caller is authorised to see, with chain progress.
    - admin / ta_manager: all offers
    - recruiter:          offers on their requisitions
    - hiring_manager:     offers on reqs where they are HM
    - any other role:     offers where they have any approval step
    """
    role = user["role"]
    uid  = user["sub"]
    join_parts, where_parts, params = [], [], []

    where_parts.append("r.tenant_id = %s")
    params.append(user.get("tenant_id"))

    if role == "recruiter":
        join_parts.append(
            "JOIN requisition_recruiter rr_s ON rr_s.requisition_id = r.id AND rr_s.recruiter_id = %s"
        )
        params.append(uid)
    elif role == "hiring_manager":
        where_parts.append("r.hiring_manager_id = %s")
        params.append(uid)
    elif role not in ("admin", "ta_manager"):
        # Any role — show offers where they are an approver
        join_parts.append(
            "JOIN offer_approval_step my_step ON my_step.offer_id = o.id AND my_step.approver_id = %s"
        )
        params.append(uid)

    if status:
        where_parts.append("o.status = %s")
        params.append(status)

    join_sql  = "\n    ".join(join_parts)
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    total = query_one(
        f"""SELECT COUNT(*) AS n
            FROM offer o
            JOIN application  a   ON a.id  = o.application_id
            JOIN candidate    c   ON c.id  = a.candidate_id
            JOIN requisition  r   ON r.id  = a.requisition_id
            LEFT JOIN app_user sub ON sub.id = o.submitted_by
            {join_sql}
            {where_sql}""",
        params,
    )["n"]
    response.headers["X-Total-Count"] = str(total)

    rows = query(
        f"""
        SELECT
            o.id,  o.status, o.current_step, o.designation,
            o.fixed_ctc, o.variable_ctc, o.bonus_ctc, o.total_ctc,
            o.joining_date, o.notes, o.revise_note,
            o.darwin_ref, o.submitted_at, o.updated_at,
            c.full_name  AS candidate_name,
            c.email      AS candidate_email,
            r.title      AS job_title,
            r.id         AS requisition_id,
            a.id         AS application_id,
            a.status     AS application_status,
            sub.full_name AS submitted_by_name,
            (SELECT COUNT(*) FROM req_offer_approver roa
             WHERE roa.requisition_id = r.id) AS total_steps,
            (SELECT u2.full_name
             FROM req_offer_approver roa2
             JOIN app_user u2 ON u2.id = roa2.approver_id
             WHERE roa2.requisition_id = r.id AND roa2.sequence = o.current_step
             LIMIT 1) AS pending_approver_name
        FROM offer o
        JOIN application  a   ON a.id  = o.application_id
        JOIN candidate    c   ON c.id  = a.candidate_id
        JOIN requisition  r   ON r.id  = a.requisition_id
        LEFT JOIN app_user sub ON sub.id = o.submitted_by
        {join_sql}
        {where_sql}
        ORDER BY o.updated_at DESC NULLS LAST, o.submitted_at DESC
        LIMIT %s OFFSET %s
        """,
        params + [limit, offset],
    )
    return rows or []


# ── Offer detail + full step log ───────────────────────────────────────────────

@router.get("/offers/{offer_id}")
def get_offer(offer_id: str, user: dict = Depends(get_current_user)):
    """Return full offer detail with approval step history."""
    offer = query_one(
        """SELECT o.*, a.status AS application_status,
                  c.full_name  AS candidate_name,
                  c.email      AS candidate_email,
                  r.title      AS job_title,
                  r.id         AS requisition_id,
                  sub.full_name AS submitted_by_name
           FROM offer o
           JOIN application  a   ON a.id  = o.application_id
           JOIN candidate    c   ON c.id  = a.candidate_id
           JOIN requisition  r   ON r.id  = a.requisition_id
           LEFT JOIN app_user sub ON sub.id = o.submitted_by
           WHERE o.id = %s""",
        [offer_id],
    )
    if not offer:
        raise HTTPException(404, "Offer not found")

    _assert_offer_role(user, offer)

    # Approval chain definition (current state on requisition)
    chain = query(
        """SELECT roa.sequence, u.id AS approver_id, u.full_name, u.role,
                  COALESCE(roa.sla_days, 2) AS sla_days
           FROM req_offer_approver roa
           JOIN app_user u ON u.id = roa.approver_id
           WHERE roa.requisition_id = %s ORDER BY roa.sequence""",
        [str(offer["requisition_id"])],
    )

    # Approval step log (history) — includes per-step sla_days captured at offer creation
    steps = query(
        """SELECT oas.sequence, oas.status, oas.notes, oas.acted_at,
                  COALESCE(oas.sla_days, 2) AS sla_days,
                  u.full_name AS approver_name, u.role AS approver_role,
                  u.id AS approver_id
           FROM offer_approval_step oas
           JOIN app_user u ON u.id = oas.approver_id
           WHERE oas.offer_id = %s ORDER BY oas.sequence""",
        [offer_id],
    )

    return {
        **{k: v for k, v in offer.items()},
        "chain":       chain or [],
        "steps":       steps or [],
        "total_steps": len(chain or []),
    }


# ── Edit offer (only while in 'revising' state) ────────────────────────────────

@router.put("/offers/{offer_id}")
def edit_offer(offer_id: str, body: EditOfferIn, user: dict = Depends(get_current_user)):
    """Update offer fields. Only allowed when status='revising'."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    offer = query_one(
        """SELECT o.id, o.status, o.fixed_ctc, o.variable_ctc, o.bonus_ctc, a.requisition_id
           FROM offer o JOIN application a ON a.id = o.application_id
           WHERE o.id=%s""",
        [offer_id],
    )
    if not offer:
        raise HTTPException(404, "Offer not found")
    _assert_recruiter_owns_req(user, offer["requisition_id"])
    if offer["status"] != "revising":
        raise HTTPException(400, "Offer can only be edited while in 'revising' state")

    sets, vals = [], []
    if body.designation  is not None: sets.append("designation=%s");   vals.append(body.designation)
    if body.joining_date is not None: sets.append("joining_date=%s");  vals.append(body.joining_date)
    if body.fixed_ctc    is not None: sets.append("fixed_ctc=%s");     vals.append(body.fixed_ctc)
    if body.variable_ctc is not None: sets.append("variable_ctc=%s");  vals.append(body.variable_ctc)
    if body.bonus_ctc    is not None: sets.append("bonus_ctc=%s");     vals.append(body.bonus_ctc)
    if body.notes        is not None: sets.append("notes=%s");         vals.append(body.notes)

    if sets:
        # Recompute total using latest values
        new_fixed    = body.fixed_ctc    if body.fixed_ctc    is not None else (offer["fixed_ctc"]    or 0)
        new_variable = body.variable_ctc if body.variable_ctc is not None else (offer["variable_ctc"] or 0)
        new_bonus    = body.bonus_ctc    if body.bonus_ctc    is not None else (offer["bonus_ctc"]    or 0)
        sets.append("total_ctc=%s");  vals.append(_total_ctc(new_fixed, new_variable, new_bonus))
        sets.append("updated_at=now()")
        vals.append(offer_id)
        query(f"UPDATE offer SET {', '.join(sets)} WHERE id=%s", vals, fetch=False)

    return {"ok": True}


# ── Resubmit offer (restart chain after rejection) ────────────────────────────

@router.post("/offers/{offer_id}/resubmit")
def resubmit_offer(offer_id: str, user: dict = Depends(get_current_user)):
    """
    Resubmit a 'revising' offer: restarts the approval chain from step 1.
    Any previous pending steps are deleted and recreated.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    offer = query_one(
        """SELECT o.id, o.status, o.application_id, o.designation,
                  o.total_ctc, o.joining_date,
                  a.requisition_id,
                  c.full_name AS candidate_name,
                  r.title     AS job_title
           FROM offer o
           JOIN application a ON a.id = o.application_id
           JOIN candidate   c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE o.id = %s""",
        [offer_id],
    )
    if not offer:
        raise HTTPException(404, "Offer not found")
    _assert_recruiter_owns_req(user, offer["requisition_id"])
    if offer["status"] != "revising":
        raise HTTPException(400, "Only 'revising' offers can be resubmitted")

    req_id = str(offer["requisition_id"])
    chain  = query(
        "SELECT approver_id, sequence FROM req_offer_approver WHERE requisition_id=%s ORDER BY sequence",
        [req_id],
    )
    if not chain:
        raise HTTPException(400, "No approval chain on this requisition")

    # Same atomicity concern as create_offer -- recreating the approval chain
    # and resetting the offer/application status must commit together.
    with transaction() as cur:
        total_steps = _create_pending_steps(cur, offer_id, req_id)
        tx_exec(
            cur,
            "UPDATE offer SET status='pending_approval', current_step=1, revise_note=NULL, updated_at=now() WHERE id=%s",
            [offer_id],
        )
        tx_exec(
            cur,
            "UPDATE application SET status='documentation' WHERE id=%s",
            [str(offer["application_id"])],
        )

    # Notify first approver
    approver_email, approver_name = _approver_email(str(chain[0]["approver_id"]))
    approver_notified = False
    if approver_email:
        approver_notified = _send_offer_email("offer_awaiting_approval", {
            "candidate_name": offer["candidate_name"],
            "job_title":      offer["job_title"],
            "designation":    offer["designation"] or "—",
            "approver_name":  approver_name,
            "total_ctc":      _fmt_inr(offer["total_ctc"]),
            "joining_date":   str(offer["joining_date"]) if offer["joining_date"] else "—",
            "step_num":       "1",
            "total_steps":    str(total_steps),
        }, [approver_email], req_id=str(offer["requisition_id"]), actor=user)
    log_activity(
        "offer", "offer_approver_notified",
        entity_id=offer_id, application_id=str(offer["application_id"]), requisition_id=offer["requisition_id"],
        actor_id=user["sub"], actor_role=user["role"],
        detail={"step_num": 1, "approver_id": str(chain[0]["approver_id"]), "notified": approver_notified, "resubmit": True},
    )

    return {"ok": True, "status": "pending_approval", "total_steps": total_steps,
            "approver_notified": approver_notified}


@router.post("/offers/{offer_id}/resend-notice")
def resend_approval_notice(offer_id: str, user: dict = Depends(get_current_user)):
    """Re-sends the 'action required' email to whichever approver the offer
    is currently sitting with -- for the case where that step's notification
    silently failed to send and the offer has been waiting unnoticed."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    offer = query_one(
        """SELECT o.id, o.status, o.current_step, o.application_id,
                  o.designation, o.total_ctc, o.joining_date,
                  a.requisition_id,
                  c.full_name AS candidate_name,
                  r.title     AS job_title
           FROM offer o
           JOIN application a ON a.id = o.application_id
           JOIN candidate   c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE o.id = %s""",
        [offer_id],
    )
    if not offer:
        raise HTTPException(404, "Offer not found")
    _assert_recruiter_owns_req(user, offer["requisition_id"])
    if offer["status"] != "pending_approval":
        raise HTTPException(400, f"Offer is not pending approval (status: {offer['status']})")

    step_row = query_one(
        "SELECT approver_id FROM offer_approval_step WHERE offer_id=%s AND sequence=%s AND status='pending'",
        [offer_id, offer["current_step"]],
    )
    if not step_row:
        raise HTTPException(400, "No pending approval step found to notify")

    total_steps = query_one(
        "SELECT COUNT(*) AS n FROM offer_approval_step WHERE offer_id=%s", [offer_id]
    )["n"]
    approver_email, approver_name = _approver_email(str(step_row["approver_id"]))
    notified = False
    if approver_email:
        notified = _send_offer_email("offer_awaiting_approval", {
            "candidate_name": offer["candidate_name"],
            "job_title":      offer["job_title"],
            "designation":    offer["designation"] or "—",
            "approver_name":  approver_name,
            "total_ctc":      _fmt_inr(offer["total_ctc"]),
            "joining_date":   str(offer["joining_date"]) if offer["joining_date"] else "—",
            "step_num":       str(offer["current_step"]),
            "total_steps":    str(total_steps),
        }, [approver_email], req_id=str(offer["requisition_id"]), actor=user)
    log_activity(
        "offer", "offer_approval_reminder_sent",
        entity_id=offer_id, application_id=str(offer["application_id"]), requisition_id=offer["requisition_id"],
        actor_id=user["sub"], actor_role=user["role"],
        detail={"step_num": offer["current_step"], "approver_id": str(step_row["approver_id"]), "notified": notified},
    )
    return {"ok": True, "notified": notified}


# ── Approve current step ───────────────────────────────────────────────────────

@router.post("/offers/{offer_id}/approve")
def approve_offer_step(offer_id: str, body: ApproveIn, user: dict = Depends(get_current_user)):
    """
    Approve the current pending step of the offer.
    Only the current-step approver (or admin) may act.
    On the final step: sets offer to approved → calls Darwinbox stub
    → status=sent_to_darwinbox → application→offered.
    Audit email sent to recruiter + all TA managers after every step.
    """
    offer = query_one(
        """SELECT o.id, o.status, o.current_step, o.application_id,
                  o.designation, o.total_ctc, o.joining_date, o.submitted_by,
                  a.requisition_id,
                  c.full_name AS candidate_name,
                  r.title     AS job_title
           FROM offer o
           JOIN application a ON a.id = o.application_id
           JOIN candidate   c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE o.id = %s""",
        [offer_id],
    )
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer["status"] != "pending_approval":
        raise HTTPException(400, f"Offer is not pending approval (status: {offer['status']})")

    current_step = offer["current_step"]
    uid  = user["sub"]
    role = user["role"]

    # Load the pending step row
    step_row = query_one(
        "SELECT id, approver_id FROM offer_approval_step WHERE offer_id=%s AND sequence=%s AND status='pending'",
        [offer_id, current_step],
    )
    if not step_row:
        raise HTTPException(400, "No pending step found at current position")

    # Server-side enforcement: only the designated approver or admin may act
    if role != "admin" and str(step_row["approver_id"]) != uid:
        raise HTTPException(403, "It is not your turn to approve this offer")

    # Load approver name for audit email
    approver_row = query_one("SELECT full_name FROM app_user WHERE id=%s", [str(step_row["approver_id"])])
    approver_name = approver_row["full_name"] if approver_row else "—"

    # Mark step approved
    query(
        "UPDATE offer_approval_step SET status='approved', notes=%s, acted_at=now() WHERE id=%s",
        [body.notes, str(step_row["id"])],
        fetch=False,
    )

    req_id      = str(offer["requisition_id"])
    # Count against offer_approval_step (the steps created at offer-time / resubmit-time),
    # not req_offer_approver — so chain edits after creation don't affect in-flight approvals.
    total_steps = query_one(
        "SELECT COUNT(*) AS n FROM offer_approval_step WHERE offer_id=%s",
        [offer_id],
    )["n"]

    now_str = to_ist(datetime.utcnow()).strftime("%d %b %Y %H:%M IST")

    # Audit email to recruiter + TA managers
    audit_to = []
    if offer["submitted_by"]:
        rec_email = _recruiter_email(str(offer["submitted_by"]))
        if rec_email:
            audit_to.append(rec_email)
    audit_to += _ta_manager_emails()
    audit_to  = list(dict.fromkeys(audit_to))  # deduplicate, preserve order

    audit_notified = _send_offer_email("offer_step_approved", {
        "candidate_name": offer["candidate_name"],
        "job_title":      offer["job_title"],
        "approver_name":  approver_name,
        "step_num":       str(current_step),
        "total_steps":    str(total_steps),
        "approved_at":    now_str,
        "notes":          body.notes or "—",
    }, audit_to, req_id=req_id, actor=user)
    log_activity(
        "offer", "offer_step_approved_notice_sent",
        entity_id=offer_id, application_id=str(offer["application_id"]), requisition_id=offer["requisition_id"],
        actor_id=uid, actor_role=role,
        detail={"step_num": current_step, "notified": audit_notified, "to": audit_to},
    )

    # ── Final step? ────────────────────────────────────────────────────────────
    if current_step >= total_steps:
        # All steps approved → Darwinbox handoff
        darwin_result = push_offer_to_darwin({
            "id":          str(offer["id"]),
            "designation": offer["designation"],
            "total_ctc":   float(offer["total_ctc"] or 0),
            "joining_date":str(offer["joining_date"]) if offer["joining_date"] else None,
            "candidate":   offer["candidate_name"],
            "job_title":   offer["job_title"],
        })
        darwin_ref = darwin_result.get("darwin_ref", "—")

        query(
            """UPDATE offer SET status='sent_to_darwinbox', darwin_ref=%s, updated_at=now()
               WHERE id=%s""",
            [darwin_ref, offer_id],
            fetch=False,
        )
        query(
            "UPDATE application SET status='offered' WHERE id=%s",
            [str(offer["application_id"])],
            fetch=False,
        )
        query(
            """INSERT INTO stage_event (application_id, from_status, to_status, actor_id, note)
               VALUES (%s,'documentation','offered',%s,'Offer fully approved — sent to Darwinbox')""",
            [str(offer["application_id"]), uid],
            fetch=False,
        )
        try:
            from .hiring_plan_api import sync_plan_on_advance as _sync_plan
            _sync_plan(str(offer["application_id"]), "offered", "documentation", req_id)
        except Exception as _sp_exc:
            print(f"[offers] sync_plan_on_advance failed for offer {offer_id}: {_sp_exc}")

        darwinbox_notified = _send_offer_email("offer_approved_darwinbox", {
            "candidate_name": offer["candidate_name"],
            "job_title":      offer["job_title"],
            "designation":    offer["designation"] or "—",
            "total_ctc":      _fmt_inr(offer["total_ctc"]),
            "joining_date":   str(offer["joining_date"]) if offer["joining_date"] else "—",
            "darwin_ref":     darwin_ref,
            "approved_at":    now_str,
        }, audit_to, req_id=req_id, actor=user)
        log_activity(
            "offer", "offer_darwinbox_notice_sent",
            entity_id=offer_id, application_id=str(offer["application_id"]), requisition_id=offer["requisition_id"],
            actor_id=uid, actor_role=role,
            detail={"notified": darwinbox_notified, "to": audit_to, "darwin_ref": darwin_ref},
        )

        return {"ok": True, "status": "sent_to_darwinbox", "darwin_ref": darwin_ref,
                "audit_notified": audit_notified, "darwinbox_notice_notified": darwinbox_notified}

    # ── More steps remain ─────────────────────────────────────────────────────
    next_step  = current_step + 1
    query(
        "UPDATE offer SET current_step=%s, updated_at=now() WHERE id=%s",
        [next_step, offer_id],
        fetch=False,
    )

    # Notify next approver — read from offer_approval_step (same source as total_steps)
    next_approver_row = query_one(
        "SELECT approver_id FROM offer_approval_step WHERE offer_id=%s AND sequence=%s",
        [offer_id, next_step],
    )
    next_approver_notified = False
    if next_approver_row:
        next_email, next_name = _approver_email(str(next_approver_row["approver_id"]))
        if next_email:
            next_approver_notified = _send_offer_email("offer_awaiting_approval", {
                "candidate_name": offer["candidate_name"],
                "job_title":      offer["job_title"],
                "designation":    offer["designation"] or "—",
                "approver_name":  next_name,
                "total_ctc":      _fmt_inr(offer["total_ctc"]),
                "joining_date":   str(offer["joining_date"]) if offer["joining_date"] else "—",
                "step_num":       str(next_step),
                "total_steps":    str(total_steps),
            }, [next_email], req_id=req_id, actor=user)
        log_activity(
            "offer", "offer_approver_notified",
            entity_id=offer_id, application_id=str(offer["application_id"]), requisition_id=offer["requisition_id"],
            actor_id=uid, actor_role=role,
            detail={"step_num": next_step, "approver_id": str(next_approver_row["approver_id"]),
                    "notified": next_approver_notified},
        )

    return {"ok": True, "status": "pending_approval", "current_step": next_step, "total_steps": int(total_steps),
            "audit_notified": audit_notified, "approver_notified": next_approver_notified}


# ── Reject current step ────────────────────────────────────────────────────────

@router.post("/offers/{offer_id}/reject")
def reject_offer_step(offer_id: str, body: RejectIn, user: dict = Depends(get_current_user)):
    """
    Reject the current pending step. Offer moves to 'revising'.
    The recruiter must edit and resubmit to restart the chain.
    Audit email sent to recruiter + all TA managers.
    """
    if not body.notes or not body.notes.strip():
        raise HTTPException(422, "A rejection reason is required.")

    offer = query_one(
        """SELECT o.id, o.status, o.current_step, o.application_id,
                  o.submitted_by,
                  c.full_name AS candidate_name,
                  r.title     AS job_title
           FROM offer o
           JOIN application a ON a.id = o.application_id
           JOIN candidate   c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE o.id = %s""",
        [offer_id],
    )
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer["status"] != "pending_approval":
        raise HTTPException(400, f"Offer is not pending approval (status: {offer['status']})")

    current_step = offer["current_step"]
    uid  = user["sub"]
    role = user["role"]

    step_row = query_one(
        "SELECT id, approver_id FROM offer_approval_step WHERE offer_id=%s AND sequence=%s AND status='pending'",
        [offer_id, current_step],
    )
    if not step_row:
        raise HTTPException(400, "No pending step found at current position")

    if role != "admin" and str(step_row["approver_id"]) != uid:
        raise HTTPException(403, "It is not your turn to act on this offer")

    approver_row = query_one("SELECT full_name FROM app_user WHERE id=%s", [str(step_row["approver_id"])])
    approver_name = approver_row["full_name"] if approver_row else "—"

    # Mark step rejected
    query(
        "UPDATE offer_approval_step SET status='rejected', notes=%s, acted_at=now() WHERE id=%s",
        [body.notes, str(step_row["id"])],
        fetch=False,
    )

    # Offer → revising
    query(
        "UPDATE offer SET status='revising', revise_note=%s, updated_at=now() WHERE id=%s",
        [body.notes, offer_id],
        fetch=False,
    )

    now_str = to_ist(datetime.utcnow()).strftime("%d %b %Y %H:%M IST")

    # Notify recruiter + TA managers
    notify_to = []
    if offer["submitted_by"]:
        rec_email = _recruiter_email(str(offer["submitted_by"]))
        if rec_email:
            notify_to.append(rec_email)
    notify_to += _ta_manager_emails()
    notify_to   = list(dict.fromkeys(notify_to))

    reject_notified = _send_offer_email("offer_rejected", {
        "candidate_name": offer["candidate_name"],
        "job_title":      offer["job_title"],
        "approver_name":  approver_name,
        "step_num":       str(current_step),
        "notes":          body.notes,
        "rejected_at":    now_str,
    }, notify_to, req_id=None, actor=user)
    log_activity(
        "offer", "offer_rejected_notice_sent",
        entity_id=offer_id, application_id=str(offer["application_id"]), requisition_id=None,
        actor_id=uid, actor_role=role,
        detail={"step_num": current_step, "notified": reject_notified, "to": notify_to},
    )

    return {"ok": True, "status": "revising", "notified": reject_notified}


# ── Set on-hold or cancel ──────────────────────────────────────────────────────

@router.patch("/offers/{offer_id}/status")
def update_offer_status(offer_id: str, body: OfferStatusIn, user: dict = Depends(get_current_user)):
    """
    Recruiter or TA manager can set an offer to 'on_hold' or 'cancelled' at any time.
    Both states are reflected back on the application record.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    if body.status not in ("on_hold", "cancelled"):
        raise HTTPException(400, "status must be 'on_hold' or 'cancelled'")

    offer = query_one(
        """SELECT o.id, o.application_id, o.status, a.requisition_id
           FROM offer o JOIN application a ON a.id = o.application_id
           WHERE o.id=%s""",
        [offer_id],
    )
    if not offer:
        raise HTTPException(404, "Offer not found")
    _assert_recruiter_owns_req(user, offer["requisition_id"])
    if offer["status"] in ("sent_to_darwinbox",):
        raise HTTPException(400, "Cannot hold or cancel an offer that has already been sent to Darwinbox")

    app_status = "on_hold" if body.status == "on_hold" else "rejected"

    query(
        "UPDATE offer SET status=%s, updated_at=now() WHERE id=%s",
        [body.status, offer_id],
        fetch=False,
    )
    query(
        "UPDATE application SET status=%s WHERE id=%s",
        [app_status, str(offer["application_id"])],
        fetch=False,
    )
    query(
        """INSERT INTO stage_event (application_id, from_status, to_status, actor_id, note)
           VALUES (%s,%s,%s,%s,%s)""",
        [str(offer["application_id"]), offer["status"], app_status,
         user["sub"], f"Offer set to {body.status}"],
        fetch=False,
    )
    if app_status == "rejected":
        from .pipeline_api import _send_application_rejected_email
        _send_application_rejected_email(str(offer["application_id"]), user)

    return {"ok": True, "status": body.status, "application_status": app_status}
