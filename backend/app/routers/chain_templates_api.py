"""
Named Approval Chain Templates API.

Endpoints
---------
GET    /api/offer-chain-templates              — list active templates + steps (recruiter+)
POST   /api/offer-chain-templates              — create template (Company Admin)
PUT    /api/offer-chain-templates/{id}         — replace template name/steps (Company Admin)
DELETE /api/offer-chain-templates/{id}         — soft-delete (Company Admin)

Templates are reusable: when the TA manager creates a requisition the frontend
copies the template steps into _approverChain (with per-step sla_days) and saves
them to req_offer_approver.  The template itself is never linked to a requisition
in the DB — it is a source-of-truth library, not a live FK.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..module_access import recruiter_has_module

router = APIRouter(prefix="/api/offer-chain-templates", tags=["chain_templates"])


def _require_write(user: dict = Depends(get_current_user)) -> dict:
    role = user.get("role")
    if role in ("admin", "platform_admin", "company_admin"):
        return user
    if role == "recruiter" and recruiter_has_module(user.get("sub"), "chain_templates"):
        return user
    raise HTTPException(403, "Company Admin access required")


def _require_read(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in ("admin", "platform_admin", "company_admin", "ta_manager", "recruiter"):
        raise HTTPException(403, "Not authorised")
    return user


# ── Pydantic models ───────────────────────────────────────────────────────────

class TemplateStepIn(BaseModel):
    approver_id: str
    sla_days: int = 2


class TemplateIn(BaseModel):
    name: str
    description: Optional[str] = None
    steps: list[TemplateStepIn]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_steps(template_id: str) -> list:
    return query(
        """SELECT s.sequence, s.sla_days,
                  u.id AS approver_id, u.full_name, u.role AS approver_role
           FROM offer_chain_template_step s
           JOIN app_user u ON u.id = s.approver_id
           WHERE s.template_id = %s
           ORDER BY s.sequence""",
        [template_id],
    ) or []


def _save_steps(template_id: str, steps: list[TemplateStepIn]) -> None:
    query(
        "DELETE FROM offer_chain_template_step WHERE template_id = %s",
        [template_id], fetch=False,
    )
    for i, step in enumerate(steps, start=1):
        query(
            """INSERT INTO offer_chain_template_step
                   (template_id, sequence, approver_id, sla_days)
               VALUES (%s, %s, %s, %s)""",
            [template_id, i, step.approver_id, max(1, step.sla_days)],
            fetch=False,
        )


def _validate_steps(steps: list[TemplateStepIn], tenant_id: str) -> None:
    if not steps:
        raise HTTPException(400, "At least one approver step is required")
    for s in steps:
        if not query_one(
            "SELECT id FROM app_user WHERE id = %s AND is_active = TRUE AND tenant_id = %s",
            [s.approver_id, tenant_id],
        ):
            raise HTTPException(400, f"Approver {s.approver_id} not found or inactive")
    if len(steps) > 20:
        raise HTTPException(400, "Maximum 20 steps per template")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def list_templates(user: dict = Depends(_require_read)):
    """Return all active chain templates with their steps (including approver names)."""
    templates = query(
        """SELECT id, name, description, created_at, updated_at
           FROM offer_chain_template
           WHERE is_active = TRUE AND tenant_id = %s
           ORDER BY name""",
        [user.get("tenant_id")],
    ) or []
    return [
        {**dict(t), "steps": _load_steps(str(t["id"]))}
        for t in templates
    ]


@router.post("", status_code=201)
def create_template(body: TemplateIn, user: dict = Depends(_require_write)):
    """Create a named approval chain template."""
    tenant_id = user.get("tenant_id")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Template name is required")
    _validate_steps(body.steps, tenant_id)

    # Prevent duplicate names
    if query_one(
        "SELECT id FROM offer_chain_template WHERE tenant_id = %s AND name = %s AND is_active = TRUE",
        [tenant_id, name],
    ):
        raise HTTPException(409, f"A template named '{name}' already exists")

    tmpl = query_one(
        """INSERT INTO offer_chain_template (name, description, created_by, tenant_id)
           VALUES (%s, %s, %s, %s)
           RETURNING id""",
        [name, body.description, user["sub"], tenant_id],
    )
    tmpl_id = str(tmpl["id"])
    _save_steps(tmpl_id, body.steps)
    return {"id": tmpl_id, "ok": True}


@router.put("/{template_id}")
def update_template(
    template_id: str,
    body: TemplateIn,
    user: dict = Depends(_require_write),
):
    """Replace a template's name, description, and steps."""
    tenant_id = user.get("tenant_id")
    if not query_one(
        "SELECT id FROM offer_chain_template WHERE id = %s AND tenant_id = %s AND is_active = TRUE",
        [template_id, tenant_id],
    ):
        raise HTTPException(404, "Template not found")

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Template name is required")
    _validate_steps(body.steps, tenant_id)

    # Prevent name collision with other templates
    clash = query_one(
        "SELECT id FROM offer_chain_template WHERE tenant_id = %s AND name = %s AND is_active = TRUE AND id != %s",
        [tenant_id, name, template_id],
    )
    if clash:
        raise HTTPException(409, f"Another template named '{name}' already exists")

    query(
        """UPDATE offer_chain_template
           SET name = %s, description = %s, updated_at = now()
           WHERE id = %s""",
        [name, body.description, template_id],
        fetch=False,
    )
    _save_steps(template_id, body.steps)
    return {"ok": True}


@router.delete("/{template_id}")
def delete_template(template_id: str, user: dict = Depends(_require_write)):
    """Soft-delete a template (sets is_active = FALSE)."""
    row = query_one(
        """UPDATE offer_chain_template
           SET is_active = FALSE, updated_at = now()
           WHERE id = %s AND tenant_id = %s AND is_active = TRUE
           RETURNING id""",
        [template_id, user.get("tenant_id")],
    )
    if not row:
        raise HTTPException(404, "Template not found")
    return {"ok": True}
