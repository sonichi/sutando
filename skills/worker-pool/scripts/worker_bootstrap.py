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
import subprocess
import sys
from pathlib import Path

# Skill script: reach the core's util_paths/workspace resolvers in src/ (repo
# root is parents[3] of skills/<name>/scripts/<file>.py, symlinks resolved).
REPO = Path(__file__).resolve().parents[3]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

import watcher_identity as wid  # noqa: E402

WATCHER_SCRIPT = wid.WATCHER_SCRIPT_NAME


class Unobserved(Exception):
    """The inspection proved nothing either way: not a verdict, so never `start`."""


def _watcher_target(pid: int, run=None, argv_vector=None):
    """What inbox is this pid's watcher watching?

    None  — proven not a watcher (a recycled or unrelated pid).
    ""    — a watcher whose inbox argv does not show, so ownership is unknown.
    path  — the inbox it was told to watch.
    Raises Unobserved when `ps` proved nothing or the argv cannot be decided.
    """
    got = wid.inspect_pid(pid, run=run if run is not None else subprocess.run,
                          argv_vector=argv_vector)
    if not got.observed:
        raise Unobserved(got.reason)
    return _target_from_argv(got.argv, pid, argv_vector)


def _target_from_argv(command: str, pid=None, argv_vector=None):
    """The inbox a watcher command line names, "" when it names none, None when
    it is not a watcher. The shared anchored policy decides which; an argv it
    cannot decide raises Unobserved rather than disowning a live watcher."""
    verdict = wid.classify_argv(command, pid, argv_vector)
    if verdict.watcher is None:
        raise Unobserved(f"argv {command!r} cannot be decided without the process's "
                         f"real argv vector")
    if verdict.watcher is False:
        return None
    for tok in verdict.operands:
        if not tok.startswith("-"):
            return tok
    return ""


def _same_path(a: str, b: str) -> bool:
    """One directory reached by two spellings — a symlinked temp prefix, a
    trailing slash — is not two inboxes."""
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return a.rstrip("/") == b.rstrip("/")


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
           alive=_pid_alive, resolve=sentinel_for,
           watcher_target=_watcher_target) -> tuple[str, str]:
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
    if not alive(int(pid)):
        return "start", f"sentinel {sentinel} names a dead pid ({pid})"
    # Being SOME watcher is not being THIS worker's: a reused pid belonging to
    # another worker's watcher would suppress this one and strand its inbox.
    try:
        target = watcher_target(int(pid))
    except Unobserved as e:
        return "unknown", (f"sentinel {sentinel} names live pid {pid}, and whether it "
                           f"is a {WATCHER_SCRIPT} could not be observed ({e}); a "
                           f"duplicate watcher processes every delivery twice")
    if target is None:
        return "start", (f"sentinel {sentinel} names live pid {pid}, which is not "
                         f"a {WATCHER_SCRIPT} — treating the sentinel as stale")
    if target == "":
        return "unknown", (f"pid {pid} is a {WATCHER_SCRIPT} but the inbox it "
                           f"watches is not visible, so it cannot be told apart "
                           f"from another worker's watcher")
    if not _same_path(target, inbox):
        return "start", (f"sentinel {sentinel} names pid {pid}, a watcher of "
                         f"{target} — not this worker's inbox {inbox}")
    return "skip", f"this instance's watcher is live (pid {pid}) on {inbox}"


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
