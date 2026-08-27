#!/usr/bin/env python3
"""sparrowd launcher — the adapter edge that names concrete workers.

The package shell (ag2_sparrow.sparrowd) is deliberately blind to what it
supervises; THIS file owns the worker list and resolved paths, so the core
never imports or locates a repo-specific loop.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
REPO = _SRC.parent
for _p in (str(_SRC), str(REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from workspace_default import resolve_workspace  # noqa: E402
from ag2_sparrow.sparrowd import WorkerSpec, run  # noqa: E402


def worker_specs() -> list:
    return [
        WorkerSpec(
            name="remote-gateway-bridge",
            argv=[sys.executable, str(REPO / "src" / "remote-gateway-bridge.py")],
            cwd=str(REPO),
        ),
    ]


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
