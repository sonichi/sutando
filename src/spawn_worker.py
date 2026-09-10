#!/usr/bin/env python3
"""Create a worker: an identity, a delivery folder, a tmux session, a watcher.

The design's one positive invariant is that every task worker is associated with
a task delivery mechanism — in Sutando a watcher. A folder with nothing reading
it is a name in a roster, not a worker, so this module either produces all four
parts or none of them.

What it does NOT do: supervise the worker's session. A restart is not a resume,
and deciding what a stopped worker needs is judgement. launchd supervises the
WATCHER; the session is started once, here, and resumed only when asked.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import worker_identity as wi  # noqa: E402

DEFAULT_SOCKET = "/tmp/sutando-tmux.sock"


def default_socket() -> str:
    """Read at CALL time. A module-level default binds at import, so a caller
    that sets the env afterwards would silently target the wrong tmux server."""
    return os.environ.get("SUTANDO_TMUX_SOCKET") or DEFAULT_SOCKET
WATCHER = "src/watch-tasks-stream.sh"


class SpawnRefused(Exception):
    """A precondition failed. Nothing was created."""


def _run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def per_instance_sentinel_supported(repo) -> bool:
    """Does this checkout give each watcher its OWN sentinel?

    On a single-sentinel checkout the Nth watcher erases the (N-1)th stamp, so
    spawning corrupts the core's own liveness signal rather than the worker's.
    Fail closed: refuse rather than half-create.
    """
    # Read the file, never import it: a cached `util_paths` from another
    # checkout would report on this process, not the repo asked about.
    try:
        src = (Path(repo) / "src" / "util_paths.py").read_text(encoding="utf-8")
    except OSError:
        return False
    return "def watcher_sentinel_path" in src


def session_exists(name: str, socket=None, runner=_run) -> bool:
    """`=name` is exact: without it tmux prefix-matches and a short id resolves
    to a different worker."""
    socket = socket or default_socket()
    return runner(["tmux", "-S", socket, "has-session", "-t", f"={name}"]).returncode == 0


def plan(workspace, repo, *, runtime: str = "claude", cwd: str = "",
         socket=None, label: str = "") -> dict:
    """What a spawn would create. Pure — no side effects, so it is reviewable
    before anything exists and testable without tmux."""
    socket = socket or default_socket()
    worker_id = wi.new_worker_id()
    return {
        "worker_id": worker_id,
        "label": label or worker_id,
        "runtime": runtime,
        "cwd": str(cwd or repo),
        "delivery_dir": str(Path(workspace) / "deliveries" / worker_id),
        "tmux": {"socket": socket, "session_name": wi.tmux_session_name(worker_id)},
        # Absolute, from `repo`: WATCHER relative would resolve against the
        # session's cwd, so `cwd` would silently pick which code the worker runs.
        "watcher_argv": ["env", f"SUTANDO_INSTANCE_ID={worker_id}",
                         "bash", str(Path(repo) / WATCHER),
                         str(Path(workspace) / "deliveries" / worker_id)],
    }


def spawn(workspace, repo, *, runtime: str = "claude", cwd: str = "",
          socket=None, label: str = "", runner=_run,
          require_sentinel: bool = True) -> dict:
    """Create the four parts, in an order where a failure leaves less behind.

    Identity first (a record with no process is inert), then the delivery folder,
    then the tmux session, then the watcher inside it. The watcher is last
    because it is the only part that starts reading.
    """
    socket = socket or default_socket()
    if require_sentinel and not per_instance_sentinel_supported(repo):
        raise SpawnRefused(
            "this checkout writes ONE watcher sentinel for every watcher, so a "
            "second watcher would erase the core's stamp — refusing to spawn")

    p = plan(workspace, repo, runtime=runtime, cwd=cwd, socket=socket, label=label)
    name = p["tmux"]["session_name"]
    if session_exists(name, socket, runner):
        raise SpawnRefused(f"tmux session {name!r} already exists")

    Path(p["delivery_dir"]).mkdir(parents=True, exist_ok=True)

    rec = wi.create_worker(workspace, runtime=runtime, cwd=p["cwd"],
                           host=os.uname().nodename, tmux_socket=socket)
    # plan() minted a candidate id; the identity record is the authority.
    worker_id = rec["worker_id"]
    if worker_id != p["worker_id"]:
        Path(p["delivery_dir"]).rmdir()
        p = plan(workspace, repo, runtime=runtime, cwd=cwd, socket=socket, label=label)
        p["worker_id"] = worker_id
        p["delivery_dir"] = str(Path(workspace) / "deliveries" / worker_id)
        p["tmux"]["session_name"] = name = rec["tmux_session"]
        p["watcher_argv"] = ["env", f"SUTANDO_INSTANCE_ID={worker_id}",
                             "bash", str(Path(repo) / WATCHER), p["delivery_dir"]]
        Path(p["delivery_dir"]).mkdir(parents=True, exist_ok=True)

    r = runner(["tmux", "-S", socket, "new-session", "-d", "-P", "-F",
                "#{pane_id}", "-s", name, "-c", p["cwd"]])
    if r.returncode != 0:
        raise SpawnRefused(f"tmux new-session failed: {(r.stderr or '').strip()}")

    # `=name` targets an exact PANE name, not a session, so send-keys cannot
    # find it; -P -F prints the new pane's id, which addresses it directly.
    pane = (r.stdout or "").strip() or f"={name}"
    watcher = " ".join(shlex.quote(a) for a in p["watcher_argv"])
    r = runner(["tmux", "-S", socket, "send-keys", "-t", pane, watcher, "Enter"])
    if r.returncode != 0:
        raise SpawnRefused(f"starting the watcher failed: {(r.stderr or '').strip()}")

    return {**p, **rec, "started": True}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="create a worker")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--repo", default=str(_SRC.parent))
    ap.add_argument("--folder", default="", help="the worker's working directory")
    ap.add_argument("--label", default="")
    ap.add_argument("--runtime", default="claude")
    ap.add_argument("--socket", default="")
    ap.add_argument("--new", action="store_true", help="fresh session (default)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        fn = plan if a.dry_run else spawn
        print(json.dumps(fn(a.workspace, a.repo, runtime=a.runtime, cwd=a.folder,
                            socket=a.socket or None, label=a.label), indent=2))
    except SpawnRefused as e:
        print(f"spawn-worker refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
