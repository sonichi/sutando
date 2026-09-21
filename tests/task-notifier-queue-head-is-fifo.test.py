#!/usr/bin/env python3
"""queue_head() must return the OLDEST marker (FIFO, announce order), not
the newest. `ls -1tr` lists oldest-first; `tail -1` of that list is the
NEWEST entry -- a LIFO bug (kewei-sutando-codex, PR #4561 review) that no
existing test caught, since every prior test only ever had one marker
queued at a time.

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


def test_queue_head_is_oldest_not_newest(runtime: str, script: Path) -> None:
    tmp = Path(tempfile.mkdtemp(prefix=f"queue-head-{runtime}-"))
    try:
        queue_dir = tmp / "queue"
        queue_dir.mkdir()
        for name in ("task-a.txt", "task-b.txt", "task-c.txt"):
            (queue_dir / name).touch()
            time.sleep(1.1)  # coarse mtime resolution on some filesystems
        fn = _function_text("queue_head", script.read_text())
        harness = f'queue_dir="{queue_dir}"\n' + fn + "\nqueue_head\n"
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=10)
        check(f"[{runtime}] queue_head returns the OLDEST marker (task-a.txt)",
              result.stdout.strip() == "task-a.txt",
              f"got {result.stdout.strip()!r}, stderr={result.stderr!r}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for runtime, script in SCRIPTS.items():
        test_queue_head_is_oldest_not_newest(runtime, script)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
