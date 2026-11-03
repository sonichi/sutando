#!/usr/bin/env python3
"""The Claude launcher's input watch and its relay loop log to the workspace's own logs/.

Both used shared /tmp paths: a scratch core on another socket truncated the production
core's input-watch log (`>`), and its relay lines mixed into the production relay log (`>>`).

This cuts the launcher's own `ensure_core_monitor` out of the script (not a copy) and runs
it with stubbed sutando-config.sh, pgrep, monitor and relay, then reads what each wrote.
`sleep` is stubbed to fail, so the relay loop ends after one pass instead of every 30 s.

Run: python3 tests/start-cli-claude-monitor-logs.test.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" -- {detail}"))
    if not cond:
        failures.append(name)


def ensure_core_monitor_source() -> str:
    text = LAUNCHER.read_text(encoding="utf-8")
    m = re.search(r"^ensure_core_monitor\(\) \{\n.*?^\}\n", text, re.M | re.S)
    if not m:
        raise SystemExit("ensure_core_monitor() not found in the launcher")
    return m.group(0)


def stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


with tempfile.TemporaryDirectory() as td:
    root, ws, binp = Path(td) / "repo", Path(td) / "workspace", Path(td) / "bin"
    (ws / "state").mkdir(parents=True)
    stub(root / "scripts" / "sutando-config.sh", f'#!/bin/bash\n[ "$1" = workspace ] && printf "%s" "{ws}"\n')
    stub(root / "src" / "core-input-watch.py", "print('monitor-stdout', flush=True)\n")
    stub(root / "src" / "core-supervisor-relay.py", "print('relay-stdout', flush=True)\n")
    stub(root / "src" / "tmux-probe-cli.py", "raise SystemExit(0)\n")
    stub(binp / "pgrep", "#!/bin/bash\nexit 1\n")                    # no monitor runs yet
    stub(binp / "sleep", "#!/bin/bash\nexit 1\n")                    # one relay pass, then the loop ends
    script = (ensure_core_monitor_source()
              + 'ensure_core_monitor\nwait\n')
    env = {**os.environ, "PATH": f"{binp}:/usr/bin:/bin", "REPO": str(root), "PY": sys.executable,
           "TMUX_SOCKET": str(Path(td) / "core.sock"), "SESSION": "sutando-core"}
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    check("ensure_core_monitor ran", proc.returncode == 0, f"rc {proc.returncode}: {proc.stderr.strip()[:300]}")
    logs = ws / "logs"
    watch, relay = logs / "core-input-watch.log", logs / "core-supervisor-relay.log"
    check("the input watch logs to the workspace's logs/",
          watch.exists() and "monitor-stdout" in watch.read_text(), f"{watch} exists={watch.exists()}")
    check("the relay loop logs to the workspace's logs/",
          relay.exists() and "relay-stdout" in relay.read_text(), f"{relay} exists={relay.exists()}")

print()
if failures:
    print(f"FAILED {len(failures)}: {failures}")
    sys.exit(1)
print("all Claude launcher monitor-log checks passed")
