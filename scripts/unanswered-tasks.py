#!/usr/bin/env python3
"""Tasks that were handled but never got a result file.

A task file stays in `tasks/` until a result is written and the bridge archives
it, so the queue is already the record of what is unanswered. Nothing reads it
at the END of a pass, though, and the miss is invisible from inside: the agent
answers in its own transcript, the terminal shows the reply, and only the queue
disagrees. Measured five times in one session, caught every time by re-listing
by hand and never by recall.

Exit 1 when a task older than --min-age-sec has no result, 0 otherwise.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


_SRC = Path(__file__).resolve().parent.parent / "src"  # lint-workspace-resolution: allow-repo-root (sys.path only; the root comes from the CLI arg)
sys.path.insert(0, str(_SRC))
try:
    import dedup_soundness
except ImportError as exc:  # pragma: no cover - exercised by the delegation test
    print(f"unanswered-tasks: cannot import src/dedup_soundness.py ({exc}) — "
          "refusing to re-implement the dedup judgement", file=sys.stderr)
    raise SystemExit(2) from exc

_result_path = dedup_soundness.result_path
_read = dedup_soundness.read


def _markers():
    """Resolve the grammar owner up front so an empty queue cannot skip the guard.
    Kept as a SystemExit(2) here because the CLI contract is 'could not answer'."""
    try:
        return dedup_soundness.markers(_SRC)
    except ImportError as exc:
        print(f"unanswered-tasks: cannot import src/result_markers.py ({exc}) — "
              "refusing to re-implement the marker grammar", file=sys.stderr)
        raise SystemExit(2) from exc


def _unanswered_reason(results: Path, task_id: str, tasks: Path | None = None) -> str | None:
    """None when the room heard something; else why it did not.

    The whole judgement belongs to `src/dedup_soundness.py`, shared with the
    PRE-write guard. Both asked the same question and answered it differently,
    and the guard was the weaker copy — which is the dangerous direction, since
    it cleared writes this check would later condemn.
    """
    return dedup_soundness.dedup_problem(results, task_id, tasks, src_dir=_SRC)


def unanswered(workspace: Path, min_age_sec: float, now: float | None = None) -> list[tuple[str, float, str]]:
    _markers()  # resolve up front: an empty queue must not silently skip the guard
    now = time.time() if now is None else now
    tasks, results = workspace / "tasks", workspace / "results"
    out: list[tuple[str, float, str]] = []
    if not tasks.is_dir():
        return out
    for f in sorted(tasks.glob("task-*.txt")):
        age = now - f.stat().st_mtime
        if age < min_age_sec:
            continue  # still plausibly in flight
        reason = _unanswered_reason(results, f.stem, tasks)
        if reason is not None:
            out.append((f.stem, age, reason))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--min-age-sec", type=float, default=120.0,
                    help="ignore tasks younger than this (default 120)")
    a = ap.parse_args()
    rows = unanswered(Path(a.workspace), a.min_age_sec)
    if not rows:
        print("unanswered-tasks: none")
        return 0
    for task_id, age, reason in rows:
        print(f"UNANSWERED {task_id} ({age / 60:.1f}m old) — {reason}; the room heard nothing")
    return 1


if __name__ == "__main__":
    sys.exit(main())
