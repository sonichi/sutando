#!/usr/bin/env python3
"""Tasks that were handled but never got a result file.

A task file stays in `tasks/` until a result is written and the bridge archives
it, so the queue is already the record of what is unanswered. Nothing reads it
at the END of a pass, though, and the miss is invisible from inside: the agent
answers in its own transcript, the terminal shows the reply, and only the queue
disagrees. Measured five times in one session, caught every time by re-listing
by hand and never by recall.

Exit 1 when a task older than --min-age-sec has no result AND no worker holds
it, 0 otherwise, 2 when the answer cannot be decided. A task the router
delegated is that worker's to answer, so counting it here reports the core as
owing work it must not do.

The third code exists because 1 and "cannot decide" must not share one: an
unreadable deliveries/ says nothing about who holds a task, and reporting that
as 1 is indistinguishable from "the core owes a reply" to the caller that
chains `unanswered-tasks.py && core-status.sh idle`.

`--json` prints the same verdict as one object, with per-task claim lag and the inbox's
holders, for a caller that must tell "busy" from "not reading its inbox". Exit codes match.
"""
from __future__ import annotations

import argparse
import os
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


_POOL_SCRIPTS = _SRC.parent / "skills" / "worker-pool" / "scripts"


def _holder(workspace: Path, task_id: str) -> str | None:
    """Delegated to the worker-pool skill's worker_delivery.py. The skill is
    optional: without it there is no router, so nobody can hold a task."""
    try:
        os.stat(_POOL_SCRIPTS / "worker_delivery.py")
    except FileNotFoundError:
        return None
    # Any other stat failure propagates: an unreadable skill is not an absent one.
    sys.path.insert(0, str(_POOL_SCRIPTS))
    from worker_delivery import holder_of  # noqa: E402
    return holder_of(workspace, task_id)


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
        holder = _holder(workspace, f.stem)
        if holder is not None:
            # Not the core's to report: the router made it that worker's. Said
            # on stderr so a genuinely stuck holder stays visible to a human.
            print(f"unanswered-tasks: {f.stem} held by {holder} ({age/60:.0f}m) — not the core's to answer",
                  file=sys.stderr)
            continue
        reason = _unanswered_reason(results, f.stem, tasks)
        if reason is not None:
            out.append((f.stem, age, reason))
    return out


_CLAIMED_PHASES = frozenset({"RUNNING", "WAITING", "COMPLETED", "FAILED", "CANCELLED"})


def _phase(workspace: Path, task_id: str) -> str | None:
    """The activity snapshot's phase, or None when there is no readable snapshot."""
    import json
    try:
        d = json.loads((workspace / "state" / "activity" / f"{task_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    phase = d.get("phase") if isinstance(d, dict) else None
    return phase if isinstance(phase, str) else None


def _inbox_view(inbox: Path, holders=None) -> dict:
    """Who holds the inbox, classified by status_ack_policy.inbox_state so probe and policy agree."""
    import status_ack_policy
    if holders is None:
        import watcher_identity
        holders = watcher_identity.inbox_holders(str(inbox))
    observed, pairs, undecided = holders.observed, list(holders.holders), holders.undecided
    state = status_ack_policy.inbox_state(observed, [role for _, role in pairs], undecided)
    return {"path": str(inbox), "observed": observed, "undecided": undecided, "state": state,
            "holders": [{"pid": pid, "role": role} for pid, role in pairs],
            "standby_only": None if state == "unknown" else state == "standby_only"}


def snapshot(workspace: Path, min_age_sec: float, now: float | None = None,
             inbox: Path | None = None, holders=None) -> dict:
    """The machine-readable probe: per task, and overall, how long the oldest unclaimed task
    has waited (from its immutable intake anchor) and whether only a standby holds the inbox."""
    import status_ack_policy
    from local_task_protocol import parse_task_headers_lenient, valid_archive_lookup_id
    from task_archive import task_id_for
    _markers()  # as in text mode: a broken grammar owner must not read as an empty queue
    now = time.time() if now is None else now
    tasks, results = workspace / "tasks", workspace / "results"
    out = []
    for f in sorted(tasks.glob("task-*.txt")) if tasks.is_dir() else []:
        task_id = task_id_for(f, accept=valid_archive_lookup_id)
        if task_id is None:
            continue
        mtime = f.stat().st_mtime
        headers = parse_task_headers_lenient(f.read_text(errors="replace")).headers
        intake = status_ack_policy.intake_at(task_id, headers)
        phase, holder = _phase(workspace, task_id), _holder(workspace, task_id)
        reason = _unanswered_reason(results, task_id, tasks)
        claimed = holder is not None or phase in _CLAIMED_PHASES
        age = None if intake is None else max(0.0, now - intake)
        pending = not (claimed or reason is None)
        flagged = reason if holder is None and now - mtime >= min_age_sec else None
        out.append({"task_id": task_id, "file": f.name, "intake_at": intake, "intake_known": intake is not None,
                    "age_s": age, "mtime_age_s": now - mtime, "phase": phase, "holder": holder,
                    "answered": reason is None, "claimed": claimed,
                    "unclaimed_age_s": age if pending else None, "unanswered": flagged})
    waiting = [t for t in out if t["unclaimed_age_s"] is not None]
    unknown = [t["task_id"] for t in out if not t["intake_known"] and not (t["claimed"] or t["answered"])]
    oldest = max(waiting, key=lambda t: t["unclaimed_age_s"]) if waiting else None
    view = _inbox_view(inbox or tasks, holders)
    return {"now": now, "min_age_sec": min_age_sec, "tasks": out,
            "unanswered_count": sum(1 for t in out if t["unanswered"]),
            "oldest_unclaimed_age_s": oldest["unclaimed_age_s"] if oldest else None,
            "oldest_unclaimed_task": oldest["task_id"] if oldest else None,
            "unclaimed_intake_unknown": unknown,
            "inbox": view, "standby_only": view["standby_only"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--min-age-sec", type=float, default=120.0,
                    help="ignore tasks younger than this (default 120)")
    ap.add_argument("--json", action="store_true",
                    help="print one JSON object (per-task and overall claim lag, inbox holders); same exit codes")
    ap.add_argument("--inbox", default=None, help="with --json: the watched inbox (default <workspace>/tasks)")
    a = ap.parse_args()
    if a.json:
        import json
        try:
            snap = snapshot(Path(a.workspace), a.min_age_sec, inbox=Path(a.inbox) if a.inbox else None)
        except ImportError as exc:
            print(json.dumps({"error": f"cannot import the probe's policy owner ({exc})"}))
            return 2
        except SystemExit as exc:
            if exc.code != 2:
                raise
            print(json.dumps({"error": "cannot import src/result_markers.py; refusing to answer"}))
            return 2
        except OSError as exc:
            print(json.dumps({"error": f"cannot read deliveries/ ({exc})"}))
            return 2
        print(json.dumps(snap, sort_keys=True))
        return 1 if snap["unanswered_count"] else 0
    try:
        rows = unanswered(Path(a.workspace), a.min_age_sec)
    except OSError as exc:
        # holder_of propagates rather than reading an unreadable deliveries/ as
        # "nobody holds it"; the CLI turns that into its own cannot-answer code.
        print(f"unanswered-tasks: cannot read deliveries/ ({exc}) — "
              "cannot decide who holds these tasks", file=sys.stderr)
        return 2
    if not rows:
        print("unanswered-tasks: none")
        return 0
    for task_id, age, reason in rows:
        print(f"UNANSWERED {task_id} ({age / 60:.1f}m old) — {reason}; the room heard nothing")
    return 1


if __name__ == "__main__":
    sys.exit(main())
