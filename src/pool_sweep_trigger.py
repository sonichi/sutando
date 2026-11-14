#!/usr/bin/env python3
"""Ask the worker pool for one sweep now, once per core-recovery event.

The pool's remedy timer sweeps every few minutes. When the core comes back from a usage-limit
hold or a re-login, the pool's parked seats are recovered by that sweep (pool_supervision's
new-login rule); this only makes it run at once instead of at the next tick. The policy stays
in the sweep: this starts no worker and decides nothing.

Each event names a key; a key already requested is not requested again, so one recovery event
causes exactly one sweep, however many readers report it. A missing worker-pool skill, or a
spawn that fails, is a no-op: the timer still sweeps.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402

REQUEST_FILE = "pool-sweep-request.json"
# Unresolved, as agent-api.py does: a bundle that symlinks src/ keeps its own skills/ beside it.
REPO = Path(__file__).parent.parent
SWEEP_SCRIPT = Path("skills") / "worker-pool" / "scripts" / "pool_remedy.py"


def request_path(workspace=None) -> Path:
    return Path(workspace if workspace is not None else resolve_workspace()) / "state" / REQUEST_FILE


def last_request(workspace) -> Optional[dict]:
    try:
        got = json.loads(request_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def _record(workspace, entry: dict) -> None:
    path = request_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(entry, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def request_sweep(workspace, cause: str, key: str, *, repo: Optional[Path] = None,
                  spawn: Optional[Callable] = None, now: Optional[float] = None) -> bool:
    """Start one detached `pool_remedy.py --sweep` for the event `key`; False when that key was
    already requested, the pool is not installed or has no roster here, or the spawn failed."""
    repo = Path(repo) if repo is not None else REPO
    spawn = spawn or subprocess.Popen
    script = repo / SWEEP_SCRIPT
    # No roster, no pool to sweep: a workspace that never spawned a worker asks for nothing.
    if not key or not script.is_file() or not (Path(workspace) / "state" / "roster.json").is_file():
        return False
    if (last_request(workspace) or {}).get("key") == key:
        return False
    now = time.time() if now is None else now
    _record(workspace, {"key": key, "cause": cause, "at": now})
    logs = Path(workspace) / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "pool-remedy.log", "ab") as log:
            spawn([sys.executable, str(script), "--workspace", str(workspace), "--repo", str(repo), "--sweep"],
                  stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    except OSError:
        return False
    return True
