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


def scope_for(role: str, uid: str, tenant_id: str = None):
    """
    Returns (join_sql, where_sql, join_params, where_params).

    Calling convention matches the existing kpi_api._scope pattern:
    params = join_params + [<other params>] + where_params

    tenant_id is applied on top of every role's scope (not instead of it) --
    ta_manager/admin previously had no boundary at all here ("unrestricted"
    meant unrestricted across every tenant, not just their own company).
    Every caller aliases requisition as `r`, which is what the tenant filter
    keys off of, same as the existing recruiter/hiring_manager conditions.
    Callers on a pre-tenant code path (none currently) can omit tenant_id
    and get the old unscoped behaviour for that piece.
    """
    tenant_clause = "r.tenant_id = %s" if tenant_id else None
    tenant_params = [tenant_id] if tenant_id else []

    if role == "recruiter":
        where_sql = f"AND {tenant_clause}" if tenant_clause else ""
        return (
            "JOIN requisition_recruiter rr ON rr.requisition_id = r.id AND rr.recruiter_id = %s",
            where_sql,
            [uid],
            tenant_params,
        )
    if role == "hiring_manager":
        where_sql = "AND r.hiring_manager_id = %s"
        params = [uid]
        if tenant_clause:
            where_sql += f" AND {tenant_clause}"
            params += tenant_params
        return ("", where_sql, [], params)
    # ta_manager / admin — full company-wide visibility, now bounded to one tenant
    where_sql = f"AND {tenant_clause}" if tenant_clause else ""
    return ("", where_sql, [], tenant_params)
