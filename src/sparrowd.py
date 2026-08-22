#!/usr/bin/env python3
"""sparrowd launcher — the adapter edge that names concrete workers.

The package shell (ag2_sparrow.sparrowd) is deliberately blind to what it
supervises; THIS file owns the worker list and resolved paths, so the core
never imports or locates a repo-specific loop.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

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


def main() -> int:
    state_dir = resolve_workspace() / "state" / "sparrowd"
    return run(worker_specs(), state_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
