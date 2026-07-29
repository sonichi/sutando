#!/usr/bin/env python3
"""remote-relay-bridge.py — DEPRECATED name; renamed to remote-gateway-bridge.py.

One-release compat stub so existing launchers (startup scripts, desktop
supervisors, tmux invocations) keep working. Runs the renamed client
IN-PROCESS (runpy) so `pgrep -f remote-relay-bridge` liveness checks still
match. Remove after one release; update launchers to the new filename.
"""
import runpy
import sys
from pathlib import Path

print("[remote-relay-bridge] DEPRECATED filename — use src/remote-gateway-bridge.py",
      file=sys.stderr, flush=True)
runpy.run_path(str(Path(__file__).resolve().parent / "remote-gateway-bridge.py"),
               run_name="__main__")
