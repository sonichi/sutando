#!/usr/bin/env python3
"""Smoke-test a built ag2-sparrow entry: with no token it must exit non-zero
naming REMOTE_TASK_TOKEN, and never with a traceback (a missing module).

Usage: smoke_entry.py <command...>   e.g. smoke_entry.py ./ag2-sparrow-linux-x86_64
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile


def minimal_env(home: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("REMOTE_TASK_", "AG2_", "SUTANDO_", "AGENT_CONNECT_", "PYTHON"))}
    env["HOME"] = home
    env["AGENT_CONNECT_STATE_DIR"] = os.path.join(home, "state")
    return env


def check_fails_without_token(cmd: list, cwd: str) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd,
                       env=minimal_env(cwd), timeout=60)
    assert r.returncode != 0, f"{cmd} with no token exited 0:\n{r.stdout}\n{r.stderr}"
    assert "Traceback" not in r.stderr, r.stderr
    assert "REMOTE_TASK_TOKEN" in r.stderr, f"expected the missing-token message:\n{r.stderr}"
    return r.stderr.strip()


def main(argv=None) -> int:
    cmd = [os.path.abspath(a) if os.path.exists(a) else a for a in (argv or sys.argv[1:])]
    if not cmd:
        sys.exit(__doc__)
    with tempfile.TemporaryDirectory() as d:
        msg = check_fails_without_token(cmd, os.path.realpath(d))
    print(f"PASS {' '.join(cmd)} fails cleanly without a token: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
