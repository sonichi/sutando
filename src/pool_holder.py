#!/usr/bin/env python3
"""Which worker-pool worker holds a queued task, with the pool skill optional.

A task in tasks/ that a router handed to a pool worker stays in tasks/ while a
sentinel under <workspace>/deliveries/<worker>/ names its holder. Every reader
that asks "would the core answer this task?" (scripts/unanswered-tasks.py,
scripts/cron-gate.sh) needs the same answer, so the lookup lives here once:
it delegates to the worker-pool skill's `holder_of`, and an absent skill means
no router, so nobody holds anything. Unreadable is not absent: an OSError from
the skill propagates, because reading it as "nobody" hands a worker's task to
the core.

    python3 src/pool_holder.py <workspace> <task-file-or-id> [--pool-scripts DIR]
      prints the holder and exits 0; exits 1 when no worker holds it, 3 when
      deliveries/ cannot be read (held: undecidable, not absent).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

_SRC = Path(__file__).resolve().parent
DEFAULT_POOL_SCRIPTS = _SRC.parent / "skills" / "worker-pool" / "scripts"


def pool_holder(workspace, task_id: str, pool_scripts=None) -> Optional[str]:
    """The worker id holding `task_id`, or None (no skill installed, or no sentinel)."""
    scripts = Path(pool_scripts) if pool_scripts is not None else DEFAULT_POOL_SCRIPTS
    try:
        os.stat(scripts / "worker_delivery.py")
    except FileNotFoundError:
        return None
    # Any other stat failure propagates: an unreadable skill is not an absent one.
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from worker_delivery import holder_of  # noqa: E402
    return holder_of(Path(workspace), task_id)


def _task_id(arg: str) -> str:
    name = Path(arg).name
    return name[:-4] if name.endswith(".txt") else name


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("workspace")
    ap.add_argument("task", help="task file path or task id")
    ap.add_argument("--pool-scripts", help="worker-pool skill scripts dir (default: the repo's)")
    a = ap.parse_args(argv)
    try:
        held = pool_holder(a.workspace, _task_id(a.task), a.pool_scripts)
    except OSError as exc:
        print(f"pool-holder: cannot read deliveries/ ({exc})", file=sys.stderr)
        return 3
    if held is None:
        return 1
    print(held)
    return 0


if __name__ == "__main__":
    sys.exit(main())
