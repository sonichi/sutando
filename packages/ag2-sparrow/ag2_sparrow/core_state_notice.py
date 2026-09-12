"""Core-state notices: tell a room WHY the agent is silent, instead of nothing.

The transport keeps accepting and queuing tasks while the core runtime cannot
answer them — usage limit hit, logged out, crashed, wedged at an interactive
prompt. From the sender's side that was a silent drop: the message posts, the
agent never replies, and no surface says why. The
detection already exists — sutando's core-input-watch writes a refined state to
``state/core-supervisor.json`` every tick — but nothing on the task path
consumed it.

This module closes that gap without touching delivery semantics:

  * The task is ALWAYS still queued (durability is the feature); the notice
    only explains the delay.
  * A room with a queued-but-unanswered task gets ONE degraded notice per
    (room, reason) per cooldown window, so a burst of messages during one
    outage produces one line, while a new failure mode re-notices.
  * When the core comes back, each noticed room gets one recovery line and its
    recovery debt (``active``) clears. Its cooldown history (``last_sent``)
    deliberately survives, so a fresh outage within the window won't re-notice
    the same reason.
  * Notice bodies are built ONLY from fixed strings in this module. The
    supervisor file's ``prompt``/``detail`` carry core pane text; rooms span
    tiers (guest/team), so file content is never forwarded.

Fail-quiet by design: no supervisor file (standalone ag2-sparrow installs), a
malformed file, or an unrecognized state each mean "do nothing", never a
guess. There is deliberately no mtime-staleness gate — see read_core_state.
Flap-bounded: cooldown history survives recovery (see _load_ledger), so no
sequence of state changes — degraded reasons alternating, or degraded/healthy
flapping — can exceed one notice per (room, reason) plus one recovery per
delivered notice per cooldown window.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

# Cap notices per sweep so one pass can't run an unbounded run of slow inline
# sends; overflow carries to the next sweep.
MAX_NOTICES_PER_SWEEP = 8
# A recovery target that keeps failing to deliver is purged after this many
# attempts, so a room the bot lost access to can't be retried forever.
MAX_RECOVERY_ATTEMPTS = 5
# Soft budget checked BEFORE each send (an in-flight send can overrun by its own
# timeout): bounds how many further sends a sweep starts on the poll path.
NOTICE_BUDGET_S = 12.0
# Status stamps land at work-step boundaries, so any step > ~90s reads "hung"
# mid-work; senders must only hear about wedges that HOLD this long.
HUNG_NOTICE_MIN_HOLD_S = 600.0

# Process-local overlay of accounting that couldn't persist to disk, so a
# write failure doesn't re-send every sweep; cleared once a disk write succeeds.
_MEM_LEDGERS: dict = {}

# Round-robin cursors so an always-failing prefix (rooms that earn no cooldown)
# can't occupy every capped batch and starve rooms behind them.
_RR_CURSORS: dict = {}


def _rotate(seq, key, size):
    """The next `size` items of `seq` starting at this key's cursor. Does NOT
    advance the cursor — the caller advances it by the number ACTUALLY attempted
    (NoticePlan.commit), so a pass cut short by the time budget resumes where it
    stopped rather than replaying the same prefix. Applies
    even when len(seq) <= size, since the budget can truncate a small batch too."""
    seq = list(seq)
    if not seq:
        return seq
    n = min(size, len(seq))  # never exceed the set (doubling must not duplicate)
    cur = _RR_CURSORS.get(key, 0) % len(seq)
    return (seq + seq)[cur:cur + n]

CORE_SUPERVISOR_FILE = "core-supervisor.json"
# Written every tick by core-input-watch; its freshness is the watcher's
# liveness (the state file's write-on-change mtime can't serve this role).
CORE_HEARTBEAT_FILE = "core-supervisor-heartbeat"
# Older than this → watcher presumed dead, last state is history not evidence.
# Generous vs the ~3s tick so a brief hiccup never reads as death.
WATCHER_STALE_S = 120
# With NO heartbeat, trust the state file only while its mtime is this young:
# nothing ever deletes it, so unbounded trust notices rooms forever.
ABSENT_HEARTBEAT_TRUST_S = 1800
# A heartbeat slightly ahead is clock jitter; further into the future is
# implausible and treated as invalid rather than fresh.
_HEARTBEAT_FUTURE_SKEW_S = 5
# Gateway's ledger name; each surface passes its own `ledger_name` so bridges
# sharing a workspace never collide on one file or cross-suppress cooldowns.
LEDGER_FILE = "core-state-notice.json"

_FIELD_MAX = 200  # supervisor fields are another process's output — bound them

# Healthy = the core is demonstrably serving. `blocked-known` is NOT here:
# only some known gates are auto-answered, so the state can hold indefinitely.
_HEALTHY_STATES = frozenset({"idle-ready", "running"})

# reason key → user-facing phrase. Keys are stable identifiers (they live in
# the on-disk ledger); phrases are the only text a room ever sees.
_REASONS = {
    "usage-limit": "my AI core has hit its usage limit and is waiting for the "
                   "limit to reset (or for my owner to intervene)",
    "logged-out": "my AI core is logged out and needs my owner to sign in again",
    "crashed": "my core process is not running",
    "hung": "my core looks stalled and may need my owner's attention",
    "blocked": "my core is stopped at a prompt that needs my owner",
}

# blocked-human refines by gate kind; unlisted kinds share the generic
# "blocked" phrase — the owner escalation reaches only the owner, not senders.
_BLOCKED_KIND_TO_REASON = {
    "session-limit": "usage-limit",
    "fable-limit-unfocused": "usage-limit",
    "login": "logged-out",
}

_STATE_TO_REASON = {
    "crashed": "crashed",
    "logged-out": "logged-out",
    "hung": "hung",
}

# The gateway's original suffix; other surfaces (discord/slack/telegram) pass
# their own via the `suffix` arg so the notice names the right transport.
_NOTICE_SUFFIX = " _(automated notice from the agent's gateway)_"


def _degraded_body(reason_key: str, suffix: str = _NOTICE_SUFFIX) -> str:
    return (f"⚠️ I received your message, but I can't work on it right now: "
            f"{_REASONS[reason_key]}. It's queued and I'll pick it up as soon "
            f"as I'm back.{suffix}")


def _recovery_body(suffix: str = _NOTICE_SUFFIX) -> str:
    return ("✅ I'm back online — working through the messages that arrived "
            f"while I was unavailable.{suffix}")


def _bounded_str(v) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v[:_FIELD_MAX] if v else None


def _watcher_liveness(state_dir: Path, now: float) -> str:
    """Heartbeat freshness as one of: "fresh" (watcher alive), "stale" (watcher
    died), "absent" (no heartbeat file), "invalid" (unreadable/garbled).

    Separate from the state file's mtime on purpose: core-supervisor.json is
    write-on-change, so its mtime tracks the last STATE CHANGE, not the
    watcher's liveness — a genuine hours-long outage leaves it arbitrarily old
    while the watcher is perfectly alive. The heartbeat is rewritten every tick,
    so ITS age is the watcher's age.

    "absent" and "invalid" are DISTINCT: absent means an
    old watcher that predates the heartbeat (trust the state, for compat);
    invalid means the file is there but garbage (freshness genuinely unknown →
    the caller withholds a verdict rather than trusting a possibly-dead watcher).
    Writes are atomic (os.replace), so a reader never sees a torn write — an
    invalid heartbeat is real corruption/tampering, not a benign race."""
    try:
        ts = float((Path(state_dir) / CORE_HEARTBEAT_FILE).read_text().strip())
    except FileNotFoundError:
        return "absent"
    except Exception:  # noqa: BLE001 — present but unreadable/garbled
        return "invalid"
    # A non-finite ts (inf/nan) or an implausibly-future one would read "fresh"
    # forever and trust a dead watcher.
    if not math.isfinite(ts):
        return "invalid"
    age = now - ts
    if age < -_HEARTBEAT_FUTURE_SKEW_S:
        return "invalid"  # heartbeat from the future beyond tolerated skew
    return "fresh" if age <= WATCHER_STALE_S else "stale"


def read_core_state(state_dir: Path, now: float | None = None):
    """``core-supervisor.json`` → (state, kind) or None.

    None means "no verdict": file absent (standalone install), unreadable,
    malformed, OR the watcher's heartbeat is stale. MUST NOT raise — this runs
    inside the poll loop and a broken side-channel must never become a delivery
    blocker (same contract as the heartbeat's core-status read).

    Freshness comes from the watcher's per-tick heartbeat, NOT the state file's
    mtime. core-supervisor.json
    is write-on-change, so a stable state — an hour idle, a multi-hour
    usage-limit outage, a week-long weekly-limit one — leaves its mtime
    arbitrarily old while the content is current; an mtime gate here therefore
    silently dropped notices for any outage longer than the bound. Instead:
      * heartbeat FRESH  → the watcher is alive, trust the state however old it is
      * heartbeat STALE  → the watcher died; its last state is history, not a
                           verdict → None (closes the "notice forever after the
                           watcher dies mid-outage" gap)
      * heartbeat ABSENT → a watcher predating the heartbeat; fall back to
                           trusting the state file, but only while its mtime is
                           within ABSENT_HEARTBEAT_TRUST_S — a leftover file
                           with no live watcher must not notice forever.
      * heartbeat INVALID→ present but garbled; freshness genuinely unknown →
                           None (do NOT trust-forever).
    """
    now = now if now is not None else time.time()
    path = Path(state_dir) / CORE_SUPERVISOR_FILE
    liveness = _watcher_liveness(state_dir, now)
    if liveness in ("stale", "invalid"):
        return None  # watcher dead or freshness unknown → last state not evidence
    if liveness == "absent":
        try:
            age = now - path.stat().st_mtime
        except OSError:
            return None
        if not (-_HEARTBEAT_FUTURE_SKEW_S <= age <= ABSENT_HEARTBEAT_TRUST_S):
            return None  # no watcher vouches for it and it isn't recent
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        state = _bounded_str(data.get("state"))
        if state is None:
            return None
        return state, _bounded_str(data.get("kind"))
    except Exception:  # noqa: BLE001 — no verdict beats a wrong one
        return None


def is_core_healthy(state_dir: Path) -> bool:
    """A fresh re-read: True only when the core is CURRENTLY in a known-healthy
    state. Used to abort a recovery batch the moment the supervisor flips back to
    degraded, so a stale "back online" isn't sent."""
    verdict = read_core_state(state_dir)
    if verdict is None:
        return False  # no verdict (watcher dead / unknown) — don't send recovery
    state, kind = verdict
    return degraded_reason(state, kind) is None and state in _HEALTHY_STATES


def degraded_reason(state: str, kind: str | None) -> str | None:
    """Supervisor state → reason key, or None when the core can serve tasks.

    Deliberately an allowlist on BOTH sides: only known-degraded states notice,
    and only known-healthy states count as recovered (see sweep). Anything
    else — ``gateway-down`` (self-referential from inside the gateway), future
    states — is "no verdict", so an unrecognized value can neither spam rooms
    nor fake a recovery. ``blocked-human`` is the one deliberate exception to
    the unknown-means-silence rule: the STATE is recognized (a human gate is
    holding the core, only the owner can clear it), so an unlisted KIND still
    tells senders the generic truth rather than nothing — the "Agent needs you"
    escalation reaches only the owner's notify channel, never these rooms.
    """
    if state == "blocked-human":
        return _BLOCKED_KIND_TO_REASON.get(kind or "", "blocked")
    return _STATE_TO_REASON.get(state)


# Debounce: a state must hold this long before we notice/recover, so a login/
# restart flap fires no premature recovery or transient-gate notice.
_DEBOUNCE: dict = {}


def _debounce_ok(state_dir, ledger_name, condition, now, debounce_s):
    """True once `condition` has held continuously for >= debounce_s; resets its
    timer whenever the condition changes (a brief flap never clears it)."""
    if debounce_s <= 0:
        return True
    key = (str(state_dir), ledger_name)
    prev = _DEBOUNCE.get(key)
    if prev is None or prev[0] != condition:
        _DEBOUNCE[key] = (condition, now)
        return False
    return now - prev[1] >= debounce_s


_DEFAULT_COOLDOWN_S = 1800.0


def _cooldown_s() -> float:
    """The per-(room, reason) cooldown. Only a FINITE POSITIVE value is
    honored: nan/0/negative would make every sweep eligible
    (defeating the spam bound) and inf would mean a permanent cooldown — both are
    treated as misconfiguration and fall back to the default. Turning notices
    off is the separate, documented SPARROW_CORE_NOTICE=0 control."""
    raw = os.environ.get("SPARROW_CORE_NOTICE_COOLDOWN_S")
    if not raw:
        return _DEFAULT_COOLDOWN_S
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_COOLDOWN_S
    return v if (math.isfinite(v) and v > 0) else _DEFAULT_COOLDOWN_S


def _enabled() -> bool:
    return (os.environ.get("SPARROW_CORE_NOTICE") or "1").strip() != "0"


def _load_ledger(state_dir: Path, ledger_name: str = LEDGER_FILE) -> dict:
    """Two separated concerns:

    ``active``    room → reason: a degraded notice was DELIVERED and its
                  recovery line is still owed. Cleared per room by recovery.
    ``last_sent`` room → {reason → ts}: cooldown history. NEVER cleared by
                  recovery — only aged out past the cooldown window.

    The v1 schema kept one {reason, ts} per room doing both jobs, which broke
    both bounds: alternating reasons (usage-limit ↔ logged-out) overwrote each
    other's cooldown and re-sent on every alternation, and a degraded/healthy
    flap cleared the cooldown with the recovery, re-noticing immediately. A v1
    (or corrupt) file resets to empty: worst case one duplicate notice.

    Overlaid with any process-local accounting that failed to persist, so
    a transient ledger-write failure doesn't make this process re-send every
    sweep. ``fail`` (room → failed-recovery attempts) bounds retries."""
    disk = {"active": {}, "last_sent": {}, "fail": {}}
    try:
        data = json.loads((Path(state_dir) / ledger_name).read_text())
        if isinstance(data, dict) and data.get("schema_version") == 2:
            active = data.get("active")
            last_sent = data.get("last_sent")
            fail = data.get("fail")
            if isinstance(active, dict) and isinstance(last_sent, dict):
                disk = {
                    "active": {str(r): str(v) for r, v in active.items()
                               if isinstance(v, str)},
                    "last_sent": {
                        str(r): {str(k): float(ts) for k, ts in v.items()
                                 if isinstance(ts, (int, float))}
                        for r, v in last_sent.items() if isinstance(v, dict)
                    },
                    "fail": {str(r): int(n) for r, n in fail.items()
                             if isinstance(n, int)} if isinstance(fail, dict) else {},
                }
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 — a corrupt ledger resets, never wedges
        pass
    return _overlay_mem(state_dir, ledger_name, disk)


def _overlay_mem(state_dir: Path, ledger_name: str, disk: dict) -> dict:
    """When an unpersisted snapshot exists, it — not disk — is authoritative.

    The overlay holds the COMPLETE ledger this process last tried to write but
    couldn't persist. Single writer per surface, so disk is never newer. A
    union-merge was wrong: it
    could only ADD entries, so a recovery that cleared `active` in the snapshot
    left the stale disk `active` in place and re-sent the recovery every pass.
    Return a copy of the snapshot so DELETIONS (recovery, purge) are honored."""
    mem = _MEM_LEDGERS.get((str(state_dir), ledger_name))
    if not mem:
        return disk
    return {"active": dict(mem.get("active", {})),
            "last_sent": {r: dict(v) for r, v in mem.get("last_sent", {}).items()},
            "fail": dict(mem.get("fail", {}))}


def _save_ledger(state_dir: Path, ledger: dict, now: float,
                 ledger_name: str = LEDGER_FILE) -> None:
    """Atomic. Cooldown history past its window is dead weight — prune it here so
    the file stays bounded by the set of currently-chatty rooms.

    On a write FAILURE the accounting is stashed in the process-local overlay
    rather than silently lost, so a read-only/full disk can't make this
    process re-send every sweep — the earlier "worst case one duplicate after
    restart" comment was wrong for a persistent failure. On success the overlay
    for this key is dropped (disk is canonical again)."""
    cooldown = _cooldown_s()
    ledger["last_sent"] = {
        room: kept for room, per_reason in ledger["last_sent"].items()
        if (kept := {k: ts for k, ts in per_reason.items()
                     if now - ts < cooldown})
    }
    ledger.setdefault("fail", {})
    key = (str(state_dir), ledger_name)
    path = Path(state_dir) / ledger_name
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"schema_version": 2, **ledger}))
        os.replace(tmp, path)
        _MEM_LEDGERS.pop(key, None)  # persisted → disk is canonical
    except OSError:
        # Keep the accounting in-process so we don't re-send known-delivered
        # notices next sweep. Bounded by the chatty-room set (pruned above).
        _MEM_LEDGERS[key] = {"active": dict(ledger["active"]),
                             "last_sent": {r: dict(v) for r, v
                                           in ledger["last_sent"].items()},
                             "fail": dict(ledger["fail"])}
        try:
            tmp.unlink()
        except OSError:
            pass


