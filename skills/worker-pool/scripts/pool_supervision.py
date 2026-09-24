#!/usr/bin/env python3
"""Supervision decisions for the worker pool: when to recover, when to ask the owner.

Pure policy. Callers do the I/O — read the beats, probe the sessions, run the
remedy — and hand the observations here. Nothing in this module touches the
filesystem or a process, so a test can simulate a week of ticks, and a host
sleep, in microseconds.

The rungs come from docs/worker-pool-design.md and the owner's settlement:
detection at the design's 90 s stale line, silent recovery, and only if recovery
has not brought the worker back by 3 minutes does the owner get asked.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

# The design's periodic OS timer. A caller sampling faster passes its own
# cadence: the wake test is relative to what that caller expected.
SAMPLE_PERIOD_S = 300.0
# Beyond period + slack the wall clock jumped further than sampling explains.
WAKE_SLACK_S = 60.0
# The design's "sustained": three consecutive ticks of the same evidence.
SUSTAINED_TICKS = 3
DETECT_AFTER_S = 90.0
ESCALATE_AFTER_S = 180.0

# Beat readings, as pool_beat classifies them.
LIVE, STALE, ABSENT, UNKNOWN = "live", "stale", "absent", "unknown"

NOTHING, RECOVER, ESCALATE, REARM_WATCHER = "nothing", "recover", "escalate", "rearm_watcher"


@dataclass(frozen=True)
class Observation:
    """One worker at one tick.

    `session_alive` is None when the probe could not answer. Unknown is not
    death: the design requires the session to be *gone*, so an unanswered probe
    authorises nothing.

    `watcher_beat` is the watcher's own beat; `watcher_held` says whether a
    session-role watcher provably serves the inbox (None: could not be told). A
    watcher that predates beat injection beats nothing yet holds the inbox, so
    a stale beat alone is never a lost watcher.
    """

    beat: str
    session_alive: bool | None
    paused: bool = False
    watcher_beat: str = UNKNOWN
    watcher_held: bool | None = None


@dataclass(frozen=True)
class WorkerEvidence:
    first_detected_at: float | None = None
    consecutive: int = 0
    recover_issued_at: float | None = None
    escalated: bool = False
    # The watcher ladder runs its own clock: a lost watcher under a live session.
    watcher_first_detected_at: float | None = None
    watcher_consecutive: int = 0
    rearm_issued_at: float | None = None
    watcher_escalated: bool = False


@dataclass(frozen=True)
class SupervisionState:
    last_sample_at: float | None = None
    workers: dict[str, WorkerEvidence] = field(default_factory=dict)


def is_resume(now: float, last_sample_at: float | None,
              *, expected_period_s: float = SAMPLE_PERIOD_S,
              slack_s: float = WAKE_SLACK_S) -> bool:
    """True when the gap since the previous sample is larger than sampling explains.

    A host sleep expires every beat at once, so the first sample after one shows
    a whole pool as dead. Counting it would relaunch the pool on every lid-open.
    """
    if last_sample_at is None:
        return False
    return (now - last_sample_at) > (expected_period_s + slack_s)


def _is_death_evidence(obs: Observation) -> bool:
    """Beat expired AND the session is gone — the design's process-death evidence.

    A live session with an expired beat is not death: the worker may simply be
    working. An unreadable beat is not death either.
    """
    return obs.beat in (STALE, ABSENT) and obs.session_alive is False


def _is_watcher_loss(obs: Observation) -> bool:
    """Session alive, watcher beat expired or missing, AND no session-role
    watcher provably holds the inbox. A held inbox with a stale beat is a watcher
    that beats nothing (launched before beat injection), not a lost one; an
    unproven holder check (None) is not evidence either way."""
    return (obs.session_alive is True and obs.watcher_beat in (STALE, ABSENT)
            and obs.watcher_held is False)


def _cleared_session(ev: WorkerEvidence) -> WorkerEvidence:
    return replace(ev, first_detected_at=None, consecutive=0,
                   recover_issued_at=None, escalated=False)


def _cleared_watcher(ev: WorkerEvidence) -> WorkerEvidence:
    return replace(ev, watcher_first_detected_at=None, watcher_consecutive=0,
                   rearm_issued_at=None, watcher_escalated=False)


def _watcher_rung(ev: WorkerEvidence, obs: Observation, now: float, *,
                  sustained_ticks: int, detect_after_s: float,
                  escalate_after_s: float) -> tuple[WorkerEvidence, str]:
    """The watcher ladder, same rungs and clocks as the session's: sustained loss
    past the stale line asks the supervisor to re-arm once; still lost past the
    owner's line escalates once. Runs only under a session that answered alive."""
    if obs.watcher_beat == LIVE or obs.watcher_held is True:
        return _cleared_watcher(ev), NOTHING
    if not _is_watcher_loss(obs):
        return ev, NOTHING
    ev = replace(
        ev,
        watcher_consecutive=ev.watcher_consecutive + 1,
        watcher_first_detected_at=(ev.watcher_first_detected_at
                                   if ev.watcher_first_detected_at is not None else now),
    )
    elapsed = now - ev.watcher_first_detected_at
    decision = NOTHING
    if ev.watcher_consecutive >= sustained_ticks:
        if ev.rearm_issued_at is None and elapsed >= detect_after_s:
            decision = REARM_WATCHER
            ev = replace(ev, rearm_issued_at=now)
        elif (ev.rearm_issued_at is not None and not ev.watcher_escalated
                and elapsed >= escalate_after_s):
            decision = ESCALATE
            ev = replace(ev, watcher_escalated=True)
    return ev, decision


