#!/usr/bin/env python3
"""Every consumer of the pool task-filename policy delegates to its owner.

`src/task_archive.py` owns file -> canonical-id and id -> live-file. Three
consumers carried private copies that each handled `.claimed-` and missed
`.assigned-` (review-caught, keweichen on #3655). The worst was the scheduler:
an assigned job looked inactive, so `tick()` retried it and a scheduled action
with irreversible side effects could execute twice.

Behavioural rows come first — plain / claimed / assigned through each entry
point — then delegation pins, so a re-introduced private copy fails here even if
it happens to be correct today.

Run: python3 tests/task-filename-policy-delegation.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import ast
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from task_archive import find_task_file, task_id_from_filename  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


SUFFIXES = [("plain", "{i}.txt"),
            ("claimed", "{i}.claimed-core-3.txt"),
            ("assigned", "{i}.assigned-core-2.txt")]


def behaviour() -> None:
    print("file -> canonical id (tasks_view + workstreams entry points):")
    for label, pattern in SUFFIXES:
        name = pattern.format(i="task-B1")
        check(f"{label}: {name} -> task-B1", task_id_from_filename(name) == "task-B1",
              f"got {task_id_from_filename(name)!r}")

    print("id -> live file (codex-scheduler activity probe):")
    d = Path(tempfile.mkdtemp(prefix="filename-policy-"))
    for label, pattern in SUFFIXES:
        tid = f"task-{label}"
        (d / pattern.format(i=tid)).write_text("x")
        found = find_task_file(d, tid)
        check(f"{label}: an existing task is found", found is not None,
              "a live task read as absent — the scheduler would retry it")
    check("a missing id is still absent (probe can return None)",
          find_task_file(d, "task-nope") is None)


def delegation() -> None:
    print("delegation pins (no consumer re-implements the policy):")
    consumers = {
        "runtime-api/tasks_view": REPO / "src" / "runtime-api" / "tasks_view.py",
        # The workstream-grouping CLI is absent on purpose: its delegation
        # lands together with the live-file lookup it needs (#3658).
        "codex-scheduler": (REPO / "skills" / "schedule-crons" / "scripts"
                            / "codex-scheduler.py"),
    }
    for name, path in consumers.items():
        text = path.read_text()
        # Parse the AST, not the spelling: a parenthesized multi-line import
        # puts the symbol on another line and a one-line regex misses it.
        names = set()
        for node in ast.walk(ast.parse(text)):
            # Either path reaches the same owner: src/ directly, or the
            # vendored ag2_sparrow copy the drift gate keeps identical.
            if (isinstance(node, ast.ImportFrom)
                    and node.module in ("task_archive", "ag2_sparrow.task_archive")):
                names.update(a.name for a in node.names)
        check(f"{name}: imports the owner",
              bool(names & {"task_id_from_filename", "find_task_file"}),
              f"{path} imports nothing from task_archive (found {sorted(names)})")
        # The exact private forms that shipped, each blind to one state suffix.
        for bad, why in ((r'\.split\("\.claimed-"\)', "hand-rolled .claimed- split"),
                         (r'glob\(f?"\{task_id\}\.claimed-core-\*', "claimed-only glob")):
            check(f"{name}: no {why}", re.search(bad, text) is None,
                  f"{path} still carries a private copy")


def main() -> int:
    behaviour()
    delegation()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
