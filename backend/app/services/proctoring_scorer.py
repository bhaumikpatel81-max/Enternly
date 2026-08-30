"""
Phase 3, Part B — server-side proctoring strike scorer.

Pure, read-only function: given a session_id, reads proctoring_flag_ledger +
proctoring_pause_event + proctoring_session_state and replays the SAME
tick-by-tick decision logic as frontend/interview.html's _evalStrikeTick(),
so the server's count can never legitimately disagree with what the browser
would have computed from the same raw flags.

Does NOT write anything, does NOT terminate anything. Not wired into any
endpoint yet (that's Phase 3, Part E, gated behind SERVER_SIDE_PROCTORING_JUDGE).

RULEBOOK — mirrors interview.html's constants of the same name. If those
values change in interview.html, they MUST be updated here too, or the
server and browser will diverge (see PROCTORING_PHASE_NOTES.txt).
"""
import json
from datetime import timedelta

from ..db import query, query_one

# Phase 3, Part C — a low_light pause left unresumed this long ends the
# interview for a NEUTRAL reason (room stayed too dark), distinct from a
# cheating-based termination.
LOW_LIGHT_PAUSE_LIMIT_SECONDS = 300  # 5 minutes

TICK_SECONDS = 5  # interview.html's _procFlagInterval period

STRIKE_FLAG_TYPES = [
    'no_face', 'multi_face', 'face_away', 'phone_detected',
    'unknown_object', 'tab_switch', 'screen_share_stopped',
]
STRIKE_PERSIST_DETECTIONS = {
    'no_face': 5, 'face_away': 4, 'multi_face': 2, 'phone_detected': 1,
    'unknown_object': 1, 'tab_switch': 3, 'screen_share_stopped': 1,
}
MAX_STRIKES = 3
STRIKE_COOLDOWN_MS = 15000

# ── KNOWN DIVERGENCE — see PROCTORING_PHASE_NOTES.txt, "DIVERGENCE 1" ──────
# Server-only phone-aware exception: interview.html has NO device_type-aware
# logic anywhere today. On a phone session, the phone IS the candidate's own
# test device, so phone_detected must never count as a strike, and a
# hand-held camera drifts more than a fixed laptop webcam, so the face-loss
# thresholds are relaxed. This makes the server MORE LENIENT than the
# browser on phone sessions until interview.html is updated to match —
# tracked as a must-fix-before-go-live item, not silently absorbed.
PHONE_EXCLUDED_TYPES = {'phone_detected'}
PHONE_STRIKE_OVERRIDES = {'no_face': 8, 'face_away': 7}


def _effective_rulebook(device_type):
    """Return (persist_thresholds, excluded_types) for this device_type."""
    persist = dict(STRIKE_PERSIST_DETECTIONS)
    excluded = set()
    if device_type == 'phone':
        persist.update(PHONE_STRIKE_OVERRIDES)
        excluded |= PHONE_EXCLUDED_TYPES
    return persist, excluded


def _load_pause_windows(session_id):
    rows = query(
        """SELECT paused_at, resumed_at FROM proctoring_pause_event
           WHERE session_id = %s AND pause_reason = 'low_light'""",
        [session_id],
    )
    return [(r['paused_at'], r['resumed_at']) for r in rows]


def _in_pause_window(ts, pause_windows):
    for start, end in pause_windows:
        if ts is None or start is None:
            continue
        if ts >= start and (end is None or ts < end):
            return True
    return False


