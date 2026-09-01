"""
Onboarding & Employee Master API (ATS spec §13).

Fires on Day-1 (the joining date) -- an independent trigger from
Preboarding (Module 3): staff call POST .../day-one and
POST .../convert-to-employee explicitly, there is no automatic hand-off
from preboarding_case reaching 'ready'/'joined'. employee_master is the
record ATS spec §14's future HRMS sync will read, so its columns are kept
plain and complete rather than abbreviated.

Gated tenant-wide via require_tenant_module -- no per-recruiter delegation
concept, mirroring document_api.py/bgv_api.py/preboarding_api.py and the
other GATED_NAV_MODULES routers.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user
from ..db import query, query_one
from ..module_access import require_tenant_module
from ..services.activity_log import log_activity

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"],
                    dependencies=[Depends(require_tenant_module("onboarding"))])

# See preboarding_api.py's _ACCEPTED_OFFER_STATUSES comment: offer.status
# never actually reaches the schema-legal 'accepted' value through any code
# path today (no candidate-facing accept/decline endpoint exists), so
# 'sent_to_darwinbox' -- the real terminal state offers reach once
# approved -- is treated as "accepted" here too. Not a Darwinbox-specific
# signal despite the name; a tenant may sync to any HRMS via Module 5.
_ACCEPTED_OFFER_STATUSES = ("accepted", "released", "sent_to_darwinbox")

_DAY_ONE_TASKS = [
    ("welcome_letter", "Welcome Letter"),
    ("induction", "Induction"),
    ("policy_acceptance", "Policy Acceptance"),
    ("credential_activation", "Credential Activation"),
]


def _generate_employee_code(tenant_id) -> str:
    """Tenant-scoped prefix+number. Not perfectly race-safe under
    concurrent conversions for the same tenant, but this is a one-at-a-time
    staff action and the UNIQUE(tenant_id, employee_code) constraint would
    reject a genuine collision rather than silently duplicate one."""
    row = query_one("SELECT COUNT(*) AS n FROM employee_master WHERE tenant_id=%s", [tenant_id])
    n = (row["n"] if row else 0) + 1
    while True:
        code = f"EMP{n:05d}"
        if not query_one("SELECT id FROM employee_master WHERE tenant_id=%s AND employee_code=%s", [tenant_id, code]):
            return code
        n += 1


def _recompute_onboarding_case_status(case_id) -> Optional[str]:
    """Derives onboarding_case.status from its onboarding_task rows --
    'completed' once every task is done, else 'day_one' -- and writes it
    back. Never written directly by a task-level update."""
    if not query_one("SELECT id FROM onboarding_case WHERE id=%s", [case_id]):
        return None
    tasks = query("SELECT status FROM onboarding_task WHERE onboarding_case_id=%s", [case_id]) or []
    new_status = "completed" if tasks and all(t["status"] == "done" for t in tasks) else "day_one"
    query("UPDATE onboarding_case SET status=%s WHERE id=%s", [new_status, case_id], fetch=False)
    return new_status


@router.post("/candidates/{candidate_id}/day-one")
def trigger_day_one(candidate_id: str, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    if not query_one("SELECT id FROM candidate WHERE id=%s AND tenant_id=%s", [candidate_id, tenant_id]):
        raise HTTPException(404, "Candidate not found")

    preboarding_ready = query_one(
        "SELECT id FROM preboarding_case WHERE candidate_id=%s AND tenant_id=%s AND status IN ('ready','joined')",
        [candidate_id, tenant_id],
    )
    offer_due = query_one(
        """SELECT o.id FROM offer o JOIN application a ON a.id = o.application_id
           WHERE a.candidate_id = %s AND o.status = ANY(%s)
             AND o.joining_date IS NOT NULL AND o.joining_date <= CURRENT_DATE
           ORDER BY o.created_at DESC LIMIT 1""",
        [candidate_id, list(_ACCEPTED_OFFER_STATUSES)],
    )
    if not preboarding_ready and not offer_due:
        raise HTTPException(409, "Candidate is not ready for Day-1 — preboarding isn't complete and the joining date hasn't arrived")

    if query_one("SELECT id FROM onboarding_case WHERE candidate_id=%s AND tenant_id=%s", [candidate_id, tenant_id]):
        raise HTTPException(409, "Day-1 onboarding has already been triggered for this candidate")

    case = query_one(
        """INSERT INTO onboarding_case (tenant_id, candidate_id, status, day_one_at, initiated_by)
           VALUES (%s,%s,'day_one',now(),%s) RETURNING id, status, day_one_at""",
        [tenant_id, candidate_id, user["sub"]],
    )
    for task_key, task_label in _DAY_ONE_TASKS:
        query(
            """INSERT INTO onboarding_task (tenant_id, onboarding_case_id, task_key, task_label)
               VALUES (%s,%s,%s,%s)""",
            [tenant_id, case["id"], task_key, task_label], fetch=False,
        )

    log_activity("onboarding_case", "day_one_triggered",
                 entity_id=case["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"candidate_id": candidate_id})

    return {"id": str(case["id"]), "status": case["status"], "day_one_at": case["day_one_at"]}


class ConvertToEmployeeIn(BaseModel):
    department_id: Optional[str] = None
    manager_id: Optional[str] = None
    location: Optional[str] = None
    grade: Optional[str] = None
    cost_center: Optional[str] = None


@router.post("/candidates/{candidate_id}/convert-to-employee")
def convert_to_employee(candidate_id: str, body: ConvertToEmployeeIn, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    if not query_one("SELECT id FROM candidate WHERE id=%s AND tenant_id=%s", [candidate_id, tenant_id]):
        raise HTTPException(404, "Candidate not found")
    if query_one("SELECT id FROM employee_master WHERE candidate_id=%s AND tenant_id=%s", [candidate_id, tenant_id]):
        raise HTTPException(409, "An employee record already exists for this candidate")

    offer = query_one(
        """SELECT o.id, o.application_id, o.designation, o.joining_date,
                  r.bu_id, r.hiring_location, r.grade_level
           FROM offer o
           JOIN application a ON a.id = o.application_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE a.candidate_id = %s AND o.status = ANY(%s)
           ORDER BY o.created_at DESC LIMIT 1""",
        [candidate_id, list(_ACCEPTED_OFFER_STATUSES)],
    )
    if not offer:
        raise HTTPException(409, "This candidate has no accepted offer — cannot convert to an employee record yet")

    department_id = body.department_id or (str(offer["bu_id"]) if offer["bu_id"] else None)
    if department_id and not query_one("SELECT id FROM business_unit WHERE id=%s", [department_id]):
        raise HTTPException(400, "Invalid department_id")
    if body.manager_id and not query_one("SELECT id FROM app_user WHERE id=%s", [body.manager_id]):
        raise HTTPException(400, "Invalid manager_id")

    location = body.location or offer["hiring_location"]
    grade = body.grade or offer["grade_level"]
    employee_code = _generate_employee_code(tenant_id)

    row = query_one(
        """INSERT INTO employee_master
             (tenant_id, candidate_id, application_id, employee_code, designation, department_id,
              manager_id, location, grade, cost_center, joining_date)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id, employee_code, designation, department_id, manager_id, location, grade,
                     cost_center, joining_date, status""",
        [tenant_id, candidate_id, offer["application_id"], employee_code, offer["designation"], department_id,
         body.manager_id, location, grade, body.cost_center, offer["joining_date"]],
    )

    log_activity("employee_master", "employee_master_created",
                 entity_id=row["id"], actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"candidate_id": candidate_id, "employee_code": employee_code})

    return {**row, "id": str(row["id"])}


@router.get("/candidates/{candidate_id}/employee-master")
def get_employee_master(candidate_id: str, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    row = query_one(
        """SELECT id, candidate_id, application_id, employee_code, designation, department_id, manager_id,
                  location, grade, cost_center, joining_date, status, created_at
           FROM employee_master WHERE candidate_id=%s AND tenant_id=%s""",
        [candidate_id, tenant_id],
    )
    if not row:
        raise HTTPException(404, "No employee record found for this candidate")
    return {**row, "id": str(row["id"])}


class EmployeeMasterPatchIn(BaseModel):
    designation: Optional[str] = None
    department_id: Optional[str] = None
    manager_id: Optional[str] = None
    location: Optional[str] = None
    grade: Optional[str] = None
    cost_center: Optional[str] = None
    joining_date: Optional[date] = None


@router.patch("/candidates/{candidate_id}/employee-master")
def patch_employee_master(candidate_id: str, body: EmployeeMasterPatchIn, user: dict = Depends(get_current_user)):
    tenant_id = user.get("tenant_id")
    row = query_one(
        "SELECT id, status FROM employee_master WHERE candidate_id=%s AND tenant_id=%s",
        [candidate_id, tenant_id],
    )
    if not row:
        raise HTTPException(404, "No employee record found for this candidate")
    if row["status"] not in ("pre_sync", "active"):
        raise HTTPException(409, f"Cannot edit an employee record in status '{row['status']}'")
    if body.department_id and not query_one("SELECT id FROM business_unit WHERE id=%s", [body.department_id]):
        raise HTTPException(400, "Invalid department_id")
    if body.manager_id and not query_one("SELECT id FROM app_user WHERE id=%s", [body.manager_id]):
        raise HTTPException(400, "Invalid manager_id")

    fields, params = [], []
    for col in ("designation", "department_id", "manager_id", "location", "grade", "cost_center", "joining_date"):
        val = getattr(body, col)
        if val is not None:
            fields.append(f"{col}=%s"); params.append(val)
    if not fields:
        raise HTTPException(400, "No fields to update")
    params.append(row["id"])
    query(f"UPDATE employee_master SET {', '.join(fields)} WHERE id=%s", params, fetch=False)

    log_activity("employee_master", "employee_master_updated",
                 entity_id=row["id"], actor_id=user["sub"], actor_role=user.get("role"))
    return {"ok": True}


class TaskPatchIn(BaseModel):
    status: str


@router.patch("/tasks/{task_id}")
def patch_onboarding_task(task_id: str, body: TaskPatchIn, user: dict = Depends(get_current_user)):
    if body.status not in ("pending", "done"):
        raise HTTPException(400, "status must be 'pending' or 'done'")
    tenant_id = user.get("tenant_id")
    task = query_one(
        "SELECT id, onboarding_case_id FROM onboarding_task WHERE id=%s AND tenant_id=%s",
        [task_id, tenant_id],
    )
    if not task:
        raise HTTPException(404, "Task not found")

    if body.status == "done":
        query(
            "UPDATE onboarding_task SET status='done', completed_by=%s, completed_at=now() WHERE id=%s",
            [user["sub"], task_id], fetch=False,
        )
    else:
        query(
            "UPDATE onboarding_task SET status='pending', completed_by=NULL, completed_at=NULL WHERE id=%s",
            [task_id], fetch=False,
        )
    new_case_status = _recompute_onboarding_case_status(task["onboarding_case_id"])

    log_activity("onboarding_task", "onboarding_task_updated",
                 entity_id=task_id, actor_id=user["sub"], actor_role=user.get("role"),
                 detail={"status": body.status})

    return {"id": task_id, "status": body.status, "case_status": new_case_status}
