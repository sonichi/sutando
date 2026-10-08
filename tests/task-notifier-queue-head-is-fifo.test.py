#!/usr/bin/env python3
"""queue_head() must return the highest-tier marker, oldest first within a
tier. Two regressions this pins:

1. FIFO within a tier: `ls -1tr` lists oldest-first; `tail -1` of that list
   is the NEWEST entry -- a LIFO bug (kewei-sutando-codex, PR #4561 review)
   that no existing test caught, since every prior test only ever had one
   marker queued at a time.
2. Cross-tier priority: a marker's tier is stored as its own content at
   enqueue time (task_dispatch.py priority-tier, the shared policy). A
   lower-tier marker that was announced (and so queued) first must not beat
   a higher-tier marker announced later -- the original gap kewei's review
   found: the priority fix only covered the startup/directory sweeps, never
   the live single-file arrival path, so a low task queued while the
   notifier was busy still went out ahead of an urgent task that arrived
   moments later.

Extracts queue_head() from each shipped script and runs it directly, so the
assertion is on the shipped function, not a copy.

Run: python3 tests/task-notifier-queue-head-is-fifo.test.py
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = {
    "claude": REPO / "src" / "agent" / "claude" / "cli" / "task-notifier.sh",
    "codex": REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh",
}
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _function_text(name: str, text: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
    assert m, f"{name} not found"
    return m.group(0)


def _run_queue_head(queue_dir: Path, fn: str) -> str:
    harness = f'queue_dir="{queue_dir}"\n' + fn + "\nqueue_head\n"
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=10)
    return result.stdout.strip(), result.stderr


def test_queue_head_is_oldest_not_newest_within_a_tier(runtime: str, script: Path) -> None:
    tmp = Path(tempfile.mkdtemp(prefix=f"queue-head-{runtime}-"))
    try:
        queue_dir = tmp / "queue"
        queue_dir.mkdir()
        for name in ("task-a.txt", "task-b.txt", "task-c.txt"):
            (queue_dir / name).write_text("normal")
            time.sleep(1.1)  # coarse mtime resolution on some filesystems
        fn = _function_text("queue_head", script.read_text())
        out, err = _run_queue_head(queue_dir, fn)
        check(f"[{runtime}] queue_head returns the OLDEST same-tier marker (task-a.txt)",
              out == "task-a.txt", f"got {out!r}, stderr={err!r}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_queue_head_prefers_urgent_announced_later_over_low_announced_first(runtime: str, script: Path) -> None:
    """The exact live-arrival shape kewei's review reproduced: task-low
    queued first (e.g. while the pane was busy with something else),
    task-urgent queued moments later -- urgent must still win."""
    tmp = Path(tempfile.mkdtemp(prefix=f"queue-head-priority-{runtime}-"))
    try:
        queue_dir = tmp / "queue"
        queue_dir.mkdir()
        (queue_dir / "task-low.txt").write_text("low")
        time.sleep(1.1)
        (queue_dir / "task-urgent.txt").write_text("urgent")
        fn = _function_text("queue_head", script.read_text())
        out, err = _run_queue_head(queue_dir, fn)
        check(f"[{runtime}] queue_head returns the urgent marker despite arriving second",
              out == "task-urgent.txt", f"got {out!r}, stderr={err!r}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for runtime, script in SCRIPTS.items():
        test_queue_head_is_oldest_not_newest_within_a_tier(runtime, script)
        test_queue_head_prefers_urgent_announced_later_over_low_announced_first(runtime, script)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
