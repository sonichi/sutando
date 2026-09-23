#!/usr/bin/env python3
"""sparrowd launcher — the adapter edge that names concrete workers.

The package shell (ag2_sparrow.sparrowd) is deliberately blind to what it
supervises; THIS file owns the worker list and resolved paths, so the core
never imports or locates a repo-specific loop.
"""
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
REPO = _SRC.parent
for _p in (str(_SRC), str(REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from workspace_default import resolve_workspace  # noqa: E402
from ag2_sparrow.sparrowd import WorkerSpec, run  # noqa: E402


def _presence_daemon_spec():
    """The room-collab presence daemon, when this install has the skill AND has
    told it which interpreter to use.

    Both halves are required and neither is guessed. The script is optional —
    a core with no room-collab skill must still boot — and it imports pycrdt
    and websockets, which the core's own interpreter is not required to have
    (the skill documents its own venv). An unconfigured interpreter is a
    skipped worker with a reason, never `sys.executable` hoping for the best:
    started under the wrong python it would crash-loop under the supervisor.
    """
    import json

    script = REPO / "skills" / "room-collab" / "scripts" / "presence_daemon.py"
    manifest = REPO / "skills" / "room-collab" / "manifest.json"
    if not script.is_file():
        return None, "room-collab is not installed"
    py = os.environ.get("ROOM_COLLAB_PYTHON") or ""
    if not py and manifest.is_file():
        try:
            py = (json.loads(manifest.read_text(encoding="utf-8"))
                  .get("config", {}).get("ROOM_COLLAB_PYTHON") or "")
        except (OSError, ValueError):
            py = ""
    if not py:
        return None, ("no interpreter configured: set ROOM_COLLAB_PYTHON in "
                      "skills/room-collab/manifest.json (it needs pycrdt + websockets)")
    if not Path(py).is_file():
        return None, f"configured interpreter does not exist: {py}"
    return WorkerSpec(name="room-collab-presence", argv=[py, str(script)],
                      cwd=str(REPO)), None


def worker_specs() -> list:
    specs = [
        WorkerSpec(
            name="remote-gateway-bridge",
            argv=[sys.executable, str(REPO / "src" / "remote-gateway-bridge.py")],
            cwd=str(REPO),
        ),
    ]
    spec, why = _presence_daemon_spec()
    if spec is not None:
        specs.append(spec)
    else:
        print(f"sparrowd: room-collab-presence not supervised — {why}", file=sys.stderr)
    return specs


def external_supervisor(marker: str) -> "str | None":
    """A live process already running the worker script means another
    supervisor (e.g. an app bundle's keepalive) owns it — dual supervision
    degrades to an eviction/reap loop, so sparrowd must refuse, not race."""
    import os
    import subprocess
    out = subprocess.run(["pgrep", "-f", marker],
                         capture_output=True, text=True)
    pids = [p for p in out.stdout.split()
            if p.isdigit() and int(p) != os.getpid()]
    if not pids:
        return None
    lines = []
    for pid in pids:
        ps = subprocess.run(["ps", "-o", "ppid=,command=", "-p", pid],
                            capture_output=True, text=True).stdout.strip()
        lines.append(f"pid {pid} ({ps or 'gone'})")
    return "; ".join(lines)


def main() -> int:
    for spec in worker_specs():
        owned = external_supervisor(Path(spec.argv[-1]).name)
        if owned:
            print(f"sparrowd: refusing to start — {spec.name} already "
                  f"supervised outside sparrowd: {owned}. Stop that "
                  f"supervisor (e.g. the app's gateway-keepalive) first.",
                  file=sys.stderr)
            return 2
    state_dir = resolve_workspace() / "state" / "sparrowd"
    return run(worker_specs(), state_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
