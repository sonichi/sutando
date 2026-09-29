#!/usr/bin/env python3
"""Discord routes malformed holder ids through the shared recovery gate.

Drive its real wrapper with a lookup that raises if reached, plus a valid-id
control. Removing the old adapter-specific lookup must retain traversal safety.
"""
from __future__ import annotations

import pathlib
import os
import json
import importlib.util
from unittest.mock import patch
import sys
import tempfile
import atexit
import shutil
from pathlib import Path

_CFG = tempfile.mkdtemp(prefix="ccd-dedup-traversal-")
atexit.register(lambda: shutil.rmtree(_CFG, ignore_errors=True))
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
_cfg = Path(_CFG) / "channels" / "discord"
_cfg.mkdir(parents=True)
(_cfg / "access.json").write_text(json.dumps({"allowFrom": []}))
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")

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

    # The wrapper harness also stubs provider clients before importing Discord.
    spec = importlib.util.spec_from_file_location(
        "_traversal_harness", REPO / "tests" / "bridge-dedup-wrappers.test.py")
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    bridge = harness._load("traversal", "discord-bridge.py")
    bridge.TASKS_DIR = root / "tasks"
    bridge.RESULTS_DIR = root / "results"
    bridge.RESULTS_DIR.mkdir()
    from dedup_recovery import MALFORMED_TEMPLATE
    for bad in ("../secret", "../../../etc/passwd", "..", ".", "a/b"):
        with patch("dedup_recovery.find_task_file", side_effect=AssertionError("unsafe lookup")) as lookup:
            action, body = bridge._dedup_recover("task-ask", bad, 4242)
            check((action, body) == ("report", MALFORMED_TEMPLATE),
                  f"3) Discord reports malformed holder {bad!r}")
            check(lookup.call_count == 0, "3) malformed holder never reaches a task lookup")
    (bridge.RESULTS_DIR / "task-holder.txt").write_text("[REPLIED]")
    (bridge.TASKS_DIR / "task-holder.txt").write_text("channel_id: 4242\n")
    check(bridge._dedup_recover("task-ask", "task-holder", 4242) == ("honour", None),
          "3) valid same-room holder still reaches normal recovery")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
        return 1
    print("PASS — a sender-influenced holder id cannot address a path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
