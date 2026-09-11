#!/usr/bin/env python3
"""Does THIS worker instance still need its watcher started?

The core's gate (`/schedule-crons` step 1.5) asks whether ANY watcher tree is
running. On a pool host that is always true — the core's own — so it answers
`skip` and suppresses exactly the watcher a worker exists to run. The question
is per instance, so this asks about one instance's own sentinel and nothing
else: the file `util_paths.watcher_sentinel_path` names for this identity.

Decision on stdout: `start`, `skip`, or `unknown`.
Exit 0 when decided, 2 when it cannot be (an unknown is never a `start`).

Owned by the pool skill; the core's `/startup --worker` runs whatever
$SUTANDO_WORKER_BOOTSTRAP names, which the spawner sets to this file.

Run: python3 skills/worker-pool/scripts/worker_bootstrap.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Skill script: reach the core's util_paths/workspace resolvers in src/ (repo
# root is parents[3] of skills/<name>/scripts/<file>.py, symlinks resolved).
REPO = Path(__file__).resolve().parents[3]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # it exists; this user may not signal it
    except OSError:
        return False
    return True


def sentinel_for(state_dir, instance: str) -> Path:
    """Resolved by the watcher's own owner, so the two cannot disagree about
    which file this instance stamps."""
    import util_paths
    return util_paths.watcher_sentinel_path(state_dir, instance=instance)


def decide(*, instance: str, inbox: str, workspace: str,
           alive=_pid_alive, resolve=sentinel_for) -> tuple[str, str]:
    """(decision, why). Pure over its seams: no process is started here and
    liveness arrives as a callable, so both polarities are testable."""
    if not instance:
        return "unknown", "no SUTANDO_INSTANCE_ID: this is not a worker session"
    if not inbox:
        return "unknown", "no SUTANDO_TASKS_DIR: this worker has no delivery folder"
    if not workspace:
        return "unknown", "no workspace: the sentinel's state dir is unknown"
    try:
        sentinel = resolve(Path(workspace) / "state", instance)
    except Exception as e:                                   # noqa: BLE001
        return "unknown", f"could not resolve this instance's sentinel: {e}"
    try:
        raw = sentinel.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "start", f"no sentinel at {sentinel} — this instance has no watcher"
    except OSError as e:
        return "unknown", f"sentinel {sentinel} is unreadable: {e}"
    pid = raw.split()[0] if raw.split() else ""
    if not pid.isdigit():
        return "start", f"sentinel {sentinel} holds no pid ({raw!r})"
    if alive(int(pid)):
        return "skip", f"this instance's watcher is live (pid {pid})"
    return "start", f"sentinel {sentinel} names a dead pid ({pid})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="worker watcher bootstrap decision")
    ap.add_argument("--instance", default=os.environ.get("SUTANDO_INSTANCE_ID", ""))
    ap.add_argument("--inbox", default=os.environ.get("SUTANDO_TASKS_DIR", ""))
    # Not the spawner's env var: a worker SHARES the host's workspace, so the
    # canonical loader is the answer and the contract's only resolution path.
    ap.add_argument("--workspace", default="")
    a = ap.parse_args(argv)
    workspace = a.workspace
    if not workspace:
        try:
            from workspace_default import resolve_workspace
            workspace = str(resolve_workspace())
        except Exception:                                    # noqa: BLE001
            workspace = ""
    decision, why = decide(instance=a.instance, inbox=a.inbox, workspace=workspace)
    print(decision)
    print(f"instance={a.instance or '(unset)'} inbox={a.inbox or '(unset)'}")
    print(f"why={why}")
    return 0 if decision in ("start", "skip") else 2


if __name__ == "__main__":
    sys.exit(main())