class NoticePlan:
    """What a sweep WOULD send this pass, decided but not yet sent — so a caller
    that sends synchronously (gateway, slack, telegram) and one that sends with
    ``await`` (discord) can share the exact ledger + cooldown logic.

    ``kind`` ∈ {"degraded", "recovery"}; ``items`` is a list of (room, body)
    to attempt. The caller sends each, collects the rooms that SUCCEEDED, and
    passes them back to :meth:`commit` — so a failed send burns nothing and the
    next pass retries (bounded for recovery, see below). Nothing is persisted
    until commit; call it even after a partial/aborted batch so completed sends
    are recorded.
    """

    __slots__ = ("kind", "reason", "items", "_ledger", "_now",
                 "_state_dir", "_ledger_name", "_purge", "_rr_key")

    def __init__(self, kind, reason, items, ledger, now, state_dir, ledger_name,
                 purge=(), rr_key=None):
        self.kind, self.reason, self.items = kind, reason, items
        self._ledger, self._now = ledger, now
        self._state_dir, self._ledger_name = state_dir, ledger_name
        # Recovery-only: active keys that failed target validation — garbage or
        # forged destinations to DROP without sending.
        self._purge = set(purge)
        self._rr_key = rr_key  # round-robin cursor to advance by actual attempts

    def commit(self, sent_rooms, attempted_rooms=None) -> None:
        """Persist the outcome. ``sent_rooms`` delivered; ``attempted_rooms`` is
        the set for which a send was actually STARTED — a room the batch never
        reached (health-abort, exception, cancellation, time budget) is NOT in
        it and keeps its debt untouched. Defaults to every planned item for
        callers that always run the whole batch (inferring "failed" from planned
        items would charge never-attempted rooms a failure and purge them
        without a single send)."""
        sent = set(sent_rooms)
        attempted = ({room for room, _ in self.items}
                     if attempted_rooms is None else set(attempted_rooms))
        # Advance the cursor by what we ACTUALLY attempted, before any early
        # return, so a batch cut short (budget/health/all-failed) resumes.
        if self._rr_key is not None and attempted:
            _RR_CURSORS[self._rr_key] = _RR_CURSORS.get(self._rr_key, 0) + len(attempted)
        if self.kind == "degraded":
            if not sent:
                return  # nothing delivered → nothing to persist
            for room in sent:
                self._ledger["last_sent"].setdefault(room, {})[self.reason] = self._now
                self._ledger["active"][room] = self.reason
        else:  # recovery: discharge delivered, purge invalid, bound retries
            failed = attempted - sent  # ATTEMPTED but did not deliver
            fail = self._ledger.setdefault("fail", {})
            purge = set(self._purge)
            for room in failed:
                # A target that keeps failing delivery is retried a bounded
                # number of times then dropped — a recovery line is benign.
                fail[room] = fail.get(room, 0) + 1
                if fail[room] >= MAX_RECOVERY_ATTEMPTS:
                    purge.add(room)
            if not sent and not failed and not purge:
                return
            for room in sent | purge:
                self._ledger["active"].pop(room, None)
                fail.pop(room, None)
        _save_ledger(self._state_dir, self._ledger, self._now, self._ledger_name)


