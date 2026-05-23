#!/usr/bin/env python3
"""Regression guard for restart-safety #4: task-file `attempts:` counter.

## The bug

If the agent crashes mid-task with non-idempotent side effects already
executed but the archive of result + task files never ran, on restart
the task file is still in `tasks/`. The watcher re-emits it and the
agent re-processes — potentially re-executing the side effect.

## The fix

`src/task_bump_attempts.py` increments an `attempts:` counter on every
emission. Wired into `src/watch-tasks-stream.sh` (initial sweep + new-
file event). Insertion point is BEFORE the `task:` delimiter line
(parsers stop at `task:`; fields after are invisible).

## What this test covers

The bumper script's pure logic. Watcher integration is asserted via
source-grep so a refactor that drops the bumper call from either of
the two emit paths fails loudly.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "task_bump_attempts", REPO / "src" / "task_bump_attempts.py"
)
bumper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bumper)

TMP = Path(tempfile.mkdtemp(prefix="sutando-bump-attempts-test-"))


def _make(name: str, content: str) -> Path:
    p = TMP / name
    p.write_text(content)
    return p


def test_fresh_task_gets_attempts_1():
    """No `attempts:` field present → `attempts: 1` inserted, position
    must be BEFORE the `task:` line."""
    p = _make(
        "fresh.txt",
        "id: task-1\ntimestamp: 2026-05-22\ntask: do the thing\n",
    )
    n = bumper.bump_attempts(p)
    assert n == 1, f"expected new count 1, got {n}"
    content = p.read_text()
    assert "attempts: 1" in content
    lines = content.split("\n")
    a_idx = next(i for i, ln in enumerate(lines) if ln.startswith("attempts:"))
    t_idx = next(i for i, ln in enumerate(lines) if ln.startswith("task:"))
    assert a_idx < t_idx, (
        f"attempts: at line {a_idx} but task: at line {t_idx} — "
        f"parsers stop at task: so attempts must come first"
    )


def test_existing_attempts_increment():
    p = _make(
        "retry.txt",
        "id: task-2\nattempts: 1\ntimestamp: 2026-05-22\ntask: retry me\n",
    )
    n = bumper.bump_attempts(p)
    assert n == 2
    content = p.read_text()
    # Exactly one attempts: line
    attempts_lines = [l for l in content.split("\n") if l.startswith("attempts:")]
    assert len(attempts_lines) == 1
    assert attempts_lines[0] == "attempts: 2"


def test_high_count_increment():
    p = _make("high.txt", "id: task-h\nattempts: 47\ntask: many retries\n")
    n = bumper.bump_attempts(p)
    assert n == 48


def test_missing_task_line_untouched():
    """Defensive: malformed file (no task: line) is left alone. The
    bumper should not mangle non-task files that somehow ended up in
    tasks/."""
    original = "this is not a task file\nno task line here\n"
    p = _make("malformed.txt", original)
    n = bumper.bump_attempts(p)
    assert n == 0
    assert p.read_text() == original


def test_no_tmp_file_left():
    """Post-#1049 follow-up: the bumper writes in-place (no tmp+rename)
    to avoid self-triggering fswatch's `Renamed` event. Pin that no
    `.tmp` file is ever created — if a future refactor reverts to
    tmp+rename, the self-trigger loop returns."""
    p = _make("tmp.txt", "id: task-tmp\ntask: x\n")
    bumper.bump_attempts(p)
    tmp = p.with_suffix(p.suffix + ".tmp")
    assert not tmp.exists(), (
        f".tmp file left on disk: {tmp} — bumper must write in-place "
        f"to avoid fswatch self-trigger loop (PR #1049 follow-up)."
    )


def test_bumper_uses_in_place_write_not_rename():
    """Architectural pin: source must NOT call `.replace(file_path)`
    or `os.rename(...file_path...)` — those trigger fswatch's
    `Renamed` event, and the upstream `.sh` watcher (no dedup set)
    enters an infinite self-trigger loop. In-place open+write only.

    The fork's `.mjs` watcher has a `seen` set that bounds the loop
    to one extra bump, but the upstream `.sh` has no such guard,
    making this the load-bearing fix for that variant."""
    src = (REPO / "src" / "task_bump_attempts.py").read_text()
    assert ".replace(file_path)" not in src, (
        "task_bump_attempts.py uses .replace(file_path) — this triggers "
        "fswatch Renamed event in the upstream watcher, causing an "
        "infinite self-trigger loop. Use in-place open+write instead."
    )
    assert "open(file_path" in src, (
        "task_bump_attempts.py must use in-place open(file_path, 'w') — "
        "rename-style writes self-trigger the watcher."
    )


def test_three_sequential_bumps():
    """Watcher-restart pattern: bump-emit, bump-emit, bump-emit.
    Counter advances by 1 each time."""
    p = _make("seq.txt", "id: task-seq\ntask: many bumps\n")
    assert bumper.bump_attempts(p) == 1
    assert bumper.bump_attempts(p) == 2
    assert bumper.bump_attempts(p) == 3
    content = p.read_text()
    attempts_lines = [l for l in content.split("\n") if l.startswith("attempts:")]
    assert len(attempts_lines) == 1
    assert attempts_lines[0] == "attempts: 3"


def test_missing_file_no_error():
    """Bumper on non-existent file returns 0, doesn't raise."""
    n = bumper.bump_attempts(TMP / "nonexistent.txt")
    assert n == 0


