#!/usr/bin/env python3
"""A real Claude launcher run starts the input watch and its relay loop with the workspace's logs.

Both used shared /tmp paths: a scratch core on another socket truncated the production
core's input-watch log (`>`), and its relay lines mixed into the production relay log (`>>`).

This drives the REAL `src/agent/claude/cli/start-cli.sh` into its heal branch: tmux reports the
session, no core process exists, and `pgrep` finds no monitor, so the launcher itself calls
`ensure_core_monitor`. The monitor and relay are stub scripts that print a marker. `sleep` is
stubbed to fail, so the relay loop ends after one pass instead of every 30 s.
  a) both markers land in <workspace>/logs/
  b) with logs/ uncreatable, the launcher still starts the monitor and the relay, and writes no log there

Run: python3 tests/start-cli-claude-monitor-logs.test.py
"""
from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REAL_REPO = Path(__file__).resolve().parent.parent
LAUNCHER = "src/agent/claude/cli/start-cli.sh"
NEEDED = (
    "src/agent/claude/cli/start-cli.sh", "src/agent/claude/cli/session-launch.sh",
    "src/agent/restart-guard.sh", "src/agent/core-runtime-record.sh", "src/agent/core-window.sh",
    "src/agent/core-workspace-log.sh", "src/agent/launch-path.sh",
    "src/core_runtime_record.py", "src/result_publish.py", "src/agent/task-event-handler-lookup.sh",
    "src/claude_config_dir.sh", "src/local_services.sh", "src/shutdown.py", "src/workspace_default.py",
    "src/sutando_config.py", "src/util_paths.py", "src/watch-tasks-stream.sh",
    "scripts/python-binary.sh", "scripts/sutando-config.sh", "scripts/install-personal-claude-hook.sh",
)
TMUX_STUB = """#!/bin/bash
case "$*" in
  *new-window*|*new-session*) echo 0; exit 0 ;;
  *has-session*)              exit 0 ;;
  *list-windows*)             echo "0: core"; exit 0 ;;
  *)                          exit 0 ;;
esac
"""
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" -- {detail}"))
    if not cond:
        failures.append(name)


def _exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def _reap(tmp: str) -> None:
    """Background children (monitor, relay loop) outlive the launcher; stop them before cleanup."""
    marker = os.path.basename(tmp)
    for _ in range(100):
        pids = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
        if not pids:
            return
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid), signal.SIGTERM)
        time.sleep(0.02)


def launch(logs_blocked: bool = False):
    """One real launcher run; returns (workspace, stderr)."""
    tmp = tempfile.mkdtemp(prefix="claude-monitor-logs-")
    root, binp = Path(tmp) / "repo", Path(tmp) / "bin"
    binp.mkdir(parents=True)
    for rel in NEEDED:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REAL_REPO / rel, dst)
    _exe(binp / "tmux", TMUX_STUB)
    env = {**os.environ, "PATH": f"{binp}:/usr/bin:/bin", "HOME": str(root),
           "SUTANDO_TMUX_SOCKET": str(root / "sock"), "SUTANDO_TMUX_SESSION": "sutando-core"}
    env.pop("TMUX", None)
    # The workspace the launcher's ensure_core_monitor will use, asked of the same resolver.
    ws = Path(subprocess.run(["bash", str(root / "scripts/sutando-config.sh"), "workspace"],
                             env=env, cwd=str(root), capture_output=True, text=True).stdout.strip())
    (ws / "state").mkdir(parents=True, exist_ok=True)
    if logs_blocked:
        (ws / "logs").write_text("a file where logs/ would go, so mkdir -p fails\n")
    ran, relayed = Path(tmp) / "monitor-ran", Path(tmp) / "relay-ran"
    _exe(root / "src" / "core-input-watch.py",
         f"import pathlib\npathlib.Path({str(ran)!r}).touch()\nprint('monitor-stdout', flush=True)\n")
    _exe(root / "src" / "core-supervisor-relay.py",
         f"import pathlib\npathlib.Path({str(relayed)!r}).touch()\nprint('relay-stdout', flush=True)\n")
    _exe(root / "src" / "tmux-probe-cli.py", "raise SystemExit(0)\n")
    _exe(binp / "pgrep", "#!/bin/bash\nexit 1\n")       # no core and no monitor run yet
    _exe(binp / "sleep", "#!/bin/bash\nexit 1\n")       # one relay pass, then the loop ends
    _exe(binp / "claude", "#!/bin/bash\nexit 0\n")
    _exe(binp / "codex", "#!/bin/bash\nexit 0\n")
    _exe(binp / "fswatch", "#!/bin/bash\nexit 0\n")
    for real in ("bash", "python3", "sed", "awk", "grep", "ps", "seq", "cat", "mkdir", "rm", "date",
                 "uname", "dirname", "basename", "tr", "head", "tail", "cut", "wc", "sort", "id",
                 "hostname", "cksum", "mktemp", "touch", "chmod", "ln", "cp", "mv", "pwd", "expr",
                 "printf", "sh", "find", "xargs", "stat", "realpath", "env", "which"):
        found = shutil.which(real)
        if found and not (binp / real).exists():
            (binp / real).symlink_to(found)
    env["PATH"] = str(binp)
    try:
        proc = subprocess.run(["bash", str(root / LAUNCHER)], capture_output=True, text=True,
                              env=env, cwd=str(root), timeout=180)
        deadline = time.monotonic() + 10       # the monitor and relay are backgrounded
        while time.monotonic() < deadline and not (ran.exists() and relayed.exists()):
            time.sleep(0.05)
        time.sleep(0.5)
        return ws, ran.exists(), relayed.exists(), proc.stderr
    finally:
        _reap(tmp)


ws, ran, relayed, err = launch()
logs = ws / "logs"
watch, relay = logs / "core-input-watch.log", logs / "core-supervisor-relay.log"
check("a) the launcher started the monitor", ran, err.strip()[-300:])
check("a) the launcher started the relay loop", relayed, err.strip()[-300:])
check("a) the input watch logs to the workspace's logs/",
      watch.exists() and "monitor-stdout" in watch.read_text(), f"{watch} exists={watch.exists()}")
check("a) the relay loop logs to the workspace's logs/",
      relay.exists() and "relay-stdout" in relay.read_text(), f"{relay} exists={relay.exists()}")

ws, ran, relayed, err = launch(logs_blocked=True)
check("b) with logs/ uncreatable the monitor still starts", ran, err.strip()[-300:])
check("b) and the relay loop still runs, so its escalation is not lost", relayed, err.strip()[-300:])
check("b) and nothing is written in place of logs/", (ws / "logs").is_file()
      and (ws / "logs").read_text().startswith("a file where"), "logs/ was replaced")

print()
if failures:
    print(f"FAILED {len(failures)}: {failures}")
    sys.exit(1)
print("all Claude launcher monitor-log checks passed")