def plan_notices(state_dir, rooms, now=None, ledger_name: str = LEDGER_FILE,
                 suffix: str = _NOTICE_SUFFIX,
                 recovery_target_ok=None, debounce_s=0.0) -> "NoticePlan | None":
    """Read core state + ledger and decide what to send — WITHOUT sending.

    Returns None when there is nothing to do (feature disabled, no verdict, an
    unrecognized state, cooldown covers every room, or no recovery owed). The
    same spam bound holds regardless of caller: per room, ≤ one degraded notice
    per reason per cooldown window, and ≤ one recovery per DELIVERED notice.
    ``suffix`` lets a surface name its own transport in the notice body.

    ``recovery_target_ok(room) -> bool`` (optional) validates ledger-derived
    RECOVERY destinations against the surface's own id rules before
    sending. Degraded targets come from the caller (authentic
    intake ids) and are never validated here. A recovery key that fails is
    neither sent to NOR retried — it is purged from the ledger, so a corrupt or
    forged `active` entry can't cause repeated sends to a junk destination.
    (Platform APIs already reject sends to chats/rooms the bot isn't in; this
    adds shape-checking and stops garbage keys from wedging the ledger.)
    """
    if not _enabled():
        return None
    verdict = read_core_state(state_dir, now)
    if verdict is None:
        return None  # no evidence either way — neither notice nor recovery
    now = now if now is not None else time.time()
    state, kind = verdict
    reason = degraded_reason(state, kind)
    ledger = _load_ledger(state_dir, ledger_name)
    if reason is not None:
        # Don't notice until the state persists (no login/restart-flap fire);
        # "hung" gets a much longer hold — see HUNG_NOTICE_MIN_HOLD_S.
        eff_debounce = (max(debounce_s, HUNG_NOTICE_MIN_HOLD_S)
                        if reason == "hung" else debounce_s)
        if not _debounce_ok(state_dir, ledger_name, "deg:" + reason, now, eff_debounce):
            return None
        cooldown = _cooldown_s()
        items = [(room, _degraded_body(reason, suffix))
                 for room in sorted(set(rooms))
                 if not (0 <= now - (ledger["last_sent"].get(room, {})
                                     .get(reason, -cooldown - 1)) < cooldown)]
        # Cap per sweep, rotating the window so an always-failing prefix
        # can't starve rooms behind it; overflow re-plans next pass.
        rr_key = (str(state_dir), ledger_name, "degraded")
        window = _rotate(items, rr_key, MAX_NOTICES_PER_SWEEP)
        return NoticePlan("degraded", reason, window,
                          ledger, now, state_dir, ledger_name, rr_key=rr_key)
    if state not in _HEALTHY_STATES:
        return None  # unrecognized state — not proof of recovery
    # Debounce: recover only once STABLY healthy (no premature "back online").
    if not _debounce_ok(state_dir, ledger_name, "healthy", now, debounce_s):
        return None
    if not ledger["active"]:
        return None
    active = sorted(ledger["active"])
    if recovery_target_ok is not None:
        purge = {room for room in active if not recovery_target_ok(room)}
        active = [room for room in active if room not in purge]
    else:
        purge = set()
    if not active and not purge:
        return None
    # Cap sends per sweep with the same rotation; overflow stays in
    # `active` for the next pass. Purges are cheap (no network) so all apply now.
    rr_key = (str(state_dir), ledger_name, "recovery")
    window = _rotate(active, rr_key, MAX_NOTICES_PER_SWEEP)
    items = [(room, _recovery_body(suffix)) for room in window]
    return NoticePlan("recovery", None, items, ledger, now,
                      state_dir, ledger_name, purge=purge, rr_key=rr_key)


