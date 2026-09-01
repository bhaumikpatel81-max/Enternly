"""
Preboarding proposer -- daily background loop (ATS spec §12 amendment).

Per tenant with preboarding_config.auto_propose_enabled, finds candidates
with an accepted offer whose joining_date falls within days_before_joining
and who have no preboarding_case yet, and creates one for each with
status='proposed', auto_proposed=TRUE. Proposing only -- it never seeds
policy acks or opens the portal; a human confirms via
POST /api/preboarding/candidates/{id}/confirm (see preboarding_api.py),
which is what actually seeds the acks and generates the portal token.

Idempotent: the NOT EXISTS guard means a candidate that already has any
preboarding_case (proposed, confirmed, or manually initiated) is never
touched again. Crash-visibility is both the generic bg_task_status:<name>
key _track_bg_task writes on an unhandled crash (main.py) and this loop's
own per-run status write below, so a "ran but proposed nothing" pass is
distinguishable from "hasn't run in days".
"""
import asyncio

from ..db import query

_DAILY_INTERVAL_SECONDS = 24 * 60 * 60
_STATUS_KEY = "bg_task_status:preboarding_proposer"

# See preboarding_api.py's _ACCEPTED_OFFER_STATUSES comment: offer.status
# never actually reaches the schema-legal 'accepted' value through any
# code path today, so 'sent_to_darwinbox' (the real terminal state offers
# reach once approved) is treated as "accepted" here too -- not a
# Darwinbox-specific signal despite the name.
_ACCEPTED_OFFER_STATUSES = ("accepted", "released", "sent_to_darwinbox")


def _set_status(status: str, detail: str = "") -> None:
    try:
        query(
            """INSERT INTO system_status (key, value) VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            [_STATUS_KEY, f"{status}|{detail}"[:500]],
            fetch=False,
        )
    except Exception as exc:
        print(f"[preboarding-proposer] could not persist status: {exc}")


def _propose_due_cases() -> int:
    """One pass across every tenant with auto-propose enabled. Returns the
    number of cases proposed."""
    configs = query(
        "SELECT tenant_id, days_before_joining FROM preboarding_config WHERE auto_propose_enabled = TRUE"
    ) or []
    proposed = 0
    for cfg in configs:
        tenant_id = cfg["tenant_id"]
        candidates = query(
            """SELECT DISTINCT ON (a.candidate_id) a.candidate_id, o.id AS offer_id, o.application_id, o.joining_date
               FROM offer o
               JOIN application a ON a.id = o.application_id
               JOIN candidate c ON c.id = a.candidate_id
               WHERE c.tenant_id = %s AND o.status = ANY(%s) AND o.joining_date IS NOT NULL
                 AND o.joining_date <= (CURRENT_DATE + (%s || ' days')::interval)
                 AND NOT EXISTS (SELECT 1 FROM preboarding_case pc WHERE pc.candidate_id = a.candidate_id)
               ORDER BY a.candidate_id, o.created_at DESC""",
            [tenant_id, list(_ACCEPTED_OFFER_STATUSES), cfg["days_before_joining"]],
        ) or []
        for row in candidates:
            try:
                query(
                    """INSERT INTO preboarding_case
                         (tenant_id, candidate_id, application_id, offer_id, status, auto_proposed, joining_date)
                       VALUES (%s,%s,%s,%s,'proposed',TRUE,%s)""",
                    [tenant_id, row["candidate_id"], row["application_id"], row["offer_id"], row["joining_date"]],
                    fetch=False,
                )
                proposed += 1
            except Exception as exc:
                print(f"[preboarding-proposer] failed to propose candidate {row['candidate_id']}: {exc}")
    return proposed


async def run_one_pass() -> int:
    """
    One pass across every tenant with auto-propose enabled. Returns the
    number of cases proposed. Public entrypoint for the Arq queued job
    (worker.py) -- already idempotent via _propose_due_cases's NOT EXISTS
    guard (a candidate with any preboarding_case, from any prior run, is
    never re-proposed), so no job_lock/claim is needed even under
    concurrent execution: two overlapping passes just do some duplicate
    read work and no duplicate writes.
    """
    count = await asyncio.to_thread(_propose_due_cases)
    _set_status("ok", f"proposed {count} case(s)")
    print(f"[preboarding-proposer] proposed {count} case(s)")
    return count


async def start_preboarding_proposer_worker():
    """Infinite background loop -- proposes preboarding cases for candidates
    approaching their joining date. Runs once at startup, then every 24h."""
    print("[preboarding-proposer] background worker started")
    while True:
        try:
            await run_one_pass()
        except asyncio.CancelledError:
            print("[preboarding-proposer] task cancelled, shutting down")
            return
        except Exception as exc:
            print(f"[preboarding-proposer] unexpected error: {exc}")
            _set_status("error", str(exc))
        await asyncio.sleep(_DAILY_INTERVAL_SECONDS)