def test_cli_invocation():
    """The CLI entry point `python3 src/task_bump_attempts.py <file>`
    must work — that's what the .sh watcher calls."""
    p = _make("cli.txt", "id: task-cli\ntask: cli call\n")
    res = subprocess.run(
        [sys.executable, str(REPO / "src" / "task_bump_attempts.py"), str(p)],
        capture_output=True, text=True, timeout=5,
    )
    assert res.returncode == 0, f"CLI exited {res.returncode}, stderr={res.stderr}"
    assert "attempts: 1" in p.read_text()


def test_watcher_wires_bumper_in_initial_sweep_and_stream():
    """Architectural source-grep: the watcher must invoke the bumper
    BOTH from the initial-sweep loop AND from the fswatch stream
    callback. Without both, one of the two emit paths breaks the
    contract (fresh-emit vs restart-emit)."""
    src = (REPO / "src" / "watch-tasks-stream.sh").read_text()
    bumper_calls = src.count("task_bump_attempts.py")
    assert bumper_calls >= 2, (
        f"Expected >=2 calls to task_bump_attempts.py in the watcher "
        f"(initial sweep + stream branch); found {bumper_calls}."
    )
    # Position check: split on the actual fswatch command (start-of-line
    # `fswatch \`), not the word in comments. One call must precede
    # (initial sweep loop), the other must follow (stream callback).
    import re
    split = re.search(r"^fswatch\s+\\$", src, re.MULTILINE)
    assert split, "could not locate `fswatch \\` command line"
    sweep_section = src[:split.start()]
    stream_section = src[split.start():]
    assert "task_bump_attempts.py" in sweep_section, (
        "initial sweep loop does NOT call task_bump_attempts.py"
    )
    assert "task_bump_attempts.py" in stream_section, (
        "fswatch stream branch does NOT call task_bump_attempts.py"
    )


def main():
    failures = []
    for fn in (
        test_fresh_task_gets_attempts_1,
        test_existing_attempts_increment,
        test_high_count_increment,
        test_missing_task_line_untouched,
        test_no_tmp_file_left,
        test_bumper_uses_in_place_write_not_rename,
        test_three_sequential_bumps,
        test_missing_file_no_error,
        test_cli_invocation,
        test_watcher_wires_bumper_in_initial_sweep_and_stream,
    ):
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print(f"All {10} attempts-counter tests passed.")


if __name__ == "__main__":
    main()