def sweep_core_state_notices(state_dir: Path, rooms, send, log=None,
                             now: float | None = None,
                             ledger_name: str = LEDGER_FILE,
                             suffix: str = _NOTICE_SUFFIX,
                             recovery_target_ok=None, debounce_s=0.0) -> None:
    """One synchronous pass: notice degraded, announce recovery, persist.

    Thin wrapper over :func:`plan_notices` for a caller with a synchronous
    ``send(room, body) → bool`` (the gateway bridge and the slack/telegram
    bridges — all synchronous). ``send`` returning False means not delivered,
    so no ledger entry is written and the next pass retries; exceptions from
    ``send`` propagate (the caller owns auth/transport policy) — but only AFTER
    the sends that DID succeed are committed, so a
    later 401 can't strip an earlier room's cooldown. ``ledger_name`` and
    ``suffix`` let each surface keep its own ledger + wording (gateway keeps the
    defaults). ``recovery_target_ok`` validates ledger-derived recovery
    destinations (see :func:`plan_notices`). Async callers (discord) use
    ``plan_notices`` + ``NoticePlan.commit`` directly.

    Recovery batches revalidate the core is STILL healthy before each send:
    if the supervisor has flipped back to degraded mid-batch, remaining "back
    online" lines are obsolete, so the batch stops and only completed work is
    committed — the still-degraded rooms keep their recovery debt.

    A wall-clock budget (NOTICE_BUDGET_S) bounds how long this can hold the
    caller's thread on slow sends, so notice IO on the gateway/telegram main
    path can't stall polling for the full per-request timeout budget.
    Rooms not reached (budget, health-abort, or a raised send)
    are NOT charged a failure — only rooms actually attempted are.
    """
    plan = plan_notices(state_dir, rooms, now, ledger_name, suffix,
                        recovery_target_ok, debounce_s)
    if plan is None:
        return
    verb = "notice" if plan.kind == "degraded" else "recovery notice"
    sent, attempted = [], []
    deadline = time.monotonic() + NOTICE_BUDGET_S
    try:
        for room, body in plan.items:
            if time.monotonic() >= deadline:
                break  # bounded main-path time; the rest re-plan next sweep
            if plan.kind == "recovery" and not is_core_healthy(state_dir):
                break  # core degraded again — stop sending stale recoveries
            attempted.append(room)
            if send(room, body):
                sent.append(room)
                if log:
                    extra = f" ({plan.reason})" if plan.kind == "degraded" else ""
                    log(f"core-state {verb}{extra} sent to {room}")
    finally:
        # Commit completed sends even if a later one raised; charge failures
        # only to rooms actually attempted, not budget/health-skipped ones.
        plan.commit(sent, attempted_rooms=attempted)
