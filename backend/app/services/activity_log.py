"""
Generic activity/audit logging — backend-only action timestamps for anything
stage_event / offer_approval_step don't already capture (those two stay the
system of record for pipeline-stage transitions and offer approvals; do not
duplicate writes into activity_log for actions they already cover).

Timestamps captured here are never surfaced live — they're only read back
through the /api/activity-log/* report endpoints (see routers/activity_log_api.py).

Best-effort by default: an audit write must never break the primary request,
mirroring routers/auth.py's login_log try/except-swallow idiom. Callers that
need the row to be atomic with the primary write (activity_log is the ONLY
record of the action — requisition lifecycle, module-access grants) should
not use this function; they should tx_exec() an equivalent INSERT inside
their own `with transaction() as cur:` block instead.
"""
import json

from ..db import query


def log_activity(
    entity_type: str,
    action: str,
    *,
    entity_id=None,
    requisition_id=None,
    application_id=None,
    actor_id=None,
    actor_role=None,
    actor_label=None,
    from_value=None,
    to_value=None,
    detail: dict | None = None,
    ip_address=None,
) -> None:
    try:
        query(
            """INSERT INTO activity_log
                 (entity_type, entity_id, requisition_id, application_id, action,
                  actor_id, actor_role, actor_label, from_value, to_value, detail, ip_address)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
            [
                entity_type, entity_id, requisition_id, application_id, action,
                actor_id, actor_role, actor_label, from_value, to_value,
                json.dumps(detail or {}), ip_address,
            ],
            fetch=False,
        )
    except Exception as exc:
        print(f"[activity_log] failed to log {entity_type}.{action}: {exc}")
        # Still print-only by design (this function must never risk breaking
        # the primary request it's called from) -- but a systemic outage here
        # would otherwise leave every finding upstream that relies on this as
        # its recovery trail equally silent. Best-effort counter so a run of
        # failures is at least visible somewhere other than stdout.
        try:
            query(
                """INSERT INTO system_status (key, value) VALUES ('activity_log_last_failure', %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                [f"{entity_type}.{action}|{exc}"[:500]],
                fetch=False,
            )
        except Exception:
            pass