def score_session(session_id):
    """
    Compute the server's authoritative strike count/outcome for a proctoring
    session, replaying the browser's own tick-by-tick logic against the
    server-side ledger. Read-only — no writes.
    """
    state = query_one(
        "SELECT device_type FROM proctoring_session_state WHERE session_id = %s",
        [session_id],
    )
    device_type = (state or {}).get('device_type') or 'unknown'
    persist, excluded_types = _effective_rulebook(device_type)
    pause_windows = _load_pause_windows(session_id)

    rows = query(
        """SELECT flag_type, tick_index, COALESCE(client_timestamp, server_received_at) AS ts
           FROM proctoring_flag_ledger
           WHERE session_id = %s
           ORDER BY tick_index ASC""",
        [session_id],
    )

    # Group by tick_index -> {flag_types present}, and capture a representative
    # timestamp per tick (all flag types sharing a tick_index share the browser's
    # single `now = Date.now()` for that tick, so first-seen is exact).
    present_by_tick = {}
    ts_by_tick = {}
    excluded_row_count = 0
    for r in rows:
        ft = r['flag_type']
        if ft not in STRIKE_FLAG_TYPES:
            continue
        if ft in excluded_types:
            excluded_row_count += 1
            continue
        if _in_pause_window(r['ts'], pause_windows):
            excluded_row_count += 1
            continue
        present_by_tick.setdefault(r['tick_index'], set()).add(ft)
        ts_by_tick.setdefault(r['tick_index'], r['ts'])

    if not present_by_tick:
        return {
            'session_id': str(session_id),
            'device_type': device_type,
            'total_strikes': 0,
            'outcome': 'ok',
            'strikes': [],
            'per_flag_type_strikes': {t: 0 for t in STRIKE_FLAG_TYPES},
            'excluded_row_count': excluded_row_count,
            'phone_exception_applied': device_type == 'phone',
        }

    min_tick = min(present_by_tick.keys())
    max_tick = max(present_by_tick.keys())
    # Anchor for interpolating timestamps of ticks with zero flags (no ledger
    # row at all for that tick_index, for any type) — ticks are 5s apart by
    # construction, so any known (tick_index, ts) pair anchors the rest.
    anchor_tick = min_tick
    anchor_ts = ts_by_tick[min_tick]

    def _approx_ts(tick_index):
        return anchor_ts + timedelta(seconds=TICK_SECONDS * (tick_index - anchor_tick))

    consecutive = {t: 0 for t in STRIKE_FLAG_TYPES}
    cooldown_last = {}
    strikes = []
    per_type_strikes = {t: 0 for t in STRIKE_FLAG_TYPES}

    for tick_index in range(min_tick, max_tick + 1):
        ts = ts_by_tick.get(tick_index) or _approx_ts(tick_index)

        # A tick whose time falls inside a low_light pause window is frozen
        # entirely — matches the browser's `if (_lowLightPaused) return;`
        # guard at the very top of the tick, which runs before any counter is
        # touched. Do not reset OR increment anything for this tick.
        if _in_pause_window(ts, pause_windows):
            continue

        present = present_by_tick.get(tick_index, set())

        # Unconditional accumulate/reset for every tracked type — mirrors
        # _evalStrikeTick's first loop exactly.
        for t in STRIKE_FLAG_TYPES:
            if t in excluded_types:
                continue
            consecutive[t] = consecutive.get(t, 0) + 1 if t in present else 0

        # Threshold + cooldown check, in STRIKE_FLAG_TYPES order, at most one
        # strike per tick — mirrors _evalStrikeTick's third loop exactly,
        # including that a type over-threshold-but-in-cooldown is NOT reset.
        for t in STRIKE_FLAG_TYPES:
            if t in excluded_types:
                continue
            req = persist.get(t, 1)
            if consecutive[t] < req:
                continue
            last = cooldown_last.get(t)
            if last is not None and (ts - last).total_seconds() * 1000 < STRIKE_COOLDOWN_MS:
                continue
            consecutive[t] = 0
            cooldown_last[t] = ts
            strikes.append({'tick_index': tick_index, 'flag_type': t, 'ts': ts.isoformat()})
            per_type_strikes[t] += 1
            break  # at most one strike per tick

    total_strikes = len(strikes)
    outcome = 'terminate_cheating' if total_strikes >= MAX_STRIKES else 'ok'

    return {
        'session_id': str(session_id),
        'device_type': device_type,
        'total_strikes': total_strikes,
        'outcome': outcome,
        'strikes': strikes,
        'per_flag_type_strikes': per_type_strikes,
        'excluded_row_count': excluded_row_count,
        'phone_exception_applied': device_type == 'phone',
    }


MONITORING_GAP_THRESHOLD_SECONDS = 60  # Phase 3, Part D default


def detect_monitoring_gaps(session_id, gap_threshold_seconds=MONITORING_GAP_THRESHOLD_SECONDS):
    """
    Phase 3, Part D — read-only. Looks at the full heartbeat history for this
    session and returns every gap between consecutive heartbeats that exceeds
    gap_threshold_seconds. A gap is informational only, for human review — it
    never auto-terminates anything on its own.
    """
    rows = query(
        """SELECT received_at FROM proctoring_heartbeat
           WHERE session_id = %s ORDER BY received_at ASC""",
        [session_id],
    )
    gaps = []
    for prev, cur in zip(rows, rows[1:]):
        delta = (cur['received_at'] - prev['received_at']).total_seconds()
        if delta > gap_threshold_seconds:
            gaps.append({
                'gap_start': prev['received_at'].isoformat(),
                'gap_end': cur['received_at'].isoformat(),
                'duration_seconds': delta,
            })
    return {
        'session_id': str(session_id),
        'heartbeat_count': len(rows),
        'gap_threshold_seconds': gap_threshold_seconds,
        'gaps': gaps,
        'has_gaps': bool(gaps),
    }


