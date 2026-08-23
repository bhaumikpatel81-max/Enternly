"""
Role-scoping helper for report/dashboard queries.

Generalizes kpi_api._scope (the more complete version — it also handles
hiring_manager, unlike reports_api._recruiter_join which only handles
recruiter) so every report source (existing and the custom-report builder)
shares one implementation.

Every explore this is used against aliases requisition as `r` (either
directly or via a join chain), which is what recruiter/hiring_manager
scoping keys off.
"""


def scope_for(role: str, uid: str):
    """
    Returns (join_sql, where_sql, join_params, where_params).

    Calling convention matches the existing kpi_api._scope pattern:
    params = join_params + [<other params>] + where_params
    """
    if role == "recruiter":
        return (
            "JOIN requisition_recruiter rr ON rr.requisition_id = r.id AND rr.recruiter_id = %s",
            "",
            [uid],
            [],
        )
    if role == "hiring_manager":
        return ("", "AND r.hiring_manager_id = %s", [], [uid])
    # ta_manager / admin — unrestricted
    return ("", "", [], [])