def evaluate(state: SupervisionState, observations: dict[str, Observation], now: float,
             *, expected_period_s: float = SAMPLE_PERIOD_S,
             slack_s: float = WAKE_SLACK_S,
             sustained_ticks: int = SUSTAINED_TICKS,
             detect_after_s: float = DETECT_AFTER_S,
             escalate_after_s: float = ESCALATE_AFTER_S,
             ) -> tuple[SupervisionState, dict[str, str]]:
    """Advance one tick. Returns the new state and one decision per worker."""
    if is_resume(now, state.last_sample_at,
                 expected_period_s=expected_period_s, slack_s=slack_s):
        # Discarded as evidence: the clock moved, the pool did not necessarily die.
        return replace(state, last_sample_at=now), {w: NOTHING for w in observations}

    workers = dict(state.workers)
    decisions: dict[str, str] = {}

    for worker_id, obs in observations.items():
        if obs.paused:
            # Owner-paused outranks every signal, and observing a paused worker
            # never clears the pause. Its evidence is left exactly as it was.
            decisions[worker_id] = NOTHING
            continue

        ev = workers.get(worker_id, WorkerEvidence())

        # A session that answers contradicts "the session is gone", so it clears a
        # death as surely as a beat does; an UNANSWERED probe does not, and holds.
        # Under a live session the watcher ladder runs instead, on its own clock.
        if obs.beat == LIVE or obs.session_alive is True:
            ev, decision = _watcher_rung(
                _cleared_session(ev), obs, now, sustained_ticks=sustained_ticks,
                detect_after_s=detect_after_s, escalate_after_s=escalate_after_s)
            workers[worker_id] = ev
            decisions[worker_id] = decision
            continue

        if not _is_death_evidence(obs):
            # Not evidence either way: hold what we have rather than resetting,
            # so an unreadable beat cannot launder away a death already seen.
            workers[worker_id] = ev
            decisions[worker_id] = NOTHING
            continue

        ev = replace(
            ev,
            consecutive=ev.consecutive + 1,
            # The ladder's clock starts at FIRST detection, not at the sample
            # that happened to confirm it.
            first_detected_at=ev.first_detected_at if ev.first_detected_at is not None else now,
        )

        elapsed = now - ev.first_detected_at
        decision = NOTHING
        if ev.consecutive >= sustained_ticks:
            if ev.recover_issued_at is None and elapsed >= detect_after_s:
                decision = RECOVER
                ev = replace(ev, recover_issued_at=now)
            elif (ev.recover_issued_at is not None and not ev.escalated
                    and elapsed >= escalate_after_s):
                # Recovery was issued and the worker is still gone: rung 2 is the
                # owner's, because changing executor is never the core's call.
                decision = ESCALATE
                ev = replace(ev, escalated=True)

        workers[worker_id] = ev
        decisions[worker_id] = decision

    return SupervisionState(last_sample_at=now, workers=workers), decisions