def record_integrity_flag(session_id, kind, detail, enteri_ai_session_id=None, dedupe_key=None):
    """
    Phase 4, Part B — insert one row into the unified integrity-flag inbox,
    unless an identical (session_id, kind, dedupe_key) already exists.
    dedupe_key defaults to a fixed sentinel when the caller doesn't need
    finer-grained separation (e.g. termination_discrepancy: one open
    discrepancy per session is enough — see call site for why). For kinds
    where the same detector can legitimately fire more than once per session
    (e.g. monitoring_gap — multiple distinct gaps), the caller passes a key
    that identifies the specific occurrence (e.g. the gap's start time) so
    re-running the detector is idempotent without collapsing genuinely
    different incidents into one row.

    Never raises on the duplicate case. Returns the new row's id if inserted,
    None if it was a no-op dedupe.
    """
    key = dedupe_key if dedupe_key is not None else 'default'
    row = query_one(
        """INSERT INTO proctoring_integrity_flag
               (session_id, enteri_ai_session_id, flag_kind, dedupe_key, detail)
           VALUES (%s, %s, %s, %s, %s::jsonb)
           ON CONFLICT (session_id, flag_kind, dedupe_key) DO NOTHING
           RETURNING id""",
        [session_id, enteri_ai_session_id, kind, key, json.dumps(detail, default=str)],
    )
    return str(row['id']) if row else None


def record_monitoring_gaps(session_id, enteri_ai_session_id=None, gap_threshold_seconds=MONITORING_GAP_THRESHOLD_SECONDS):
    """
    Phase 4, Part B — wires detect_monitoring_gaps (pure, Phase 3) into the
    integrity-flag inbox. Call this at a real trigger point (session
    completion, or a termination attempt); detect_monitoring_gaps itself
    stays a pure read-only function, unchanged, so anything that only wants
    the numbers (e.g. judge_termination) is unaffected by this side effect.
    Returns the list of newly-recorded flag ids (empty if no new gaps, or if
    all gaps found were already recorded on a prior call).
    """
    result = detect_monitoring_gaps(session_id, gap_threshold_seconds)
    new_ids = []
    for gap in result['gaps']:
        fid = record_integrity_flag(
            session_id, 'monitoring_gap', gap,
            enteri_ai_session_id=enteri_ai_session_id,
            dedupe_key=gap['gap_start'],
        )
        if fid:
            new_ids.append(fid)
    return new_ids


def judge_termination(session_id):
    """
    Phase 3, Part E — orchestrates the three read-only checks above into a
    single termination decision. Pure computation only; the caller
    (enteri_ai_api.terminate_invite_session, gated behind
    SERVER_SIDE_PROCTORING_JUDGE) is responsible for actually writing
    termination state or a discrepancy row based on this result — this
    function never writes anything itself.

    Precedence: a timed-out low_light pause is checked first (it's a neutral,
    unambiguous "we genuinely lost the ability to monitor" condition, and
    should not be shadowed by a coincidentally-also-true strike count).
    Otherwise, the strike scorer is authoritative. If neither supports
    termination, a monitoring gap (if any) is surfaced as the likely
    explanation for a browser-claimed termination the ledger doesn't back up.
    """
    low_light = check_low_light_pause_timeout(session_id)
    if low_light['timed_out']:
        return {'outcome': 'ended_low_light', 'should_terminate': True, 'detail': low_light}

    score = score_session(session_id)
    if score['outcome'] == 'terminate_cheating':
        return {'outcome': 'terminate_cheating', 'should_terminate': True, 'detail': score}

    gaps = detect_monitoring_gaps(session_id)
    outcome = 'needs_review_monitoring_gap' if gaps['has_gaps'] else 'discrepancy_no_support'
    return {'outcome': outcome, 'should_terminate': False, 'detail': {'score': score, 'gaps': gaps}}


def check_low_light_pause_timeout(session_id):
    """
    Phase 3, Part C — read-only. Looks at the most recent OPEN (resumed_at IS
    NULL) low_light pause for this session, if any, and reports whether it
    has exceeded LOW_LIGHT_PAUSE_LIMIT_SECONDS since paused_at. Used by
    /pause's response, the polling endpoint, and (Part E) the judge's
    'ended_low_light' outcome. Does not write anything or close the pause.
    """
    row = query_one(
        """SELECT id, paused_at FROM proctoring_pause_event
           WHERE session_id = %s AND pause_reason = 'low_light' AND resumed_at IS NULL
           ORDER BY paused_at DESC LIMIT 1""",
        [session_id],
    )
    if not row:
        return {'open_pause_id': None, 'timed_out': False, 'remaining_seconds': None, 'deadline_at': None}

    now = query_one("SELECT now() AS now")['now']
    deadline = row['paused_at'] + timedelta(seconds=LOW_LIGHT_PAUSE_LIMIT_SECONDS)
    remaining = (deadline - now).total_seconds()
    return {
        'open_pause_id': str(row['id']),
        'paused_at': row['paused_at'].isoformat(),
        'deadline_at': deadline.isoformat(),
        'remaining_seconds': max(0, remaining),
        'timed_out': remaining <= 0,
    }
