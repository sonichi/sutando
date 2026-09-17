#!/usr/bin/env python3
"""The relay loop that start-cli.sh backgrounds must stop when its inputs vanish.

The launcher runs `bash -c '<loop>' relay-loop "$PY" "$REPO/src/core-supervisor-relay.py" …`
detached, with `$PY` an absolute path and `sleep` resolved from PATH. A test that
runs the real launcher from a temporary repo copy (shutdown-sentinel-immediate-exit-
controls) deletes that copy afterwards; the loop then has no interpreter and no
`sleep`, so `while true` spins at full CPU forever, appending two error lines per
iteration to /tmp/core-supervisor-relay.log (107 GB on one machine before it was
noticed). The same shape bites whenever the engine directory is removed from under
a running loop.

This test extracts the loop body from start-cli.sh and drives it directly:
  a) interpreter, script and `sleep` all gone -> exits promptly, no spin
  b) the relay is invoked with the launcher's argv and the loop ends once the
     script disappears
  c) a failing `sleep` ends the loop instead of degrading into a busy loop

Run: python3 tests/start-cli-relay-loop-dies-with-its-repo.test.py  (exit 0/1)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
failures: list[str] = []


def _loop_body() -> str:
    src = LAUNCHER.read_text(encoding="utf-8")
    m = re.search(r"bash -c '([^']*)' \\\n\s*relay-loop ", src)
    assert m, "relay loop launch not found in start-cli.sh — did its shape change?"
    return m.group(1)


def _run(body: str, argv: list[str], path: str, timeout: float = 5.0):
    """Returns (rc, stderr) or (None, stderr) when the loop outlived the timeout."""
    p = subprocess.Popen(["/bin/bash", "-c", body, "relay-loop", *argv],
                         env={"PATH": path}, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, err = p.communicate(timeout=timeout)
        return p.returncode, err
    except subprocess.TimeoutExpired:
        p.kill()
        _, err = p.communicate()
        return None, err


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(f"{label}: {detail}")


body = _loop_body()

# a) the fixture is gone: no interpreter, no script, no `sleep` on PATH.
with tempfile.TemporaryDirectory() as td:
    gone = Path(td) / "gone"
rc, err = _run(body, [f"{gone}/bin/python3", f"{gone}/repo/src/core-supervisor-relay.py",
                      f"{gone}/ws/state/core-supervisor.json",
                      f"{gone}/ws/state/core-supervisor-relay.state",
                      f"{gone}/ws/state/last-owner-activity.json"],
               path=f"{gone}/bin")
check("a) loop exits when its repo and PATH are gone", rc is not None,
      f"still running after 5s; wrote {err.count(chr(10))} stderr lines — the busy loop")
check("a) no spin before exit", err.count("\n") <= 4,
      f"{err.count(chr(10))} stderr lines before exiting")

# b) the relay runs with the launcher's argv, and the loop ends once the script vanishes.
with tempfile.TemporaryDirectory() as td:
    binp, repo = Path(td) / "bin", Path(td) / "repo"
    binp.mkdir()
    (repo / "src").mkdir(parents=True)
    calls = Path(td) / "calls.log"
    script = repo / "src" / "core-supervisor-relay.py"
    script.write_text("# stand-in for the relay\n")
    # Records its argv, then removes the script: the next loop check must stop.
    _exe(binp / "python3", f'#!/bin/sh\necho "$@" >> "{calls}"\n/bin/rm -f "$1"\n')
    _exe(binp / "sleep", "#!/bin/sh\nexit 0\n")
    rc, err = _run(body, [str(binp / "python3"), str(script), "SIG", "STATE", "ACTIVE"],
                   path=str(binp))
    recorded = calls.read_text().splitlines() if calls.exists() else []
    check("b) loop exits once the relay script is gone", rc is not None,
          f"still running after 5s; relay invoked {len(recorded)} times; stderr: {err[-300:]}")
    check("b) relay invoked exactly once with the launcher's argv",
          recorded == [f"{script} --signal SIG --state-file STATE --active-from ACTIVE"],
          f"recorded {len(recorded)} calls; first: {recorded[:1]!r}")

# c) `sleep` failing must end the loop, not turn it into a busy loop.
with tempfile.TemporaryDirectory() as td:
    binp, repo = Path(td) / "bin", Path(td) / "repo"
    binp.mkdir()
    (repo / "src").mkdir(parents=True)
    calls = Path(td) / "calls.log"
    script = repo / "src" / "core-supervisor-relay.py"
    script.write_text("# stand-in for the relay\n")
    _exe(binp / "python3", f'#!/bin/sh\necho run >> "{calls}"\n')
    _exe(binp / "sleep", "#!/bin/sh\nexit 1\n")
    rc, err = _run(body, [str(binp / "python3"), str(script), "SIG", "STATE", "ACTIVE"],
                   path=str(binp))
    n = len(calls.read_text().splitlines()) if calls.exists() else 0
    check("c) a failing sleep ends the loop", rc is not None and rc != 0,
          f"rc={rc}; relay invoked {n} times")
    check("c) the relay ran once before the loop gave up", n == 1, f"relay invoked {n} times")

if failures:
    print("\n".join(f"  FAIL {f}" for f in failures))
else:
    print("  ok  the relay loop stops when its interpreter, script or sleep are gone")
sys.exit(1 if failures else 0)
