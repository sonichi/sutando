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
from delivery.readiness import read_ready_result
from local_task_protocol import find_result
from pool_follower import LEAD_STALE_S  # noqa: E402
from task_priority import sort_tasks_by_priority  # noqa: E402

SLEEP_SKEW_S = 5.0

# Sticky-channel window (#884 semantics): tasks from a channel follow its
# handler until the channel has been idle this long, then rebalance.
AFFINITY_IDLE_S = 30 * 60
AFFINITY_BUSY_MAX = 3  # outstanding assigned+claimed before affinity yields

# ids legitimately contain dots (task-<inst>~<id>), so exclude the
# assigned/claimed states explicitly rather than banning dots
_TASK_RE = re.compile(
    r"^task-(?!.*\.(?:assigned|claimed)-)[A-Za-z0-9._~-]+\.txt$")
_CHANNEL_RE = re.compile(r"^(?:channel_id|chat_id):\s*(\S+)", re.M)
_INST_RE = re.compile(r"^[A-Za-z0-9@:._-]{1,128}$")


def _read_channel(path: Path) -> "str | None":
    try:
        head = path.read_text(errors="replace")[:2048]
    except OSError:
        return None
    m = _CHANNEL_RE.search(head)
    return m.group(1) if m else None


class PoolLead:
    def __init__(self, tasks_dir, state_dir, followers_fn, alive_fn,
                 now_fn=time.time, metrics=None, results_dir=None,
                 mono_fn=time.monotonic):
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
        self.mono = mono_fn
        self.metrics = metrics  # PoolMetrics or None; recording is optional

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
              affinity: dict) -> str:
        if channel:
            row = affinity.get(channel)
            if (isinstance(row, dict) and row.get("instance") in followers
                    and self.now() - float(row.get("ts") or 0)
                    < AFFINITY_IDLE_S
                    # a backlogged handler serializes the whole channel;
                    # parallelism outranks continuity past this depth
                    and self._load(row["instance"]) < AFFINITY_BUSY_MAX):
                return row["instance"]
        return min(followers, key=lambda f: (self._load(f), str(f)))

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
            inst = self._pick(channel, followers, affinity)
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
    def _advance_reclaim_guard(self) -> bool:
        """Use wall-vs-monotonic skew to distinguish host sleep from a slow
        daemon. Once opened, the grace expires by time rather than a sibling's
        heartbeat so staggered beats cannot expose a live claimant.

        A cold lead has no skew to read, and a stale beat there is equally
        consistent with death and with a follower that has not re-beaten yet,
        so ANY unproven follower opens the window."""
        now = self.now()
        mono = self.mono()
        last = getattr(self, "_last_reclaim_tick", None)
        last_mono = getattr(self, "_last_reclaim_mono", None)
        self._last_reclaim_tick = now
        self._last_reclaim_mono = mono
        if last is not None and last_mono is not None:
            if (now - last) - (mono - last_mono) > SLEEP_SKEW_S:
                self._reclaim_defer_until = now + LEAD_STALE_S
        elif last is None:
            try:
                followers = list(self.followers_fn())
            except Exception:  # noqa: BLE001 — resolver failure must not defer
                return False
            if followers and not all(self.alive_fn(f) for f in followers):
                self._reclaim_defer_until = now + LEAD_STALE_S
        until = getattr(self, "_reclaim_defer_until", None)
        return until is not None and now < until

    def reclaim_dead(self) -> "list[str]":
        """Return dead followers' assignments to the unassigned pool.
        Claimed files are NOT touched here — a claim means work may have
        side-effected; that recovery keeps the done-flag path (L2)."""
        if self._advance_reclaim_guard():
            return []
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
            if self.metrics is not None:
                self.metrics.reclaimed(f.name, m.group(2), "assigned")
            reclaimed.append(f.name)
        return reclaimed

    def _result_evidence(self, task_name: str) -> "str | None":
        """How an existing result was disposed of, or None when none is ready.

        Locating and readiness are not this module's policy: `find_result`
        already knows the live dir, `archive/YYYY-MM/` and the flat gateway
        archive, and `read_ready_result` already knows that an empty or
        half-written file is not an answer. Quarantine stays a distinct
        disposition — it is evidence the work ran, not that it reached anyone.
        """
        stem = task_name[:-len(".txt")] if task_name.endswith(".txt") else task_name
        found = find_result(self.results_dir, stem)
        if found is not None and read_ready_result(found) is not None:
            return "delivered"
        quarantined = self.results_dir / "undelivered" / f"{stem}.txt"
        if read_ready_result(quarantined) is not None:
            return "undelivered"
        return None

    def reclaim_claimed(self) -> "list[tuple[str, str]]":
        """Recover claimed files whose claimer died. Delivered means result
        evidence exists; then restore the canonical name so bridges can
        deliver (sweep skips it). The reachable crash residue is a result
        with no done-flag — finish_task writes the result first — and that
        work is COMPLETE, so it must not be repooled for re-execution."""
        if self._advance_reclaim_guard():
            return []
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
            disposition = self._result_evidence(canonical) or "repooled"
            if self.metrics is not None:
                self.metrics.reclaimed(f.name, m.group(2), disposition)
            out.append((f.name, disposition))
        return out
