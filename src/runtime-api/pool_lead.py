#!/usr/bin/env python3
"""Lead-side assignment engine (lead-follower pool, slice L1).

One brain for scheduling policy: priority ordering, channel affinity, and
least-loaded fallback all live here, testable without a follower running.
Assignment is an atomic rename (`task-X.txt` -> `task-X.assigned-<inst>.txt`),
so a crash mid-sweep loses nothing and never double-assigns. Followers (L2)
execute only their assignments; on a stale lead beat they fall back to
leaderless claiming — this module is inert then by construction (no process).

Everything is injected (dirs, follower enumeration, liveness, clock) so tests
compose tmp dirs and fake pools. See docs/lead-follower-pool.md.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
from task_priority import sort_tasks_by_priority  # noqa: E402
from pool_routing import (  # noqa: E402
    Decision, WorkerView, build_router, read_task_meta)

# Depth at which channelless work overflows to the idle lane core; room
# affinity itself is binding and never yields on load (owner 2026-08-26).
AFFINITY_BUSY_MAX = max(1, int(os.environ.get("SUTANDO_AFFINITY_BUSY_MAX", "3")))
ASSIGN_STUCK_S = 300         # assigned but unclaimed this long → repool
BUSY_DEFER_MAX_S = 1800.0    # busy defers repool this long, then wedge rules apply
DONE_FLAG_RETENTION_S = 7 * 86400
BUSY_EXIT_GRACE_S = 60.0     # claim window left after a busy spell ends
NOCLAIM_COOLDOWN_S = ASSIGN_STUCK_S  # repool pops ledger; keep follower marked non-claiming

# Leading id component that is an epoch in ms or s; restore/re-clone resets
# mtime, so the name is the only immutable birth time a done-flag carries.
_FLAG_STAMP_RE = re.compile(r"^task-(\d{13}|\d{10})(?![0-9A-Za-z])")

# ids legitimately contain dots (task-<inst>~<id>), so exclude the
# assigned/claimed states explicitly rather than banning dots
_TASK_RE = re.compile(
    r"^task-(?!.*\.(?:assigned|claimed)-)[A-Za-z0-9._~-]+\.txt$")


def _flag_stamp_s(name: str) -> "float | None":
    m = _FLAG_STAMP_RE.match(name)
    if not m:
        return None
    digits = m.group(1)
    return int(digits) / (1000.0 if len(digits) == 13 else 1.0)
_CHANNEL_RE = re.compile(r"^(?:channel_id|chat_id):\s*(\S+)", re.M)
_TARGET_RE = re.compile(r"^target_worker:\s*(\S+)", re.M)
_FANOUT_RE = re.compile(r"^fan_out:\s*true\s*$", re.M | re.I)
_INST_RE = re.compile(r"^[A-Za-z0-9@:._-]{1,128}$")
_LANE_RE = re.compile(
    r"^(?P<key>access_tier|priority|interaction_type):\s*(?P<val>\S+)", re.M)


def _read_channel(path: Path) -> "str | None":
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    m = _CHANNEL_RE.search(text)
    return m.group(1) if m else None


class _FlockCtx:
    """flock is per-open-file-description: each ctx opens its own fd so two
    holders in one process still serialize correctly."""
    def __init__(self, path: Path):
        self._path = path

    def __enter__(self):
        self._fh = open(self._path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self._fh

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()
        return False


def _read_addressing(path: Path) -> "tuple[str | None, bool]":
    """(target_worker, fan_out) from the task header — the per-message
    address outranks any room binding (owner semantics 2026-08-31)."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None, False
    # Headers end at the task: delimiter — a body line can never forge
    # routing (same containment rule as parse_task_headers).
    m = re.search(r"^task:", text, re.M)
    head = text[:m.start()] if m else text
    t = _TARGET_RE.search(head)
    return (t.group(1) if t else None), bool(_FANOUT_RE.search(head))


