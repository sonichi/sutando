#!/usr/bin/env python3
"""Run an action that can kill its caller (a restart of the session the caller lives in) in its own
session, and know its outcome by a deadline.

The core's and a worker's restart both go through here. `begin` records the action as running with
a deadline before anything starts; `detach` starts a process in its own session, so the take-down of
the caller's tmux session never takes it down; the action, or the `run` wrapper around a plain
command, writes the one terminal outcome with `report` (first report wins). `status` is what the
caller's successor reads: running until reported, and `expired` once the deadline passes with no
report, so nothing waits on an action that never answered. The `run` wrapper first waits until
its caller has recorded the hand-off (the recovery gate's claim is finished), and acts only while
the action is still running and unexpired, so a late or orphaned child never acts.

Records: state/recovery/detached/<key digest>.json
  {"key", "state": "running"|"done"|"failed", "started_at", "deadline_at", "reason"?, "rc"?}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, NamedTuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import status_path  # noqa: E402

#: Longest an action may stay unanswered before its caller treats it as failed.
DEADLINE_S = 5 * 60
#: The environment variable that hands the action's key to the process it starts.
KEY_ENV = "SUTANDO_DETACHED_KEY"
#: Longest the `run` wrapper waits for its caller to finish recording before it acts anyway.
HANDOFF_WAIT_S = 15.0
RUNNING, DONE, FAILED, EXPIRED = "running", "done", "failed", "expired"


class Status(NamedTuple):
    state: Optional[str]  # None when no action was begun under the key
    reason: Optional[str]
    rc: Optional[int]


def _path(workspace: Path, key: str) -> Path:
    return status_path("recovery", Path(workspace)) / "detached" / (
        hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")


def _read(workspace: Path, key: str) -> Optional[dict]:
    try:
        data = json.loads(_path(workspace, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(workspace: Path, key: str, body: dict) -> None:
    path = _path(workspace, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".detached-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(body, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def begin(workspace: Path, key: str, now: Optional[float] = None, deadline_s: float = DEADLINE_S) -> None:
    at = time.time() if now is None else now
    _write(workspace, key, {"key": key, "state": RUNNING, "started_at": at, "deadline_at": at + deadline_s})


def report(workspace: Path, key: str, outcome: str, reason: Optional[str] = None,
           rc: Optional[int] = None, now: Optional[float] = None) -> bool:
    """Record the terminal outcome; False when one was already recorded (the first one stands)."""
    prior = _read(workspace, key)
    if prior is None or prior.get("state") != RUNNING or outcome not in (DONE, FAILED):
        return False
    _write(workspace, key, dict(prior, state=outcome, reason=reason, rc=rc,
                                ended_at=time.time() if now is None else now))
    return True


def status(workspace: Path, key: str, now: Optional[float] = None) -> Status:
    data = _read(workspace, key)
    if data is None:
        return Status(None, None, None)
    state = data.get("state")
    if state == RUNNING and (time.time() if now is None else now) > float(data.get("deadline_at") or 0):
        return Status(EXPIRED, None, None)
    return Status(state, data.get("reason"), data.get("rc"))


def detach(argv: List[str], *, env: Optional[dict] = None, log: Optional[Path] = None,
           popen=subprocess.Popen) -> bool:
    """Start `argv` in its own session (it outlives the caller's tmux session); False if it could not start."""
    out = None
    try:
        if log is not None:
            log.parent.mkdir(parents=True, exist_ok=True)
            out = open(log, "ab")
        popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=out or subprocess.DEVNULL,
              stderr=out or subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return False
    finally:
        if out is not None:
            out.close()
    return True


def spawn(workspace: Path, key: str, argv: List[str], *, env: Optional[dict] = None, log: Optional[Path] = None,
          now: Optional[float] = None, deadline_s: float = DEADLINE_S, popen=subprocess.Popen,
          after_gate: Optional[str] = None) -> bool:
    """begin + detach the `run` wrapper around `argv`, which reports its exit code as the outcome
    unless `argv` reported first. With `after_gate`, the wrapper waits for that recovery-gate claim
    to be finished first. False (and the action reported failed) when it could not start."""
    begin(workspace, key, now, deadline_s)
    wrapped = [sys.executable, str(Path(__file__).resolve()), "run", "--workspace", str(workspace),
               "--key", key, *(["--after-gate", after_gate] if after_gate else []), "--", *argv]
    child_env = dict(os.environ if env is None else env, **{KEY_ENV: key})
    if detach(wrapped, env=child_env, log=log, popen=popen):
        return True
    report(workspace, key, FAILED, "could not start", now=now)
    return False


def _wait_for_gate(workspace: Path, gate: str, wait_s: float = HANDOFF_WAIT_S, sleep=time.sleep,
                   clock=time.monotonic) -> None:
    import recovery_gate
    end = clock() + wait_s
    while clock() < end:
        if (recovery_gate.state(workspace, gate) or {}).get("state") == recovery_gate.FINISHED:
            return
        sleep(0.2)


def _run(workspace: Path, key: str, argv: List[str], runner=subprocess.run, after_gate: Optional[str] = None,
         wait=_wait_for_gate) -> int:
    if after_gate:
        wait(workspace, after_gate)
    if status(workspace, key).state != RUNNING:
        return 1  # reported or expired meanwhile: this child is no longer the action, so it never acts
    try:
        rc = runner(argv, stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.SubprocessError):
        rc = None
    report(workspace, key, DONE if rc == 0 else FAILED, None if rc == 0 else "exit", rc)
    return 0 if rc == 0 else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--workspace", required=True)
    r.add_argument("--key", required=True)
    r.add_argument("--after-gate", default=None)
    r.add_argument("command", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    return _run(Path(a.workspace), a.key, command, after_gate=a.after_gate)


if __name__ == "__main__":
    sys.exit(main())
