#!/usr/bin/env python3
"""Give Codex-core schedules a durable launchd owner.

Codex does not have Claude Code's session CronCreate surface.  When a host
switches to the Codex core, ordinary crons.json entries would otherwise remain
defined but stop firing after the old Claude session exits.  Reconciliation is
idempotent and initializes runner state before changing ownership so enabling
the durable runner cannot replay a backlog of old actions.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from cron_execution_form import (  # noqa: E402
    launchd_eligible as _shared_launchd_eligible)


def launchd_eligible(entry: dict[str, Any]) -> bool:
    """Return whether Codex should move this entry to the launchd runner.

    Delegates: this predicate is also read by the Codex scheduler and the
    session-crons health probe, and a private copy is how a record they all
    declined came to be owned by nobody.
    """
    return _shared_launchd_eligible(entry)


def runner_required(crons: list[Any]) -> bool:
    """Return whether the current config needs the launchd runner.

    This read-only preflight lets the launcher install the runner before
    ownership is changed.  Eligible entries without a usable name are ignored
    because ``reconcile`` cannot migrate them.
    """
    for entry in crons:
        if not isinstance(entry, dict):
            continue
        if entry.get("launchd") is True:
            return True
        name = entry.get("name")
        if isinstance(name, str) and name and launchd_eligible(entry):
            return True
    return False


@contextmanager
def _state_lock(state_file: Path) -> Iterator[None]:
    """Exclusive lock serializing the ``cron-runner-state.json`` read-modify-write
    against a running launchd cron-runner.

    This reconciler and ``src/cron-runner.py``'s ``run()`` lock the SAME path
    (``<state_file>.lock``). Without it, this reconciler could seed a migration
    boundary that an already-in-flight runner tick then clobbers with its stale
    full-dict write-back — dropping the boundary and letting the next tick
    replay a whole ``MAX_CATCHUP_SECONDS`` window of daily crons. The lock makes
    the two read-modify-write sections mutually exclusive so the boundary
    always survives. Peer: ``cron-runner._state_lock``."""
    lock_path = state_file.parent / (state_file.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(value, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def reconcile(crons_file: Path, state_file: Path, now: Optional[int] = None) -> dict[str, Any]:
    """Move eligible entries to launchd ownership without replaying old slots."""
    crons = json.loads(crons_file.read_text())
    if not isinstance(crons, list):
        raise ValueError(f"{crons_file} must contain a JSON list")

    boundary = int(time.time() if now is None else now)
    migrated: list[str] = []

    # Serialize the state read-modify-write against a concurrent launchd tick so
    # the seeded boundary cannot be clobbered by the runner's write-back (and
    # vice-versa). See _state_lock for the race this closes.
    with _state_lock(state_file):
        try:
            state = json.loads(state_file.read_text())
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict):
            raise ValueError(f"{state_file} must contain a JSON object")

        for entry in crons:
            if not isinstance(entry, dict) or not launchd_eligible(entry):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            state.setdefault(name, boundary)
            migrated.append(name)

        if migrated:
            # Ordering is the safety property: a launchd tick can see either the
            # old session-owned config or the new config plus an initialized
            # boundary, never launchd ownership with an absent 24h catch-up
            # state. The state write happens under the lock so a runner tick
            # cannot interleave a stale write-back between it and the crons flip.
            _atomic_json(state_file, state)

    if migrated:
        for entry in crons:
            if isinstance(entry, dict) and entry.get("name") in migrated:
                entry["launchd"] = True
        _atomic_json(crons_file, crons)

    runner_needed = any(
        isinstance(entry, dict) and entry.get("launchd") is True for entry in crons
    )
    return {"migrated": migrated, "runner_needed": runner_needed}


def _default_paths() -> tuple[Path, Path]:
    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo / "src"))
    from util_paths import _host_label  # type: ignore  # noqa: PLC0415
    from workspace_default import resolve_workspace  # type: ignore  # noqa: PLC0415

    workspace = Path(resolve_workspace())
    return (
        workspace / "hosts" / _host_label() / "crons.json",
        workspace / "state" / "cron-runner-state.json",
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = [] if argv is None else argv
    crons_file, state_file = _default_paths()
    if not crons_file.exists():
        print("runner_needed=0 migrated=0")
        return 0
    if args == ["--check"]:
        crons = json.loads(crons_file.read_text())
        if not isinstance(crons, list):
            raise ValueError(f"{crons_file} must contain a JSON list")
        print(f"runner_needed={int(runner_required(crons))} migrated=0")
        return 0
    if args:
        print(f"usage: {Path(sys.argv[0]).name} [--check]", file=sys.stderr)
        return 2
    result = reconcile(crons_file, state_file)
    names = ",".join(result["migrated"])
    print(
        f"runner_needed={int(result['runner_needed'])} "
        f"migrated={len(result['migrated'])} names={names}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