def _read_lane(path: Path) -> str:
    """'routine' for non-owner senders and low-priority/self-driven work;
    'owner' otherwise. Unreadable or unenumerated headers fail to 'owner' —
    a malformed header must never shunt an owner message behind maintenance."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return "owner"
    fields = dict(m.group("key", "val") for m in _LANE_RE.finditer(text))
    tier = fields.get("access_tier")
    if (tier is not None and tier != "owner") or fields.get("priority") == "low" \
            or fields.get("interaction_type") == "self_reflective":
        return "routine"
    return "owner"


class PoolLead:
    def __init__(self, tasks_dir, state_dir, followers_fn, alive_fn,
                 now_fn=time.time, metrics=None, results_dir=None,
                 runtime_fn=None, core_fn=None, router=None):
        """followers_fn() -> list of instance ids eligible for assignment.
        alive_fn(instance) -> bool (fresh heartbeat). Both injected — the
        production binder wires instance_registry + the .alive files.
        runtime_fn(instance) -> 'claude'|'codex'; absent means all-claude,
        which is what every pool was before the runtime dimension existed."""
        self.runtime_fn = runtime_fn or (lambda _inst: "claude")
        # tie-break memory: an all-idle pool rotates instead of always
        # handing every load-tie to the lexicographically-first follower
        self._last_pick: "dict[str, float]" = {}
        self.tasks_dir = Path(tasks_dir)
        self.state_dir = Path(state_dir)
        self.results_dir = (Path(results_dir) if results_dir
                            else self.tasks_dir.parent / "results")
        self.followers_fn = followers_fn
        self.alive_fn = alive_fn
        self.now = now_fn
        self.metrics = metrics  # PoolMetrics or None; recording is optional
        # core_fn(): core id while its beat is fresh, else None; never a follower.
        self.core_fn = core_fn or (lambda: None)
        self.router = router or build_router(self.state_dir, self._default_pick)

    # ── affinity table (single-writer: the lead) ────────────────────────────
    def _affinity_path(self) -> Path:
        return self.state_dir / "pool" / "affinity.json"

    def _load_affinity(self) -> dict:
        try:
            data = json.loads(self._affinity_path().read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_affinity(self, table: dict) -> None:
        p = self._affinity_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(table))
        os.replace(tmp, p)

    def _affinity_lock(self) -> _FlockCtx:
        """Serializes table read-modify-write against the pin CLI, which runs
        in another process; plain reads of the atomic file need no lock."""
        path = self._affinity_path().with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        return _FlockCtx(path)

    # ── explicit pins: owner-declared room -> instance (2026-08-30) ─────────
    def pin_room(self, channel: str, instance: "str | list[str]",
                 dedicated: bool = False) -> dict:
        """Pin a room to one worker, or to a bound SET (pool-restriction
        semantics: the lead routes each task to one member of the set)."""
        instances = [instance] if isinstance(instance, str) else [
            str(i) for i in instance if str(i).strip()]
        if not instances:
            raise ValueError("pin_room: empty worker set")
        with self._affinity_lock():
            table = self._load_affinity()
            row = {"instance": instances[0], "ts": self.now(), "pinned": True}
            if len(instances) > 1:
                row["instances"] = instances
            if dedicated:
                row["exclusive"] = True
            table[channel] = row
            self._save_affinity(table)
            return table[channel]

    def unpin_room(self, channel: str) -> bool:
        """Drop only the pin flag; the binding stays and auto re-homing
        (death/wedge moves the room) resumes for it."""
        with self._affinity_lock():
            table = self._load_affinity()
            row = table.get(channel)
            if not (isinstance(row, dict) and row.get("pinned")):
                return False
            row.pop("pinned", None)
            row.pop("exclusive", None)
            row.pop("instances", None)
            self._save_affinity(table)
            return True

    def bindings(self) -> dict:
        return self._load_affinity()

    # ── liveness trace (change-driven; forensic aid for routing anomalies) ──
    def _trace_path(self) -> Path:
        return self.state_dir / "pool" / "liveness-trace.jsonl"

    def _trace(self, event: dict) -> None:
        """Append-only JSONL; a trace failure must never break assignment."""
        try:
            p = self._trace_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > 5_000_000:
                p.replace(p.with_suffix(".jsonl.1"))  # one-deep rotation
            with open(p, "a") as f:
                f.write(json.dumps(event) + "\n")
        except OSError:
            pass

    # ── pool state ──────────────────────────────────────────────────────────
    def _live_followers(self) -> "list[str]":
        return [f for f in self.followers_fn()
                if _INST_RE.match(str(f)) and self.alive_fn(f)]

    def _claimed_load(self, instance: str) -> int:
        """Claims only — assigned-but-unclaimed files do not count. A core
        with claims in flight is BUSY, which is not the same as wedged."""
        pat = re.compile(rf"\.claimed-{re.escape(instance)}\.txt$")
        try:
            return sum(1 for f in self.tasks_dir.iterdir()
                       if pat.search(f.name))
        except OSError:
            return 0

    def _load(self, instance: str) -> int:
        pat = re.compile(
            rf"\.(?:assigned|claimed)-{re.escape(instance)}\.txt$")
        try:
            return sum(1 for f in self.tasks_dir.iterdir()
                       if pat.search(f.name))
        except OSError:
            return 0

    def _lane_core_of(self, followers: "list[str]") -> "str | None":
        """Highest CLAUDE core is the routine lane (owner 2026-08-25); a codex
        follower sweeps on a timer, so maintenance there waits for a poll."""
        eligible = [f for f in followers
                    if self.runtime_fn(f) == "claude"] or followers
        return (max(eligible, key=lambda f: (len(str(f)), str(f)))
                if len(followers) > 1 else None)

    def _pick(self, channel: "str | None", followers: "list[str]",
              affinity: dict, lane: str = "owner") -> str:
        # Soft lanes (owner 2026-08-23): owner traffic stays off the lane
        # core except as saturated-pool overflow.
        eligible = [f for f in followers
                    if self.runtime_fn(f) == "claude"] or followers
        lane_core = self._lane_core_of(followers)
        # A dedicated worker serves only its own room (owner 2026-08-30);
        # reserved-elsewhere workers leave the general rotation entirely.
        reserved = {r.get("instance") for ch, r in affinity.items()
                    if isinstance(r, dict) and r.get("exclusive")
                    and ch != channel}
        # Wedge escape: the routine pin holds only while the lane core is
        # below the busy cap and demonstrably claiming; else fall through.
        if (lane == "routine" and lane_core is not None
                and lane_core not in reserved
                and self._load(lane_core) < AFFINITY_BUSY_MAX
                and self._claiming(lane_core)):
            return lane_core
        # owner prefers claude seats; a sole claude doubles as lane core yet stays
        # owner-eligible. All-reserved pool falls back — never starve a task.
        free = [f for f in eligible if f not in reserved] or eligible
        primary = [f for f in free if f != lane_core] or free
        # A repool drops the follower's load, so least-loaded actively PREFERS
        # the core that just failed to claim. Never narrow to empty.
        primary = [f for f in primary if self._claiming(f)] or primary
        if channel:
            row = affinity.get(channel)
            # Binding: a live home core keeps its room; only death or an
            # unclaimed-reclaim moves it. Explicit pins beat lane defaults.
            bound = row.get("instances") if isinstance(row, dict) else None
            if isinstance(bound, list) and row.get("pinned"):
                # Bound SET: the selected workers ARE the room's pool; pick
                # the least-loaded claiming member, whole set busy -> loan.
                live = [i for i in bound if i in followers
                        and self._claiming(i)]
                if live:
                    pick = min(live, key=lambda f: (
                        self._load(f), self._last_pick.get(str(f), 0.0),
                        str(f)))
                    self._last_pick[str(pick)] = self.now()
                    return pick
            elif isinstance(row, dict) and row.get("instance") in followers:
                # A pinned home that stopped claiming is loaned out below;
                # the pin survives, so the room returns when it recovers.
                if not row.get("pinned") or self._claiming(row["instance"]):
                    self._last_pick[str(row["instance"])] = self.now()
                    return row["instance"]
        # equal load -> least-recently-picked, so an idle pool round-robins
        pick = min(primary, key=lambda f: (
            self._load(f), self._last_pick.get(str(f), 0.0), str(f)))
        if (lane_core is not None and lane_core not in reserved
                and self._load(pick) >= AFFINITY_BUSY_MAX
                and self._load(lane_core) == 0 and self._claiming(lane_core)):
            pick = lane_core  # overflow: owner lane saturated, lane idle
        self._last_pick[str(pick)] = self.now()
        return pick

    # ── routing policy seam ───────────────────────────────────────────────
    def _default_pick(self, task, workers, affinity, state):
        """`affinity-first`: the lead's historical choice over the follower
        subset — the core joins only when a configured policy names it."""
        followers = [w.id for w in workers if not w.is_core]
        if not followers:
            core = next((w.id for w in workers if w.is_core), None)
            return core
        return self._pick(task.channel, followers, affinity, task.lane)

    def _members(self, followers: "list[str]") -> "list[WorkerView]":
        views = [WorkerView(str(i), self._load(i), self._claiming(i),
                            self.runtime_fn(i)) for i in followers]
        core = self.core_fn()
        if core:
            views.append(WorkerView(str(core), self._load(core),
                                    self._claiming(core), "claude", True))
        return views

    def _route(self, f: Path, channel, followers, affinity, lane) -> str:
        members = self._members(followers)
        task = read_task_meta(f, lane)
        d: Decision = self.router.pick(task, members, affinity)
        inst = d.worker
        if inst is None or inst not in {m.id for m in members}:
            inst = self._pick(channel, followers, affinity, lane)
            d = Decision(inst, d.policy, d.rule, True,
                         d.reason or "policy declined; lead default")
        self._trace({"ts": self.now(), "event": "routed", "task": f.name,
                     "inst": str(inst), "policy": d.policy, "rule": d.rule,
                     "fallback": d.fallback, "reason": d.reason})
        return str(inst)

    def _fan_out(self, f: Path, followers: "list[str]") -> "list[tuple[str, str]]":
        """One assigned COPY per claiming worker; the original is archived so
        it can't double-assign. Each copy gets a per-worker id suffix so
        results and dedup stay distinct."""
        live = [i for i in followers if self._claiming(i)]
        if not live:
            return []
        stem = f.name[:-len(".txt")]
        if self._fanned_already(stem):
            # A prior fan-out survived a failed archive: retire the original
            # instead of re-copying, or a claimed slot gets a second assignment.
            self._retire_original(f)
            return []
        try:
            body = f.read_text(errors="replace")
        except OSError:
            return []
        out = []
        for inst in sorted(live):
            copy = f.with_name(f"{stem}~{inst}.assigned-{inst}.txt")
            try:
                copy.write_text(body)
                out.append((copy.name, inst))
            except OSError:
                continue
        if out:
            self._retire_original(f)
        return out

    def _fanned_already(self, stem: str) -> bool:
        """~-suffixed traces of a prior fan-out: live/claimed copies in
        tasks/, archived copies, or per-copy results in any disposition."""
        pat = f"{stem}~*"
        dirs = (self.tasks_dir, self.tasks_dir / "archive",
                self.results_dir, self.results_dir / "archive",
                self.results_dir / "undelivered")
        return any(any(d.glob(pat)) for d in dirs)

    def _retire_original(self, f: Path) -> None:
        # Failure leaves the original for the next sweep, where the entry
        # guard retires it again instead of re-fanning: idempotent by entry.
        try:
            archive = f.parent / "archive"
            archive.mkdir(parents=True, exist_ok=True)
            os.rename(f, archive / f.name)
        except OSError:
            pass

    # ── the sweep ───────────────────────────────────────────────────────────
    def sweep(self) -> "list[tuple[str, str]]":
        """Assign every unassigned task; returns [(task_name, instance)].
        Priority order first (urgent > normal > low, then mtime), so a
        burst never starves an urgent task behind low-priority backlog."""
        followers = self._live_followers()
        live = sorted(str(f) for f in followers)
        if getattr(self, "_last_live_set", None) != live:
            self._trace({"ts": self.now(), "event": "live_set_changed",
                         "alive": live, "prev": getattr(self, "_last_live_set", None)})
            self._last_live_set = live
        if not followers:
            return []  # nothing to assign TO — leave tasks for fallback mode
        try:
            pending = [f for f in self.tasks_dir.iterdir()
                       if _TASK_RE.match(f.name)]
        except OSError:
            return []
        affinity = self._load_affinity()
        out = []
        for f in sort_tasks_by_priority(pending):
            if self._result_evidence(f.name):
                # finish_task writes the result BEFORE the flag, so result-
                # without-flag is COMPLETE; reassigning would re-execute it.
                continue
            channel = _read_channel(f)
            lane = _read_lane(f)
            target, fan_out = _read_addressing(f)
            if fan_out:
                assigned = self._fan_out(f, followers)
                out.extend(assigned)
                continue
            bound = None
            if channel and isinstance(affinity.get(channel), dict):
                bound = affinity[channel].get("instance")
            if target in followers and self._claiming(target):
                inst = target  # explicit address outranks bindings + load
            else:
                inst = self._route(f, channel, followers, affinity, lane)
            if lane == "owner" and (
                    (inst == self._lane_core_of(followers) and inst != bound)
                    or (bound is not None and bound not in followers)):
                self._trace({"ts": self.now(), "event": "anomalous_owner_pick",
                             "task": f.name, "inst": str(inst), "bound": bound,
                             "channel": channel, "alive": live})
            target = f.with_name(
                f.name[:-len(".txt")] + f".assigned-{inst}.txt")
            try:
                arrived = f.stat().st_mtime  # before rename — f is gone after
            except OSError:
                arrived = self.now()
            try:
                os.rename(f, target)  # atomic; a racing writer keeps its file
                os.utime(target)  # stamp assignment time; rename keeps arrival mtime
            except OSError:
                continue
            # Only an owner-lane pick off the lane core may (re)bind a room:
            # a routine/overflow landing there must not become a sticky steal
            prev = affinity.get(channel) if channel else None
            if (channel and lane == "owner"
                    and inst != self._lane_core_of(followers)
                    and not (isinstance(prev, dict) and prev.get("pinned"))):
                affinity[channel] = {"instance": inst, "ts": self.now()}
            if self.metrics is not None:
                self.metrics.assigned(f.name, inst, channel,
                                      max(0.0, self.now() - arrived))
            out.append((f.name, inst))
        if out:
            with self._affinity_lock():
                merged = self._load_affinity()
                # Re-read under the lock: a pin written mid-sweep by the CLI
                # must win over this sweep's stale auto rebinds.
                for ch, row in affinity.items():
                    cur = merged.get(ch)
                    if not (isinstance(cur, dict) and cur.get("pinned")):
                        merged[ch] = row
                self._save_affinity(merged)
        return out

    # ── crash recovery ──────────────────────────────────────────────────────
    def reclaim_dead(self) -> "list[str]":
        """Return dead followers' assignments to the unassigned pool.
        Claimed files are NOT touched here — a claim means work may have
        side-effected; that recovery keeps the done-flag path (L2)."""
        reclaimed = []
        pat = re.compile(r"^(task-[A-Za-z0-9._~-]+)\.assigned-(.+)\.txt$")
        try:
            entries = list(self.tasks_dir.iterdir())
        except OSError:
            return reclaimed
        for f in entries:
            m = pat.match(f.name)
            if not m or self.alive_fn(m.group(2)):
                continue
            try:
                os.rename(f, f.with_name(m.group(1) + ".txt"))
            except OSError:
                continue
            reclaimed.append(f.name)
        return reclaimed

    # ── liveness hygiene ────────────────────────────────────────────────────
    def _assign_ledger_path(self) -> Path:
        return self.state_dir / "pool" / "assignments.json"

    def _load_assign_ledger(self) -> dict:
        try:
            d = json.loads(self._assign_ledger_path().read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_assign_ledger(self, ledger: dict) -> None:
        p = self._assign_ledger_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp2")
            tmp.write_text(json.dumps(ledger))
            os.replace(tmp, p)
        except OSError:
            pass  # hygiene is best-effort; assignment correctness never depends on it

    # ── no-claim cooldown ───────────────────────────────────────────────────
    def _noclaim_path(self) -> Path:
        return self.state_dir / "pool" / "no-claim.json"

    def _load_noclaim(self) -> dict:
        try:
            d = json.loads(self._noclaim_path().read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _mark_noclaim(self, instance: str) -> None:
        """Record that this follower held an assignment past the stuck window.
        Survives the repool that clears the assignment ledger."""
        table = self._load_noclaim()
        table[instance] = self.now()
        cutoff = self.now() - NOCLAIM_COOLDOWN_S
        table = {k: v for k, v in table.items() if float(v) >= cutoff}
        p = self._noclaim_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp3")
            tmp.write_text(json.dumps(table))
            os.replace(tmp, p)
        except OSError:
            pass  # best-effort: a lost mark costs one retry, never correctness

    def _claiming(self, instance: str) -> bool:
        """False while a follower is inside the cooldown after failing to claim.
        Heartbeat cannot answer this: a session wedged at its input layer beats
        perfectly while never claiming, so `alive_fn` says yes throughout."""
        ts = self._load_noclaim().get(instance)
        return ts is None or (self.now() - float(ts)) >= NOCLAIM_COOLDOWN_S

    def reclaim_stuck_assignments(self, max_age_s: int = ASSIGN_STUCK_S) -> "list[str]":
        """Repool assignments a LIVE follower has not claimed in time. A hung
        session's wrapper keeps its heartbeat fresh, so reclaim_dead never
        fires — unclaimed age is the only signal. No claim = no work started,
        so repooling cannot double-fire a side effect."""
        pat = re.compile(r"^(task-[A-Za-z0-9._~-]+)\.assigned-(.+)\.txt$")
        ledger = self._load_assign_ledger()
        out = []
        live = set()
        try:
            entries = list(self.tasks_dir.iterdir())
        except OSError:
            return out
        for f in entries:
            m = pat.match(f.name)
            if not m:
                continue
            live.add(f.name)
            ts = ledger.get(f.name)
            if ts is None:
                ledger[f.name] = self.now()  # adopt pre-ledger assignments
                continue
            if self.now() - float(ts) < max_age_s or not self.alive_fn(m.group(2)):
                continue  # young, or dead (reclaim_dead owns that path)
            try:
                assigned_age = self.now() - f.stat().st_mtime
            except OSError:
                assigned_age = self.now() - float(ts)
            if (self._claimed_load(m.group(2)) > 0
                    and assigned_age < BUSY_DEFER_MAX_S):
                # busy pauses the clock and leaves a post-busy claim grace;
                # the cap anchors to file age, so a wedged core still yields
                ledger[f.name] = self.now() - max_age_s + BUSY_EXIT_GRACE_S
                continue
            try:
                os.rename(f, f.with_name(m.group(1) + ".txt"))
            except OSError:
                continue
            ledger.pop(f.name, None)
            live.discard(f.name)
            self._mark_noclaim(m.group(2))
            out.append(f.name)
            # An unresponsive home releases its rooms so the re-pick moves
            # them; a pinned row survives — pins move only by owner command.
            ch = _read_channel(f.with_name(m.group(1) + ".txt"))
            if ch:
                with self._affinity_lock():
                    aff = self._load_affinity()
                    row = aff.get(ch)
                    if (isinstance(row, dict)
                            and row.get("instance") == m.group(2)
                            and not row.get("pinned")):
                        del aff[ch]
                        self._save_affinity(aff)
        self._save_assign_ledger({k: v for k, v in ledger.items() if k in live})
        return out

    def prune_done_flags(self, retention_s: int = DONE_FLAG_RETENTION_S) -> int:
        """Drop done-flags past retention whose task no longer exists in
        tasks/ in any state — bounds the dirs and retires stale flags that
        could shadow a reused task id."""
        cores = self.state_dir / "cores"
        removed = 0
        try:
            dirs = [d / "done" for d in cores.iterdir() if (d / "done").is_dir()]
        except OSError:
            return 0
        for d in dirs:
            for flag in d.glob("task-*.flag"):
                try:
                    born = _flag_stamp_s(flag.name)
                    if born is None:
                        born = flag.stat().st_mtime
                    if self.now() - born < retention_s:
                        continue
                    stem = flag.name[:-len(".flag")]
                    if any(self.tasks_dir.glob(f"{stem}*.txt")):
                        continue
                    flag.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def _result_evidence(self, task_name: str) -> bool:
        """Delegates to the shared owner; see pool_follower.result_evidence."""
        from pool_follower import result_evidence
        return result_evidence(self.results_dir, task_name)

    def reclaim_claimed(self) -> "list[tuple[str, str]]":
        """Recover claimed files whose claimer died. Delivered means result
        evidence exists; then restore the canonical name so bridges can
        deliver (sweep skips it). The reachable crash residue is a result
        with no done-flag — finish_task writes the result first — and that
        work is COMPLETE, so it must not be repooled for re-execution."""
        out = []
        pat = re.compile(r"^(task-[A-Za-z0-9._~-]+)\.claimed-(.+)\.txt$")
        try:
            entries = list(self.tasks_dir.iterdir())
        except OSError:
            return out
        for f in entries:
            m = pat.match(f.name)
            if not m or self.alive_fn(m.group(2)):
                continue
            canonical = m.group(1) + ".txt"
            try:
                os.rename(f, f.with_name(canonical))
            except OSError:
                continue
            done = self._result_evidence(canonical)
            out.append((f.name, "delivered" if done else "repooled"))
        return out
