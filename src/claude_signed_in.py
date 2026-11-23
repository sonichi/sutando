#!/usr/bin/env python3
"""Whether a Claude config dir is signed in on its own, for the launcher's direct-routing guard.

A session that worked through the credential proxy may have no login of its own (the proxy
injected the credential), so routing it direct would leave it unauthenticated. This asks
`claude auth status --json` (non-secret: it reports `loggedIn`, never a token) under that
config dir, with a short timeout, and records the answer in
`<workspace>/state/claude-direct-routing.json` for health-check. Exit 0: signed in; 1: not
signed in; 2: could not tell (the launcher stays on the proxy for both 1 and 2).

Usage: claude_signed_in.py --config-dir <dir> [--timeout <seconds>]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

RECORD = "claude-direct-routing.json"
TIMEOUT_S = 8.0


def _runner(config_dir: str, timeout: float) -> Callable:
    def run(cmd):
        cmd = [os.environ.get("CLAUDE_BIN") or cmd[0], *cmd[1:]]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=dict(os.environ, CLAUDE_CONFIG_DIR=config_dir))
        return proc.returncode, proc.stdout
    return run


def signed_in(config_dir: str, runner: Optional[Callable] = None, timeout: float = TIMEOUT_S) -> Optional[bool]:
    """True signed in, False not, None unknown (the probe failed, timed out or answered garbage)."""
    from hitl.detector import probe_claude_auth
    return probe_claude_auth(runner or _runner(config_dir, timeout)).ready


def record(workspace, config_dir: str, verdict: Optional[bool], now: Optional[float] = None) -> None:
    from workspace_default import status_path
    path = status_path(RECORD, Path(workspace))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data[os.path.realpath(config_dir)] = {"signed_in": verdict, "at": time.time() if now is None else now}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None, runner: Optional[Callable] = None, workspace=None) -> int:
    ap = argparse.ArgumentParser(prog="claude_signed_in")
    ap.add_argument("--config-dir", required=True)
    ap.add_argument("--timeout", type=float, default=TIMEOUT_S)
    a = ap.parse_args(argv)
    try:
        verdict = signed_in(a.config_dir, runner, a.timeout)
    except Exception:  # noqa: BLE001 — unknown, never a guess
        verdict = None
    try:
        if workspace is None:
            from workspace_default import resolve_workspace
            workspace = resolve_workspace(migrate=False)
        record(workspace, a.config_dir, verdict)
    except Exception:  # noqa: BLE001 — the launch decision does not depend on the record
        pass
    return 0 if verdict is True else 1 if verdict is False else 2


if __name__ == "__main__":
    sys.exit(main())
