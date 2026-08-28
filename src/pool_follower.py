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

import os
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from task_priority import sort_tasks_by_priority  # noqa: E402
from task_archive import _move_without_clobbering  # noqa: E402

# Re-exported: the daemon and its liveness test import the constant here.
from heartbeat_freshness import (  # noqa: E402,F401
    HEARTBEAT_FUTURE_TOLERANCE_S, age_is_fresh)

LEAD_STALE_S = 90  # 3 missed 30s beats — same threshold every reader uses
# The lead writes cores/<LEAD_LABEL>.alive and lead_alive() reads it; one
# definition, imported by the daemon, so the two can never disagree.
LEAD_LABEL = "pool-lead"

_UNASSIGNED_RE = re.compile(
    r"^task-(?!.*\.(?:assigned|claimed)-)([A-Za-z0-9._~-]+)\.txt$")

_CLAIMED_RE = re.compile(
    r"^task-([A-Za-z0-9._~-]+)\.claimed-(.+)\.txt$")


def lead_alive(state_dir, lead_label: str, now_fn=time.time) -> bool:
    """False on missing, stale, or implausibly future-dated beats."""
    f = Path(state_dir) / "cores" / f"{lead_label}.alive"
    try:
        age = now_fn() - f.stat().st_mtime
    except OSError:
        return False
    return age_is_fresh(age, LEAD_STALE_S)


def _claim_assignment(tasks_dir: Path, f: Path, instance: str) -> "Path | None":
    suffix = f".assigned-{instance}.txt"
    target = f.with_name(
        f.name[:-len(suffix)] + f".claimed-{instance}.txt")
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
    suffix = f".assigned-{instance}.txt"
    try:
        assigned = [f for f in tasks.iterdir() if f.name.endswith(suffix)
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
    for f in sort_tasks_by_priority(pending):
        # assignment-suffix convention so lead-side load counting and
        # reclaim see fallback claims (legacy .claimed-core-N stays put)
        target = f.with_name(f.name[:-4] + f".claimed-{instance}.txt")
        try:
            os.rename(f, target)
            return target
        except OSError:
            continue
    return None


def finish_task(tasks_dir, results_dir, state_dir, instance: str,
                claimed_path, body: str) -> Path:
    """Complete a claimed task: result write, done-flag, archive — in that
    order. The body's first line MUST echo `task: <id>` (stripped before
    writing); a mismatch means the body was composed for another task, so
    refuse with ValueError and write nothing."""
    claimed = Path(claimed_path)
    m = _CLAIMED_RE.match(claimed.name)
    if m is None or m.group(2) != instance:
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

    archive = Path(tasks_dir) / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    # Canonical name: result consumers resolve destinations by task-<id>.txt,
    # so a claimed-suffix archive name dead-letters the reply as no-task.
    _move_without_clobbering(claimed, archive / f"task-{task_id}.txt")
    return result


def _finish_cli(argv: "list[str]") -> int:
    if len(argv) != 2:
        print("usage: pool_follower.py finish <claimed_path> <instance>",
              file=sys.stderr)
        return 2
    claimed = Path(argv[0]).resolve()
    workspace = claimed.parent.parent  # tasks/<claim> → workspace siblings
    try:
        result = finish_task(claimed.parent, workspace / "results",
                             workspace / "state", argv[1], claimed,
                             sys.stdin.read())
    except ValueError as e:
        print(f"finish refused: {e}", file=sys.stderr)
        return 2
    print(result)
    return 0


_USAGE = ("usage: pool_follower.py acquire <tasks_dir> <instance> "
          "[lead_label]\n"
          "       pool_follower.py finish <claimed_path> <instance>")


def _acquire_cli(argv: "list[str]") -> int:
    """0 = claimed (path on stdout), 1 = nothing to claim, 2 = usage.

    The exit codes ARE the follower contract in SKILL.md; 1 is an ordinary
    idle tick, not an error, so callers must not treat non-zero as failure."""
    if len(argv) not in (2, 3):
        print(_USAGE, file=sys.stderr)
        return 2
    tasks = Path(argv[0]).resolve()
    if not tasks.is_dir():
        print(f"acquire: not a directory: {tasks}", file=sys.stderr)
        return 2
    got = acquire_work(tasks, tasks.parent / "state", argv[1],
                       argv[2] if len(argv) == 3 else LEAD_LABEL)
    if got is None:
        return 1
    print(got)
    return 0


if __name__ == "__main__":
    _CMDS = {"acquire": _acquire_cli, "finish": _finish_cli}
    if len(sys.argv) >= 2 and sys.argv[1] in _CMDS:
        sys.exit(_CMDS[sys.argv[1]](sys.argv[2:]))
    print(_USAGE, file=sys.stderr)
    sys.exit(2)
