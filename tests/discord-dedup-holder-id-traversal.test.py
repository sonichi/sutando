#!/usr/bin/env python3
"""A dedup holder id is a lookup key, not a path — the bridge must gate it.

The guard used to refuse `[deduped: <anything>]` on a guarded tier, so a
sender-influenced holder id could never reach a filesystem lookup. Honouring
suppression on every tier removes that accident, and the two consumers were
NOT symmetric:

    dedup_recovery -> find_result      -> valid_archive_lookup_id  GATED
    discord-bridge -> find_task_file   -> tasks_dir / f"{id}.txt"  UNGATED

so `[deduped: ../../secret]` resolved outside the tasks dir and the bridge read
whatever it found. The gate is applied at the call site, and an id that fails it
resolves to "holder not found" — which keeps the existing recovery path rather
than silently archiving the asker's question.

Run: python3 tests/discord-dedup-holder-id-traversal.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from local_task_protocol import valid_archive_lookup_id  # noqa: E402
from task_archive import find_task_file  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    # 1. The primitive is real: find_task_file alone escapes its directory.
    root = pathlib.Path(tempfile.mkdtemp(prefix="dedup-traversal-"))
    (root / "tasks").mkdir()
    (root / "secret.txt").write_text("OWNER ONLY", encoding="utf-8")
    escaped = find_task_file(root / "tasks", "../secret")
    check(escaped is not None and escaped.resolve() == (root / "secret.txt").resolve(),
          "1) find_task_file resolves ../secret OUTSIDE tasks_dir (the primitive)")
    check(find_task_file(root / "tasks", "task-nope") is None,
          "1) and returns None for an ordinary miss, so the two are distinguishable")

    # 2. The gate rejects traversal and accepts every id shape in production use
    #    (task-*, the gateway's named-instance `task-<inst>~<broker-id>`).
    for bad in ("../secret", "../../../etc/passwd", "..", ".", "a/b", "", "   "):
        check(not valid_archive_lookup_id(bad), f"2) gate rejects {bad!r}")
    for good in ("task-1787190753943", "task-chat-1787190753", "task-inst~abc123",
                 "ask-42", "sc-ask-7", "reco-skill-9"):
        check(valid_archive_lookup_id(good), f"2) gate accepts {good!r}")

    # 3. The bridge applies it at the call site, and only around the lookup —
    #    the recovery branch itself must still run for an unresolvable holder.
    src = (REPO / "src" / "discord-bridge.py").read_text(encoding="utf-8")
    # The branch condition deliberately does NOT test _skip.extra: an empty
    # holder is a dedup marker too, and the shared plan answers it.
    call = re.search(
        r"if _skip\.value == \"deduped\":(.{0,700}?)_holder_text",
        src, re.S)
    check(call is not None, "3) the dedup branch is where it was")
    if call:
        block = call.group(1)
        check("valid_archive_lookup_id(_skip.extra)" in block,
              "3) the holder id is gated before find_task_file")
        check("find_task_file(TASKS_DIR, _skip.extra)" in block,
              "3) and the lookup still happens for a well-formed id")
        check("else None" in block,
              "3) a rejected id becomes 'holder not found', not a skipped branch")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
        return 1
    print("PASS — a sender-influenced holder id cannot address a path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
