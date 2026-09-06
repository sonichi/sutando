#!/usr/bin/env python3
"""PostToolUse hook — structurally enforce the owner's task-ID requirement AND
keep a per-day completion history so "how many tasks did I finish each day?" is
answerable at any time (see scripts/task-completions.py).

The owner requires every task reply to carry a `[task YYYYMMDD-NNN]` ID (daily
counter, resets each day). Relying on the agent to remember to prepend it failed
(memory had the rule; the agent still lapsed across a busy session). This hook
removes the reliance on memory: after any tool runs, it scans the live `results/`
dir and, for any `task-*.txt` whose body does NOT already start with a
`[task YYYYMMDD-NNN]` marker, it hands the file to the shared stamping
transaction, which allocates the next counter ID and prepends it. So a reply the
agent wrote without an ID still gets one before the bridge delivers it.

This hook and the delivery path are two writers of the same stamp on the same
files, so the stamp must be one locked transaction owned by `result_ready`; this
module must never allocate an ID and write it separately.

The daily counter (`state/task-counter.json`) resets every day, so on its own it
only knows *today's* count — yesterday's total is overwritten. To make the
per-day history durable, every allocation also upserts today's running total
into `state/task-completions-daily.json` ({"YYYYMMDD": count}). Past days are
never touched; only today's entry advances. That file is the source of truth for
the completions report.

Idempotent (skips already-stamped files) and fail-open (never blocks a tool).
"""
import datetime
import fcntl
import glob
import json
import re
import sys
import time
from pathlib import Path

# Only stamp what the current turn wrote (mtime within this window): a backlog of
# stale results would each mint an ID, so NNN would count files, not tasks done.
_FRESH_S = 45

# The sanctioned resolver owns all fallback/override logic; never reconstruct a
# workspace path inline here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workspace_default import resolve_workspace  # noqa: E402
from delivery.readiness import needs_task_stamp, stamp_result_file  # noqa: E402

WS = Path(resolve_workspace())

COUNTER = WS / "state" / "task-counter.json"
HISTORY = WS / "state" / "task-completions-daily.json"
RESULTS = WS / "results"





def main() -> None:
    try:
        now = time.time()
        for f in glob.glob(str(RESULTS / "task-*.txt")):
            p = Path(f)
            try:
                if now - p.stat().st_mtime > _FRESH_S:
                    continue  # stale/backlog file — not something this turn wrote
                body = p.read_text()
            except Exception:
                continue
            if not body.strip():
                continue  # empty/placeholder — leave it
            if not needs_task_stamp(p.name, body):
                continue  # cheap pre-filter only; the binding re-check is under the lock
            # Delivery stamps these too; allocating outside the lock double-mints.
            stamp_result_file(p)
    except Exception:
        pass
    sys.exit(0)  # fail-open: a stamping error must never block the tool


if __name__ == "__main__":
    main()
