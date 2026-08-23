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

# Sticky-channel window (#884 semantics): tasks from a channel follow its
# handler until the channel has been idle this long, then rebalance.
AFFINITY_IDLE_S = 30 * 60

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
                 now_fn=time.time, metrics=None):
        """followers_fn() -> list of instance ids eligible for assignment.
        alive_fn(instance) -> bool (fresh heartbeat). Both injected — the
        production binder wires instance_registry + the .alive files."""
        self.tasks_dir = Path(tasks_dir)
        self.state_dir = Path(state_dir)
        self.followers_fn = followers_fn
        self.alive_fn = alive_fn
        self.now = now_fn
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
                    < AFFINITY_IDLE_S):
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
            if self._done_flag_exists(f.name):
                continue  # processed by a since-dead claimer; bridge owns it now
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

    def _done_flag_exists(self, task_name: str) -> bool:
        cores = self.state_dir / "cores"
        try:
            dirs = [d for d in cores.iterdir() if d.is_dir()]
        except OSError:
            return False
        stem = task_name[:-len(".txt")] if task_name.endswith(".txt") else task_name
        return any((d / "done" / f"{stem}.flag").exists() for d in dirs)

    def reclaim_claimed(self) -> "list[tuple[str, str]]":
        """Recover claimed files whose claimer died. The claimer's
        done-flag discriminates crash-before vs crash-after processing:
        flag present -> the result was written; restore the canonical name
        so bridges can deliver (sweep's done-flag skip prevents
        reassignment). Flag absent -> no side effects ran; restore to the
        pool for normal reassignment."""
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
            done = self._done_flag_exists(canonical)
            out.append((f.name, "delivered" if done else "repooled"))
        return out
