#!/usr/bin/env python3
"""scripts/restart-gateway-lanes.sh finds the lanes startup.sh finds, and reports what it did.

Reported (#6248): run without CLAUDE_CONFIG_DIR, it read ~/.claude's channel env, started nothing
for the configured lane, and still printed "Gateway lane(s) (re)started where configured."
This runs the real script in a scratch repo; the bridge and connections.py are recording stubs.
  a) CLAUDE_CONFIG_DIR unset: the token in the configured store is found, and both lanes launch
  b) no token anywhere: exit 1, says so, starts nothing and never claims a restart
  c) a lane that fails to launch: exit 1, says so, and never claims a restart

Run: python3 tests/restart-gateway-lanes-says-what-happened.test.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _helpers.hermetic_env import hermetic_env, isolate_os_environ  # noqa: E402

isolate_os_environ()

REAL_REPO = Path(__file__).resolve().parent.parent
CLAIM = "(re)started where configured"
BANNER = "CLAUDE_CONFIG_DIR"
failures: list[str] = []

CONNECTIONS_STUB = """#!/usr/bin/env python3
import os, sys
from pathlib import Path
Path(os.environ["STUB_LOG"]).open("a").write("connections " + " ".join(sys.argv[1:]) + "\\n")
cmd = sys.argv[1]
if cmd == "legacy-parked":
    sys.exit(1)
if cmd == "launch-plan":
    print("s_dspace\\tx")
if cmd == "launch":
    sys.exit(int(os.environ.get("STUB_LAUNCH_RC", "0")))
"""
BRIDGE_STUB = """#!/usr/bin/env python3
import os
from pathlib import Path
Path(os.environ["STUB_LOG"]).open("a").write("bridge token=" + os.environ.get("REMOTE_TASK_TOKEN", "") + "\\n")
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" -- {detail}"))
    if not cond:
        failures.append(name)


def scratch() -> Path:
    root = Path(tempfile.mkdtemp(prefix="restart-lanes-")) / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "src").mkdir()
    for pattern, sub in (("*.sh", "scripts"), ("*.sh", "src"), ("*.py", "src")):
        for f in (REAL_REPO / sub).glob(pattern):
            shutil.copy2(f, root / sub / f.name)
    shutil.copy2(REAL_REPO / "sutando.config.json", root / "sutando.config.json")
    shutil.copytree(REAL_REPO / "shared", root / "shared")     # channel-env's credential contract
    (root / "scripts" / "connections.py").write_text(CONNECTIONS_STUB)
    (root / "src" / "remote-gateway-bridge.py").write_text(BRIDGE_STUB)
    return root


def run(root: Path, launch_rc: str = "0", token: bool = True):
    home, log = root.parent / "home", root.parent / "stub.log"
    home.mkdir()
    env = hermetic_env(home, STUB_LOG=str(log), STUB_LAUNCH_RC=launch_rc)
    store = Path(subprocess.run(["bash", str(root / "scripts/sutando-config.sh"), "claude-sutando-config-dir"],
                                env=env, capture_output=True, text=True).stdout.strip())
    if token:
        channel = store / "channels" / "ag2space"
        channel.mkdir(parents=True)
        (channel / ".env").write_text("AG2_REMOTE_TOKEN='https://relay.example|secret'\n")
    proc = subprocess.run(["bash", str(root / "scripts/restart-gateway-lanes.sh")], env=env, cwd=str(root),
                          capture_output=True, text=True, timeout=120)
    time.sleep(0.5)     # the primary bridge is launched in the background
    return proc, (log.read_text() if log.exists() else "")


proc, log = run(scratch())
output = proc.stdout + proc.stderr
check("a) exit 0", proc.returncode == 0, output[-400:])
check("a) the primary bridge started with the configured store's token", "bridge token=https://relay.example|secret" in log, log)
check("a) the configured secondary lane launched", "connections launch s_dspace" in log, log)
check("a) no fallback to ~/.claude", BANNER not in proc.stderr, proc.stderr[-300:])
check("a) and it says so", CLAIM in proc.stdout, proc.stdout[-300:])

proc, log = run(scratch(), token=False)
output = proc.stdout + proc.stderr
check("b) no token: exit 1", proc.returncode == 1, f"rc {proc.returncode}")
check("b) nothing starts", "bridge" not in log and "connections launch " not in log, log)
check("b) it says none started", "No gateway lane started" in proc.stderr, proc.stderr[-300:])
check("b) and does not claim a restart", CLAIM not in output, output[-300:])

proc, log = run(scratch(), launch_rc="1")
output = proc.stdout + proc.stderr
check("c) a lane that fails to launch: exit 1", proc.returncode == 1, f"rc {proc.returncode}")
check("c) the lane is named", "gateway bridge (s_dspace) not launched" in proc.stdout, proc.stdout[-300:])
check("c) the count is reported", "1 gateway lane(s) not launched" in proc.stderr, proc.stderr[-300:])
check("c) and it does not claim a restart", CLAIM not in output, output[-300:])

print()
if failures:
    print(f"FAILED {len(failures)}: {failures}")
    sys.exit(1)
print("all restart-gateway-lanes reporting checks passed")
