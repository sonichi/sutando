#!/usr/bin/env python3
"""Follower-side work acquisition (lead-follower pool, slice L2).

Under a live lead a follower executes ONLY its own assignments — taking
unassigned work would bypass every lead-side policy (priority, affinity,
consolidation). When the lead's beat goes stale the follower degrades to
the leaderless #884 claim so the queue keeps draining with no election;
assignment-only resumes the moment the beat is fresh again.

Injected paths/clock; stdlib only. See docs/lead-follower-pool.md.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from task_priority import sort_tasks_by_priority  # noqa: E402
from task_archive import _move_without_clobbering  # noqa: E402

import pool_names as pn  # noqa: E402

LEAD_STALE_S = 90  # 3 missed 30s beats — same threshold every reader uses

_UNASSIGNED_RE = re.compile(
    r"^task-(?!.*\.(?:assigned|claimed)-)([A-Za-z0-9._~-]+)\.txt$")

_CLAIMED_RE = re.compile(
    r"^task-([A-Za-z0-9._~-]+)\.claimed-(.+)\.txt$")


def result_evidence(results_dir: Path, task_name: str) -> bool:
    """A result was produced: live in results/, or already consumed by a
    bridge (archive/ and undelivered/ are the two consumer dispositions)."""
    stem = task_name[:-len(".txt")] if task_name.endswith(".txt") else task_name
    name = f"{stem}.txt"
    results_dir = Path(results_dir)
    return any(p.exists() for p in (
        results_dir / name,
        results_dir / "archive" / name,
        results_dir / "undelivered" / name))


def lead_alive(state_dir, lead_label: str, now_fn=time.time) -> bool:
    """False on a missing OR future-dated beat — clock skew must degrade,
    never keep followers deferring to a lead that is not really there."""
    f = Path(state_dir) / "cores" / f"{lead_label}.alive"
    try:
        age = now_fn() - f.stat().st_mtime
    except OSError:
        return False
    return 0 <= age < LEAD_STALE_S


def _claim_assignment(tasks_dir: Path, f: Path, instance: str) -> "Path | None":
    # Either spelling of the assigned suffix is honoured; the claim is canonical.
    suffix = next(s for s in pn.assigned_suffixes(instance) if f.name.endswith(s))
    target = f.with_name(
        f.name[:-len(suffix)] + pn.claimed_suffix(instance))
    try:
        os.rename(f, target)
        return target
    except OSError:
        return None  # lead reclaimed or a restart raced us — not an error


def acquire_work(tasks_dir, state_dir, instance: str,
                 lead_label: str, now_fn=time.time) -> "Path | None":
    """Claim the next unit of work for `instance`, or None when idle.
    Own assignments are honored in priority order in BOTH modes — the
    fallback additionally opens the unassigned pool."""
    tasks = Path(tasks_dir)
    suffixes = pn.assigned_suffixes(instance)
    try:
        assigned = [f for f in tasks.iterdir() if f.name.endswith(suffixes)
                    and f.name.startswith("task-")]
    except OSError:
        assigned = []
    for f in sort_tasks_by_priority(assigned):
        got = _claim_assignment(tasks, f, instance)
        if got is not None:
            return got
    if lead_alive(state_dir, lead_label, now_fn):
        return None  # a live lead owns the unassigned pool — never steal
    try:
        pending = [f for f in tasks.iterdir() if _UNASSIGNED_RE.match(f.name)]
    except OSError:
        return None
    # results/ is the tasks dir's workspace sibling — derived, not passed,
    # so no caller can omit the guard on the path that exists to survive crashes.
    results_dir = tasks.parent / "results"
    for f in sort_tasks_by_priority(pending):
        # A reclaimed task whose work already produced a result must not be
        # re-executed (same guard as the lead's pooling scan).
        if result_evidence(results_dir, f.name):
            continue
        # assignment-suffix convention so lead-side load counting and
        # reclaim see fallback claims (a pre-rename claim stays put)
        target = f.with_name(f.name[:-4] + pn.claimed_suffix(instance))
        try:
            os.rename(f, target)
            return target
        except OSError:
            continue
    return None


def _source_of(claimed: Path) -> str:
    try:
        for line in claimed.read_text(errors="replace").splitlines():
            if line.startswith("source:"):
                return line.split(":", 1)[1].strip()[:40]
            if not line.strip():
                break
    except OSError:
        pass
    return ""


def _append_metric(metrics_path, record: dict) -> None:
    """Bookkeeping only — a metrics failure must never fail a completed task."""
    try:
        p = Path(metrics_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def finish_task(tasks_dir, results_dir, state_dir, instance: str,
                claimed_path, body: str, metrics_path=None) -> Path:
    """Complete a claimed task: result write, done-flag, archive — in that
    order. The body's first line MUST echo `task: <id>` (stripped before
    writing); a mismatch means the body was composed for another task, so
    refuse with ValueError and write nothing."""
    claimed = Path(claimed_path)
    instance = pn.canonical(instance)
    m = _CLAIMED_RE.match(claimed.name)
    if m is None or pn.canonical(m.group(2)) != instance:
        raise ValueError(
            f"not a claim held by {instance!r}: {claimed.name}")
    if not claimed.is_file():
        raise ValueError(f"claimed file missing: {claimed}")
    task_id = m.group(1)
    if not body or not body.strip():
        raise ValueError("empty result body")
    first, _, rest = body.partition("\n")
    # Built by concatenation, not an f-string: the injection sweep keys on an
    # interpolated `task:` field, and this compares one — it never writes one.
    expected_echo = "task: " + task_id
    if first.rstrip("\r") != expected_echo:
        raise ValueError(
            f"pairing echo mismatch: need {expected_echo!r} "
            f"as first line, got {first!r}")
    if not rest.strip():
        raise ValueError("result body is only the pairing echo line")

    results = Path(results_dir)
    result = results / f"task-{task_id}.txt"
    tmp = results / f".task-{task_id}.txt.tmp-{instance}"
    results.mkdir(parents=True, exist_ok=True)
    tmp.write_text(rest)
    os.replace(tmp, result)

    done = Path(state_dir) / "cores" / instance / "done"
    done.mkdir(parents=True, exist_ok=True)
    (done / f"task-{task_id}.flag").write_text("")

    # Renames preserve mtime, so the claim still carries the task's arrival
    # time — read both before the archive move takes the file away.
    source = _source_of(claimed)
    try:
        arrived_at = claimed.stat().st_mtime
    except OSError:
        arrived_at = None

    archive = Path(tasks_dir) / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    # Canonical name: result consumers resolve destinations by task-<id>.txt,
    # so a claimed-suffix archive name dead-letters the reply as no-task.
    _move_without_clobbering(claimed, archive / f"task-{task_id}.txt")

    if metrics_path is not None:
        finished_at = time.time()
        _append_metric(metrics_path, {
            "task_id": task_id,
            "worker": instance,
            "source": source,
            "arrived_at": arrived_at,
            "finished_at": finished_at,
            "duration_s": (None if arrived_at is None
                           else round(finished_at - arrived_at, 3)),
        })
    return result


def _finish_cli(argv: "list[str]") -> int:
    if len(argv) != 2:
        print("usage: pool_follower.py finish <claimed_path> <worker>",
              file=sys.stderr)
        return 2
    claimed = Path(argv[0]).resolve()
    workspace = claimed.parent.parent  # tasks/<claim> → workspace siblings
    try:
        result = finish_task(claimed.parent, workspace / "results",
                             workspace / "state", argv[1], claimed,
                             sys.stdin.read(),
                             workspace / "data" / "pool-metrics.jsonl")
    except ValueError as e:
        print(f"finish refused: {e}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "finish":
        sys.exit(_finish_cli(sys.argv[2:]))
    print("usage: pool_follower.py finish <claimed_path> <instance>",
          file=sys.stderr)
    sys.exit(2)
