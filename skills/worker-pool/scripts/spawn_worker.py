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
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_REPO = _SCRIPTS.parents[2]
# Sibling skill scripts, then the core's src/ for the delivery-record grammar
# (repo root is parents[3] of skills/<name>/scripts/<file>.py, symlinks resolved).
for _p in (str(_SCRIPTS), str(_REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pool_delivery as pd  # noqa: E402

import worker_identity as wi  # noqa: E402

DEFAULT_SOCKET = "/tmp/sutando-tmux.sock"


def default_socket() -> str:
    """Read at CALL time. A module-level default binds at import, so a caller
    that sets the env afterwards would silently target the wrong tmux server."""
    return os.environ.get("SUTANDO_TMUX_SOCKET") or DEFAULT_SOCKET
WATCHER = "src/watch-tasks-stream.sh"
# The core's own launcher, run under per-worker env: one argv, one set of
# hooks, one runtime for every session in the pool.
LAUNCHER = "src/agent/start-cli.sh"


# Worker mode is a property of an ADAPTER, not of the pool: a runtime is
# listed here once its launcher isolates a worker's session, cwd and state.
WORKER_MODE_RUNTIMES = ("claude",)


class SpawnRefused(Exception):
    """A precondition failed. Nothing was created."""


def _run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def per_instance_sentinel_supported(repo, runner=_run) -> bool:
    """Does this checkout give each watcher its OWN sentinel?

    On a single-sentinel checkout the Nth watcher erases the (N-1)th stamp, so
    spawning corrupts the core's own liveness signal rather than the worker's.

    Ask the resolver what it RESOLVES, not what it is spelled: run the repo's
    own `util_paths.py watcher-sentinel` under two instance identities and
    require two different paths. A checkout that returns one shared sentinel
    answers the same string twice, however its function is named. Out of
    process, so a `util_paths` already imported from another checkout cannot
    answer for this one. Fail closed: refuse rather than half-create.
    """
    script = Path(repo) / "src" / "util_paths.py"
    if not script.is_file():
        return False
    seen = set()
    # A throwaway directory, not a state tree: the resolver joins onto whatever
    # it is handed, and the answer under test is the NAME, not the location.
    with tempfile.TemporaryDirectory() as scratch:
        for probe in ("sentinel-probe-a", "sentinel-probe-b"):
            env = {**os.environ, "SUTANDO_INSTANCE_ID": probe}
            r = runner([sys.executable, str(script), "watcher-sentinel",
                        scratch], env=env)
            out = (r.stdout or "").strip()
            if r.returncode != 0 or not out:
                return False
            seen.add(out)
    return len(seen) == 2


def session_exists(name: str, socket=None, runner=_run) -> bool:
    """`=name` is exact: without it tmux prefix-matches and a short id resolves
    to a different worker."""
    socket = socket or default_socket()
    return runner(["tmux", "-S", socket, "has-session", "-t", f"={name}"]).returncode == 0


def core_runtime(repo, runner=_run) -> str:
    """The runtime the core is configured for; a worker runs the same one."""
    r = runner(["bash", str(Path(repo) / "scripts" / "sutando-config.sh"), "core-runtime"])
    return ((r.stdout or "").strip() if r.returncode == 0 else "") or "claude"


def resolve_runtime(repo, runtime=None, runner=_run) -> str:
    """The one runtime decision a dry-run and a real spawn must share — an
    unsupported value refuses here so both paths refuse identically."""
    runtime = runtime or core_runtime(repo, runner)
    if runtime not in WORKER_MODE_RUNTIMES:
        raise SpawnRefused(
            f"runtime {runtime!r} has no worker mode — its launcher would run "
            f"the canonical-core bootstrap; supported: "
            f"{', '.join(WORKER_MODE_RUNTIMES)}")
    return runtime


def plan(workspace, repo, *, runtime: str = "claude", cwd: str = "",
         socket=None, label: str = "", worker_id=None) -> dict:
    """What a spawn would create. Pure — no side effects, so it is reviewable
    before anything exists and testable without tmux."""
    socket = socket or default_socket()
    worker_id = worker_id or wi.new_worker_id()
    delivery_dir = str(pd.deliveries_dir(workspace, worker_id))
    return {
        "worker_id": worker_id,
        "label": label or worker_id,
        "runtime": runtime,
        "cwd": str(cwd or repo),
        "delivery_dir": delivery_dir,
        "tmux": {"socket": socket, "session_name": wi.tmux_session_name(worker_id)},
        # Absolute, from `repo`: relative, `cwd` would pick which code runs.
        # `--runtime`: unselected, the dispatcher rereads the CORE's config.
        "launcher_argv": ["bash", str(Path(repo) / LAUNCHER), "--runtime", runtime],
        "env": {"SUTANDO_TMUX_SOCKET": socket,
                "SUTANDO_TMUX_SESSION": wi.tmux_session_name(worker_id),
                "SUTANDO_INSTANCE_ID": worker_id,
                "SUTANDO_TASKS_DIR": delivery_dir,
                # The inbox holds sentinels, not task bodies. The reader is TOLD
                # that here; inferring it from the path is the reader deciding.
                "SUTANDO_INBOX_KIND": "deliveries",
                # The watcher infers the workspace from its inbox; a delivery
                # folder would put state/ and results/ under deliveries/.
                "SUTANDO_WORKSPACE_DIR": str(workspace),
                # Named, not derived: every runtime writes its answers where the
                # bridges drain them, which is the workspace's own results/.
                "SUTANDO_RESULTS_DIR": str(pd.results_dir(workspace)),
                "SUTANDO_CLAUDE_WORKING_DIR": str(cwd or repo),
                # The worker's own gate, named by the skill that owns it: the
                # core's `/startup --worker` runs what it is handed, not a path.
                "SUTANDO_WORKER_BOOTSTRAP": str(_SCRIPTS / "worker_bootstrap.py")},
    }


def spawn(workspace, repo, *, runtime=None, cwd: str = "",
          socket=None, label: str = "", runner=_run,
          require_sentinel: bool = True) -> dict:
    """Create the four parts, in an order where a failure leaves less behind.

    Identity first (a record with no process is inert), then the delivery folder,
    then the runtime session via the core's launcher, which starts the watcher
    from inside the agent exactly as the core does. It is last because it is
    the only part that starts reading.
    """
    socket = socket or default_socket()
    runtime = resolve_runtime(repo, runtime, runner)
    if require_sentinel and not per_instance_sentinel_supported(repo, runner):
        raise SpawnRefused(
            "this checkout writes ONE watcher sentinel for every watcher, so a "
            "second watcher would erase the core's stamp — refusing to spawn")

    session_id = str(uuid.uuid4())   # the CLI wants a dashed UUID
    rec = wi.create_worker(workspace, runtime=runtime, cwd=str(cwd or repo),
                           host=os.uname().nodename, session_id=session_id,
                           tmux_socket=socket)
    p = plan(workspace, repo, runtime=runtime, cwd=cwd, socket=socket,
             label=label, worker_id=rec["worker_id"])
    name = p["tmux"]["session_name"]
    if session_exists(name, socket, runner):
        raise SpawnRefused(f"tmux session {name!r} already exists")
    Path(p["delivery_dir"]).mkdir(parents=True, exist_ok=True)

    env = {**os.environ, **p["env"], "SUTANDO_CLAUDE_SESSION_ID": session_id}
    r = runner(p["launcher_argv"], env=env)
    if r.returncode != 0:
        # Roll back only what this call minted — the worker_id is freshly
        # generated, so nothing else can be pointing at either path yet.
        shutil.rmtree(wi.worker_dir(workspace, rec["worker_id"]), ignore_errors=True)
        shutil.rmtree(p["delivery_dir"], ignore_errors=True)
        raise SpawnRefused(f"the runtime launcher failed: {(r.stderr or '').strip()}")

    return {**p, **rec, "runtime_session_id": session_id, "started": True}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="create a worker")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--repo", default=str(_REPO))
    ap.add_argument("--folder", default="", help="the worker's working directory")
    ap.add_argument("--label", default="")
    ap.add_argument("--runtime", default="", help="default: the core's configured runtime")
    ap.add_argument("--socket", default="")
    ap.add_argument("--new", action="store_true", help="fresh session (default)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        fn = plan if a.dry_run else spawn
        print(json.dumps(fn(a.workspace, a.repo, runtime=a.runtime or None, cwd=a.folder,
                            socket=a.socket or None, label=a.label), indent=2))
    except SpawnRefused as e:
        print(f"spawn-worker refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
