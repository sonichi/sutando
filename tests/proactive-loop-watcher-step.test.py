#!/usr/bin/env python3
"""The proactive loop's watcher step asks the per-inbox question, not the host-wide one.

On a pool host a worker's watcher satisfies any "is a watcher running" probe while the
core's own inbox has none, so a step that acts only on that probe never re-arms the core's
watcher. The step must ask `watcher_identity role-present` for the core's inbox and act on
its three answers; the verdict vocabulary is pinned against the real script.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills/proactive-loop/SKILL.md"
IDENTITY = REPO / "src/watcher_identity.py"

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


text = SKILL.read_text(encoding="utf-8")
m = re.search(r"^9\. \*\*Watcher\.\*\*(.*?)^9\.5\. ", text, re.S | re.M)
check("step 9 exists and is followed by 9.5", m is not None)
# The skill wraps at ~110 columns; assertions are about words, not line breaks.
step = re.sub(r"\s+", " ", m.group(1)) if m else ""

check("step 9 asks role-present for the session role",
      "watcher_identity.py role-present session" in step)
check("step 9 scopes the question to this core's inbox",
      '--inbox "$WORKSPACE/tasks"' in step)
check("step 9 asks the sentinel-gated (ready) form",
      '--ready "$WORKSPACE/state"' in step)
check("`no` starts the tagged session watcher",
      re.search(r"`no` → start it: `Monitor` `bash src/watch-tasks-stream\.sh --role session --inbox", step) is not None)
check("`yes` changes nothing", "`yes` → nothing" in step)
check("`unknown` changes nothing and is reported", "`unknown` → change nothing and say so" in step)
check("the start decision no longer hangs on the host-wide task-watcher probe",
      "Act only on the `task-watcher` probe" not in step)
check("the stop rule still needs the probe's owned/ownerless split",
      "owned and ownerless as two separately labelled groups" in step)
check("the instance substitution for a non-default inbox is kept",
      "$SUTANDO_TASKS_DIR" in step)

# The vocabulary the step acts on is the script's, not the prose's: a fresh inbox with no
# watcher must answer exactly `no`, and the answer set is exactly {yes, no, unknown}.
with tempfile.TemporaryDirectory() as tmp:
    ws = Path(tmp)
    (ws / "tasks").mkdir()
    (ws / "state").mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SUTANDO_") and k not in ("AGENT_ID", "AG2_AGENT_NAME", "TMUX", "TMUX_PANE")}
    out = subprocess.run(
        [sys.executable, str(IDENTITY), "role-present", "session",
         "--inbox", str(ws / "tasks"), "--ready", str(ws / "state")],
        capture_output=True, text=True, env=env, cwd=str(REPO))
    check("role-present answers `no` for an inbox nobody watches",
          out.stdout.strip() == "no" and out.returncode == 0)

src = IDENTITY.read_text(encoding="utf-8")
printed = set(re.findall(r'print\("(yes|no|unknown)"\)', src)) | (
    {"yes", "no"} if 'print("yes" if verdict else "no")' in src else set())
check("the script's answer set is exactly yes / no / unknown", printed == {"yes", "no", "unknown"})

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall passed")
