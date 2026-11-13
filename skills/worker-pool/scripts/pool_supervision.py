#!/usr/bin/env python3
"""Supervision decisions for the worker pool: when to recover, when to ask the owner.

Pure policy. Callers do the I/O — read the beats, probe the sessions, run the
remedy — and hand the observations here. Nothing in this module touches the
filesystem or a process, so a test can simulate a week of ticks, and a host
sleep, in microseconds.

The rungs come from docs/worker-pool-design.md and the owner's settlement:
detection at the design's 90 s stale line, silent recovery, and only if recovery
has not brought the worker back by 3 minutes does the owner get asked. A live
session is never restarted for a live-pane wedge. Limits with owed work request
bounded fresh turns through delivery; other wedges reach the owner as cards.
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
# A dead worker that still owes work recovers on the tick that confirms its
# first detection, not after the full sustain.
FAST_SUSTAINED_TICKS = 2
# An idle seat whose watcher is live but whose owed task was never typed in: longer than any
# normal hand-off, short enough that a blocked delivery is not left for an hour.
UNDELIVERED_AFTER_S = 900.0

# Beat readings, as pool_beat classifies them.
LIVE, STALE, ABSENT, UNKNOWN = "live", "stale", "absent", "unknown"

RETRY_LIMIT = "retry_limit"
LIMIT_PROBE_S = 900.0
LIMIT_MAX_ATTEMPTS = 4
# A seat parked on a limit seen under one account, while another account is on disk now: the owner
# signed in again, so its limit is over and the seat restarts on the new login (its owed work kept).
RESTART_NEW_LOGIN = "restart_new_login"
# The old binding stands until a restart is confirmed, so a failed try is tried again, this often:
RELOGIN_RETRY_S = 300.0
RELOGIN_MAX_ATTEMPTS = 3
# A seat a relogin restart took down and could not start again is tried until it starts: no cap,
# only a backoff that grows to this. A seat this rule killed always comes back.
RELOGIN_DOWN_MAX_RETRY_S = 3600.0

NOTHING, RECOVER, ESCALATE, REARM_WATCHER = "nothing", "recover", "escalate", "rearm_watcher"
# A wedged live session's card: the cause named (abnormal text), a frozen turn
# offering an Escape, or an expired login. No card decision ends or types into a session.
CARD_CAUSE, CARD_FROZEN, CARD_LOGIN, CARD_HUMAN, CARD_UNDELIVERED = (
    "card_cause", "card_frozen", "card_login", "card_human", "card_undelivered")
# Each wedge kind asks for a durable card.
WEDGE_CARDS = {"abnormal": CARD_CAUSE, "stuck": CARD_FROZEN, "login": CARD_LOGIN,
               "human": CARD_HUMAN, "undelivered": CARD_UNDELIVERED}

# The worker's pane, as the caller reads one capture. GATE and LIMIT wait on a
# human (a dialog, a spend or wait decision), so their cards offer only a terminal link.
PANE_IDLE, PANE_WORKING, PANE_GATE, PANE_LIMIT, PANE_ABNORMAL, PANE_UNKNOWN = (
    "idle", "working", "gate", "limit", "abnormal", "unknown")
# The CLI's own "Login expired · Please run /login": alive, and able to do no work.
PANE_LOGGED_OUT = "logged-out"
_HUMAN_PANES = (PANE_GATE, PANE_LIMIT)


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
    # This worker's own queue (None: not read), its pane's kind, and the raw
    # frame's identity, which a frozen turn repeats tick after tick.
    work_outstanding: bool | None = None
    pane: str | None = None
    pane_id: str | None = None
    # Why typed delivery cannot reach this seat, when the caller can tell ("pane-key-fence").
    delivery_block: str | None = None
    limit_reset_at: float | None = None
    # The banner's own reset words: an unchanged banner keeps the deadline it first parsed to.
    limit_banner: str | None = None
    # The pane waits on a provider limit and nothing else (pane_gate.limit_holds): no turn runs,
    # the composer is empty.
    limit_parked: bool = False
    # The seat's limit wait can be probed and retried (a Claude seat). A Codex seat's limit is only
    # shown: its watcher and undelivered-work checks run on while the cell is on screen.
    limit_waits: bool = True
    # The account the seat's open run was launched under (worker_identity's run `account`).
    launch_account: str | None = None


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
    # The wedge ladder: a live session whose pane will not progress while it owes work.
    wedge_first_detected_at: float | None = None
    wedge_consecutive: int = 0
    wedge_escalated: bool = False
    # The kind this tick's wedge reads as; `wedge_escalated` is about this kind only.
    wedge_kind: str | None = None
    last_pane_id: str | None = None
    limit_next_at: float | None = None
    limit_attempts: int = 0
    limit_exhausted: bool = False
    limit_banner: str | None = None
    limit_reset_at: float | None = None
    # The account a parked seat's limit was first seen under; another one on disk ends the wait.
    # It stays until a restart is confirmed; the tries so far and when the next one is due.
    limit_account: str | None = None
    relogin_attempts: int = 0
    relogin_next_at: float | None = None
    # The account the tries are counted against (a switch to another one starts the count again),
    # and whether a restart took the seat down without a confirmed start.
    relogin_target: str | None = None
    relogin_down: bool = False
    # The owner was told this down seat is not coming back: later failed tries stay silent.
    relogin_down_told: bool = False


@dataclass(frozen=True)
class SupervisionState:
    last_sample_at: float | None = None
    workers: dict[str, WorkerEvidence] = field(default_factory=dict)
    # The account on disk at the last tick that read one: a seat first seen parked after the
    # owner signed in is bound to it, the account its limit was hit under.
    last_account: str | None = None
    # The account the owner was last told the pool restarts on: one opening note per sign-in.
    relogin_announced: str | None = None


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


def _wait_part(pane_id: str | None) -> str | None:
    """The progress half of a "raw:progress" frame id; None for a bare raw id."""
    head, sep, tail = (pane_id or "").partition(":")
    return tail if sep and head and tail else None


def _wait_moved(obs: Observation, ev: WorkerEvidence) -> bool:
    """The last tick read the same gate or limit and the work on screen has changed since
    (pane_gate.progress_id): the session progresses under a stale banner."""
    now, before = _wait_part(obs.pane_id), _wait_part(ev.last_pane_id)
    return bool(ev.wedge_kind == "human" and now and before and now != before)


def _new_login(ev: WorkerEvidence, obs: Observation, now: float, account: str | None,
               before: str | None) -> tuple[WorkerEvidence, bool]:
    """(evidence, restart). A parked seat's limit is bound to the account its run was launched
    under, else the one it was first seen under (just after a switch: `before`, the tick
    before's). While it stays parked and another known account is on disk, a restart is due, at
    most RELOGIN_MAX_ATTEMPTS times with a doubling RELOGIN_RETRY_S backoff, counted afresh for
    each new account. A seat a restart took down without a confirmed start (`relogin_down`) is
    tried until it starts, with no cap. The binding stands until a restart is confirmed
    (`confirm_new_login`) or the seat runs or idles again. An unknown account binds nothing."""
    if ev.relogin_down and obs.session_alive is False:
        if ev.relogin_next_at is not None and now < ev.relogin_next_at:
            return ev, False
        wait = min(RELOGIN_RETRY_S * 2 ** ev.relogin_attempts, RELOGIN_DOWN_MAX_RETRY_S)
        return replace(ev, relogin_attempts=ev.relogin_attempts + 1, relogin_next_at=now + wait), True
    if obs.session_alive is not True:
        return ev, False
    if ev.relogin_down or ev.relogin_down_told:
        ev = replace(ev, relogin_down=False, relogin_down_told=False)
    if not obs.limit_parked:
        if (ev.limit_account or ev.relogin_attempts) and obs.pane in (PANE_IDLE, PANE_WORKING):
            ev = replace(ev, limit_account=None, relogin_attempts=0, relogin_next_at=None, relogin_target=None)
        return ev, False
    bound = ev.limit_account or obs.launch_account or (
        before if before and account and before != account else account)
    if bound != ev.limit_account:
        ev = replace(ev, limit_account=bound)
    if not (bound and account and account != bound):
        return ev, False
    if ev.relogin_target != account:
        ev = replace(ev, relogin_target=account, relogin_attempts=0, relogin_next_at=None)
    if ev.relogin_attempts >= RELOGIN_MAX_ATTEMPTS or (ev.relogin_next_at is not None and now < ev.relogin_next_at):
        return ev, False
    return replace(_cleared_wedge(ev), relogin_attempts=ev.relogin_attempts + 1,
                   relogin_next_at=now + RELOGIN_RETRY_S * 2 ** ev.relogin_attempts), True


def mark_relogin_down(state: SupervisionState, worker_id: str) -> SupervisionState:
    """A restart took the seat down and has not confirmed a start: keep trying it until one is."""
    ev = state.workers.get(worker_id, WorkerEvidence())
    workers = dict(state.workers)
    workers[worker_id] = replace(ev, relogin_down=True)
    return replace(state, workers=workers)


def release_relogin_try(state: SupervisionState, worker_id: str) -> SupervisionState:
    """A restart the pool's suspension skipped before any kill: it does not count as a try."""
    ev = state.workers.get(worker_id, WorkerEvidence())
    workers = dict(state.workers)
    workers[worker_id] = replace(ev, relogin_attempts=max(0, ev.relogin_attempts - 1), relogin_next_at=None)
    return replace(state, workers=workers)


def tell_relogin_down(state: SupervisionState, worker_id: str) -> SupervisionState:
    """The owner was told this down seat is not coming back; its later failed tries stay silent."""
    ev = state.workers.get(worker_id, WorkerEvidence())
    workers = dict(state.workers)
    workers[worker_id] = replace(ev, relogin_down_told=True)
    return replace(state, workers=workers)


def confirm_new_login(state: SupervisionState, worker_id: str, account: str | None) -> SupervisionState:
    """The seat restarted on `account`: bind it there and end the episode, so it is not restarted again."""
    ev = state.workers.get(worker_id, WorkerEvidence())
    workers = dict(state.workers)
    workers[worker_id] = replace(ev, limit_account=account, relogin_attempts=0, relogin_next_at=None,
                                 relogin_target=None, relogin_down=False, relogin_down_told=False,
                                 limit_next_at=None, limit_attempts=0, limit_exhausted=False,
                                 limit_banner=None, limit_reset_at=None)
    return replace(state, workers=workers)


def _wedge_kind(obs: Observation, ev: WorkerEvidence) -> str | None:
    """"login" (the session is logged out), "human" (a gate or limit on a frame
    that has not moved since the last tick),
    "abnormal" (cli_wedge text), "stuck" (a turn whose raw frame has not changed
    since the last tick), "undelivered" (an idle pane that owes work while its
    watcher beats: the task was never typed in), else None. A logged-out session is
    wedged whether or not it owes work: nothing routed to it can ever run. Otherwise
    only a worker that owes work is wedged; one whose watcher is not live is the
    watcher rung's case."""
    if obs.session_alive is not True:
        return None
    if obs.pane == PANE_LOGGED_OUT:
        return "login"
    if obs.work_outstanding is not True:
        return None
    if obs.pane in _HUMAN_PANES and not _wait_moved(obs, ev):
        return "human"
    if obs.pane == PANE_ABNORMAL:
        return "abnormal"
    if obs.pane == PANE_WORKING and obs.pane_id and obs.pane_id == ev.last_pane_id:
        return "stuck"
    if obs.pane == PANE_IDLE and obs.watcher_beat == LIVE:
        return "undelivered"
    return None


def _cleared_wedge(ev: WorkerEvidence) -> WorkerEvidence:
    return replace(ev, wedge_first_detected_at=None, wedge_consecutive=0,
                   wedge_escalated=False, wedge_kind=None)


def _wedge_rung(ev: WorkerEvidence, obs: Observation, now: float, *,
                sustained_ticks: int, detect_after_s: float,
                undelivered_after_s: float = UNDELIVERED_AFTER_S) -> tuple[WorkerEvidence, str]:
    """Same sustain and stale line as death, and one decision per episode: a gate
    or limit asks for a terminal card; abnormal text asks for a card naming its cause; a frozen
    turn asks for a card offering Escape; an expired login asks for a card on
    the tick that reads it, since the banner is the CLI's own words and not a
    timing question a sustain could answer. No wedge kind restarts a session.
    The one decision is per (episode, kind): a pane that moves from one wedge
    straight into another is owed the new kind's decision, since the card already
    up names a condition the pane no longer shows."""
    if ev.wedge_kind == "human" and (
            obs.session_alive is not True or obs.pane in (None, PANE_UNKNOWN)
            or (obs.pane in _HUMAN_PANES and obs.work_outstanding is None)):
        return ev, NOTHING
    kind = _wedge_kind(obs, ev)
    ev = replace(ev, last_pane_id=obs.pane_id)
    if kind is None:
        return _cleared_wedge(ev), NOTHING
    # A legacy acknowledgment cannot prove a login card exists; other kinds
    # retain their existing compatibility behavior.
    if ev.wedge_escalated and (ev.wedge_kind not in (None, kind)
                              or (ev.wedge_kind is None and kind == "login")):
        ev = replace(ev, wedge_escalated=False)
    if kind == "undelivered" and ev.wedge_kind != "undelivered":
        # Its threshold is time spent idle and owing, not time since an earlier wedge began.
        ev = replace(ev, wedge_first_detected_at=None, wedge_consecutive=0)
    ev = replace(
        ev,
        wedge_kind=kind,
        wedge_consecutive=ev.wedge_consecutive + 1,
        wedge_first_detected_at=(ev.wedge_first_detected_at
                                 if ev.wedge_first_detected_at is not None else now),
    )
    elapsed = now - ev.wedge_first_detected_at
    if kind != "login" and (ev.wedge_consecutive < sustained_ticks or elapsed < detect_after_s):
        return ev, NOTHING
    if kind == "undelivered" and elapsed < undelivered_after_s:
        return ev, NOTHING
    if ev.wedge_escalated:
        return ev, NOTHING
    # A card decision repeats each tick until `acknowledge` records that it was raised.
    return ev, WEDGE_CARDS[kind]


def acknowledge(state: SupervisionState, worker_ids) -> SupervisionState:
    """Record that these workers' wedge cards exist, so the episode asks no more."""
    workers = dict(state.workers)
    for w in worker_ids:
        if w in workers:
            workers[w] = replace(workers[w], wedge_escalated=True)
    return replace(state, workers=workers)


def _cleared_session(ev: WorkerEvidence) -> WorkerEvidence:
    return replace(ev, first_detected_at=None, consecutive=0,
                   recover_issued_at=None, escalated=False)


def clear_death_ladder(state: SupervisionState, worker_ids) -> SupervisionState:
    """Reset only the session (death) ladder of these workers; wedge and watcher evidence stand."""
    workers = dict(state.workers)
    for w in worker_ids:
        if w in workers:
            workers[w] = _cleared_session(workers[w])
    return replace(state, workers=workers)


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
             fast_sustained_ticks: int = FAST_SUSTAINED_TICKS,
             account: str | None = None,
             ) -> tuple[SupervisionState, dict[str, str]]:
    """Advance one tick. Returns the new state and one decision per worker. `account` is the
    account on disk now (None: unread), which the new-login rule compares a parked seat with."""
    known = account if account is not None else state.last_account
    if is_resume(now, state.last_sample_at,
                 expected_period_s=expected_period_s, slack_s=slack_s):
        # Discarded as evidence: the clock moved, the pool did not necessarily die.
        # A gone session is not explained by a sleep, so one that still owes work
        # is noted as first detection, and the next tick can confirm it.
        workers = dict(state.workers)
        for worker_id, obs in observations.items():
            if (not obs.paused and obs.work_outstanding is True
                    and _is_death_evidence(obs)):
                ev = workers.get(worker_id, WorkerEvidence())
                workers[worker_id] = replace(
                    ev, consecutive=ev.consecutive + 1,
                    first_detected_at=(ev.first_detected_at
                                       if ev.first_detected_at is not None else now))
        return (SupervisionState(last_sample_at=now, workers=workers, last_account=known,
                                 relogin_announced=state.relogin_announced),
                {w: NOTHING for w in observations})

    workers = dict(state.workers)
    decisions: dict[str, str] = {}

    for worker_id, obs in observations.items():
        if obs.paused:
            # Owner-paused outranks every signal, and observing a paused worker
            # never clears the pause. Its evidence is left as it was, except that an
            # undelivered episode ends: a pause is not time a delivery was refused.
            ev = workers.get(worker_id)
            if ev is not None and ev.wedge_kind == "undelivered":
                workers[worker_id] = _cleared_wedge(ev)
            decisions[worker_id] = NOTHING
            continue

        ev = workers.get(worker_id, WorkerEvidence())

        ev, restart = _new_login(ev, obs, now, account, state.last_account)
        if restart:
            workers[worker_id] = ev
            decisions[worker_id] = RESTART_NEW_LOGIN
            continue

        if obs.session_alive is True and obs.pane == PANE_LIMIT and obs.limit_waits:
            ev = _cleared_wedge(ev)
            if obs.work_outstanding is not True:
                workers[worker_id] = ev
                decisions[worker_id] = NOTHING
                continue
            reset = obs.limit_reset_at
            if obs.limit_banner is not None and obs.limit_banner == ev.limit_banner:
                reset = ev.limit_reset_at
            else:
                ev = replace(ev, limit_banner=obs.limit_banner, limit_reset_at=reset)
            due = ev.limit_next_at
            if due is None:
                due = reset if reset is not None else now + LIMIT_PROBE_S
            elif reset is not None:
                due = max(due, reset)
            ev = replace(ev, limit_next_at=due)
            decision = NOTHING
            if now >= due and not ev.limit_exhausted:
                if ev.limit_attempts >= LIMIT_MAX_ATTEMPTS:
                    decision = ESCALATE
                    ev = replace(ev, limit_exhausted=True)
                else:
                    decision = RETRY_LIMIT
                    ev = replace(ev, limit_attempts=ev.limit_attempts + 1,
                                 limit_next_at=now + LIMIT_PROBE_S * 2 ** ev.limit_attempts)
            workers[worker_id] = ev
            decisions[worker_id] = decision
            continue
        # A refused probe can show a spinner first; only a finished turn ends the episode.
        if obs.session_alive is True and obs.pane == PANE_IDLE:
            ev = replace(ev, limit_next_at=None, limit_attempts=0, limit_exhausted=False,
                         limit_banner=None, limit_reset_at=None)

        # A session that answers contradicts "the session is gone", so it clears a
        # death as surely as a beat does; an UNANSWERED probe does not, and holds.
        # Under a live session the watcher ladder runs instead, on its own clock.
        if obs.beat == LIVE or obs.session_alive is True:
            ev, decision = _watcher_rung(
                _cleared_session(ev), obs, now, sustained_ticks=sustained_ticks,
                detect_after_s=detect_after_s, escalate_after_s=escalate_after_s)
            ev, wedge = _wedge_rung(
                ev, obs, now, sustained_ticks=sustained_ticks,
                detect_after_s=detect_after_s)
            workers[worker_id] = ev
            decisions[worker_id] = wedge if wedge != NOTHING else decision
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
        needed = fast_sustained_ticks if obs.work_outstanding is True else sustained_ticks
        if ev.consecutive >= needed:
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

    return SupervisionState(last_sample_at=now, workers=workers, last_account=known,
                            relogin_announced=state.relogin_announced), decisions
