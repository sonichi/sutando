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

import json
import os
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
from task_priority import sort_tasks_by_priority  # noqa: E402
from pool_profiles import ProfileStoreCorrupt  # noqa: E402

# Sticky-channel window (#884 semantics): tasks from a channel follow its
# handler until the channel has been idle this long, then rebalance.
AFFINITY_IDLE_S = 30 * 60
# Outstanding assigned+claimed before affinity yields. Env-tunable: 1 = yield
# the moment the handler is busy (latency over continuity — owner preference).
AFFINITY_BUSY_MAX = max(1, int(os.environ.get("SUTANDO_AFFINITY_BUSY_MAX", "3")))
ASSIGN_STUCK_S = 300         # assigned but unclaimed this long → repool
# A repool pops the ledger entry, so "is it stuck right now" reads false on the
# very next sweep — the follower must stay marked or the task returns to it.
NOCLAIM_COOLDOWN_S = ASSIGN_STUCK_S
DONE_FLAG_RETENTION_S = 7 * 86400

# ids legitimately contain dots (task-<inst>~<id>), so exclude the
# assigned/claimed states explicitly rather than banning dots
_TASK_RE = re.compile(
    r"^task-(?!.*\.(?:assigned|claimed)-)[A-Za-z0-9._~-]+\.txt$")
_CHANNEL_RE = re.compile(r"^(?:channel_id|chat_id):\s*(\S+)", re.M)
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
                 profiles=None):
        """followers_fn() -> list of instance ids eligible for assignment.
        alive_fn(instance) -> bool (fresh heartbeat). Both injected — the
        production binder wires instance_registry + the .alive files."""
        self.tasks_dir = Path(tasks_dir)
        self.state_dir = Path(state_dir)
        self.results_dir = (Path(results_dir) if results_dir
                            else self.tasks_dir.parent / "results")
        self.followers_fn = followers_fn
        self.alive_fn = alive_fn
        self.now = now_fn
        self.metrics = metrics  # PoolMetrics or None; recording is optional
        self.profiles = profiles  # ProfileStore or None; seating is optional

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

    # ── pool state ──────────────────────────────────────────────────────────
    def _live_followers(self) -> "list[str]":
        return [f for f in self.followers_fn()
                if _INST_RE.match(str(f)) and self.alive_fn(f)]

    def _load(self, instance: str) -> int:
        pat = re.compile(
            rf"\.(?:assigned|claimed)-{re.escape(instance)}\.txt$")
        try:
            return sum(1 for f in self.tasks_dir.iterdir()
                       if pat.search(f.name))
        except OSError:
            return 0

    def _pick(self, channel: "str | None", followers: "list[str]",
              affinity: dict, lane: str = "owner") -> str:
        # Soft lanes (owner 2026-08-23): the highest core is the routine lane;
        # owner traffic stays off it except as saturated-pool overflow.
        lane_core = (max(followers, key=lambda f: (len(str(f)), str(f)))
                     if len(followers) > 1 else None)
        # Same escape the owner side already has: pinned unconditionally, one
        # wedged lane core absorbs every routine task and no other is offered.
        if (lane == "routine" and lane_core is not None
                and self._load(lane_core) < AFFINITY_BUSY_MAX
                and self._claiming(lane_core)):
            return lane_core
        primary = [f for f in followers if f != lane_core] or followers
        # A repool drops the follower's load, so least-loaded actively PREFERS
        # the core that just failed to claim. Never narrow to empty.
        primary = [f for f in primary if self._claiming(f)] or primary
        if channel:
            row = affinity.get(channel)
            if (isinstance(row, dict) and row.get("instance") in primary
                    and self.now() - float(row.get("ts") or 0)
                    < AFFINITY_IDLE_S
                    # a backlogged handler serializes the whole channel;
                    # parallelism outranks continuity past this depth
                    and self._load(row["instance"]) < AFFINITY_BUSY_MAX):
                return row["instance"]
        pick = min(primary, key=lambda f: (self._load(f), str(f)))
        if (lane_core is not None and self._load(pick) >= AFFINITY_BUSY_MAX
                and self._load(lane_core) == 0 and self._claiming(lane_core)):
            return lane_core  # overflow: whole owner lane saturated, lane idle
        return pick

    # ── profile seating (single-writer: the lead) ───────────────────────────
    def reconcile_seating(self) -> "list[tuple[str, str | None]]":
        """Keep every profile seated on a live core; returns what moved.

        No scheduling effect yet — assignment does not read seats, so this only
        maintains the table. A profile whose core is gone is re-seated rather
        than dropped, which is what makes a core's death a move and not a loss.
        """
        if self.profiles is None:
            return []
        try:
            store = self.profiles.load()
        except ProfileStoreCorrupt:
            return []  # never re-seat off a store we could not read
        writer = self.profiles.lead_label
        live = self._live_followers()
        moved: "list[tuple[str, str | None]]" = []
        for pid, prof in sorted(store["profiles"].items()):
            held = prof["seat"]["core_id"]
            if held is not None and held in live:
                continue
            if not live:
                # Unseat rather than leave a dead holder: a stale seat would
                # still pass the epoch check if that core ever came back.
                if held is not None:
                    self.profiles.unseat(pid, writer=writer)
                    moved.append((pid, None))
                continue
            pick = min(live, key=lambda f: (self._load(f), str(f)))
            self.profiles.seat(pid, pick, writer=writer)
            moved.append((pid, pick))
        return moved

    # ── the sweep ───────────────────────────────────────────────────────────
    def sweep(self) -> "list[tuple[str, str]]":
        """Assign every unassigned task; returns [(task_name, instance)].
        Priority order first (urgent > normal > low, then mtime), so a
        burst never starves an urgent task behind low-priority backlog."""
        followers = self._live_followers()
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
            inst = self._pick(channel, followers, affinity, _read_lane(f))
            target = f.with_name(
                f.name[:-len(".txt")] + f".assigned-{inst}.txt")
            try:
                arrived = f.stat().st_mtime  # before rename — f is gone after
            except OSError:
                arrived = self.now()
            try:
                os.rename(f, target)  # atomic; a racing writer keeps its file
            except OSError:
                continue
            if channel:
                affinity[channel] = {"instance": inst, "ts": self.now()}
            if self.metrics is not None:
                self.metrics.assigned(f.name, inst, channel,
                                      max(0.0, self.now() - arrived))
            out.append((f.name, inst))
        if out:
            self._save_affinity(affinity)
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
                os.rename(f, f.with_name(m.group(1) + ".txt"))
            except OSError:
                continue
            ledger.pop(f.name, None)
            live.discard(f.name)
            self._mark_noclaim(m.group(2))
            out.append(f.name)
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
                    if self.now() - flag.stat().st_mtime < retention_s:
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
        """A result was produced: live in results/, or already consumed by a
        bridge (archive/ and undelivered/ are the two consumer dispositions)."""
        stem = task_name[:-len(".txt")] if task_name.endswith(".txt") else task_name
        name = f"{stem}.txt"
        return any(p.exists() for p in (
            self.results_dir / name,
            self.results_dir / "archive" / name,
            self.results_dir / "undelivered" / name))

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
