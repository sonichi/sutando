#!/usr/bin/env python3
"""The one owner of the core-monitor log policy, and both runtime launchers delegating to it.

`src/agent/core-workspace-log.sh` decides where a launcher's monitor and relay write: the
workspace's own logs/, never a shared /tmp file, and /dev/null when logs/ cannot be made,
so a log never stops a start. Both launchers source it and keep only their launch mechanics
(docs/architecture-boundaries.md, "Shared adapter policy").
  a) it prints <workspace>/logs/<name> and makes logs/
  b) with logs/ uncreatable it prints /dev/null and still succeeds
  c) both launchers source it and spell no log path or fallback of their own
     (structural; the behaviour is driven in start-cli-claude-monitor-logs and codex-core-launcher)

Run: python3 tests/core-workspace-log.test.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "src" / "agent" / "core-workspace-log.sh"
LAUNCHERS = ("src/agent/claude/cli/start-cli.sh", "src/agent/codex/cli/start-cli.sh")
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" -- {detail}"))
    if not cond:
        failures.append(name)


def log_path(ws: Path, name: str) -> "subprocess.CompletedProcess":
    return subprocess.run(["bash", "-c", f'. "{HELPER}"; sutando_core_workspace_log "$1" "$2"', "_",
                           str(ws), name], capture_output=True, text=True, timeout=30)


with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    ws.mkdir()
    out = log_path(ws, "core-input-watch.log")
    check("a) the log is the workspace's own logs/<name>",
          out.returncode == 0 and out.stdout == f"{ws}/logs/core-input-watch.log", repr(out.stdout))
    check("a) and logs/ is made", (ws / "logs").is_dir())

    blocked = Path(td) / "blocked"
    blocked.mkdir()
    (blocked / "logs").write_text("a file where logs/ would go\n")
    out = log_path(blocked, "core-supervisor-relay.log")
    check("b) with logs/ uncreatable the log is /dev/null", out.stdout == "/dev/null", repr(out.stdout))
    check("b) and the helper still succeeds, so the caller still starts", out.returncode == 0,
          f"rc {out.returncode}")

for rel in LAUNCHERS:
    text = (REPO / rel).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    check(f"c) {rel} sources the shared owner", '. "$REPO/src/agent/core-workspace-log.sh"' in code)
    check(f"c) {rel} takes its monitor log from it",
          'mon_log="$(sutando_core_workspace_log "$ws" core-input-watch.log)"' in code)
    own = re.findall(r'/logs/core-[\w-]+\.log|mon_log=/dev/null|relay_log=/dev/null|/tmp/core-[\w-]+\.log', code)
    check(f"c) {rel} spells no log path or fallback of its own", own == [], str(own))

print()
if failures:
    print(f"FAILED {len(failures)}: {failures}")
    sys.exit(1)
print("all core workspace-log checks passed")
