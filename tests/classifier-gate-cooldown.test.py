#!/usr/bin/env python3
"""The classifier gate cools down after a completed run; churn cannot re-queue it.

Every completed owner task shifts the candidate window, so an ungated hash
comparison re-enqueues the grouping classifier on every maintenance tick —
~390 tasks/day measured live (sonichi/sutando#3621). The gate now returns
``cooling-down`` while the last completed run is younger than
``cooldown_seconds``, without paying the archive scan; a stale completion,
an inflight run, and a first-ever run keep their existing behavior.

Run: python3 tests/classifier-gate-cooldown.test.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import task_workstreams as tw  # noqa: E402

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def make_workspace(tmp: Path, name: str) -> Path:
    ws = tmp / name
    (ws / "tasks").mkdir(parents=True)
    (ws / "results").mkdir()
    (ws / "state").mkdir()
    (ws / "state" / "core-status.json").write_text(
        json.dumps({"status": "idle", "ts": time.time()}))
    # One done owner task -> one candidate row, no active-user-task veto.
    tid = "task-1788000000000"
    (ws / "tasks" / f"{tid}.txt").write_text(
        f"id: {tid}\ntimestamp: 2026-08-30T00:00:00Z\nsource: chat\n"
        "access_tier: owner\ntask: sample work item\n")
    (ws / "results" / f"{tid}.txt").write_text("done\n")
    return ws


def write_state(ws: Path, **kw) -> None:
    (ws / "state" / "task-workstream-classifier.json").write_text(json.dumps(kw))


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # --- fresh completion + changed candidates -> cooling-down, no task file
        ws = make_workspace(tmp, "cooling")
        write_state(ws, status="complete", snapshot_hash="aaaa",
                    enqueued_at=time.time() - 60, source_token="stale-token")
        r = tw.maybe_enqueue_classifier_task(ws)
        check(r.reason == "cooling-down",
              f"fresh completion + churn -> cooling-down (got {r.reason})")
        check(r.pending is False and r.enqueued is False,
              "cooling-down is not pending and enqueues nothing")
        check(not list((ws / "tasks").glob(f"{tw.CLASSIFIER_TASK_PREFIX}*")),
              "no classifier task file written while cooling")
        state = json.loads(
            (ws / "state" / "task-workstream-classifier.json").read_text())
        check(state["snapshot_hash"] == "aaaa" and state["source_token"] == "stale-token",
              "state untouched while cooling (no scan, no rewrite)")

        # --- stale completion -> the gate proceeds and re-enqueues
        ws = make_workspace(tmp, "stale")
        write_state(ws, status="complete", snapshot_hash="aaaa",
                    enqueued_at=time.time() - tw.CLASSIFIER_COOLDOWN_SECONDS - 5,
                    source_token="stale-token")
        r = tw.maybe_enqueue_classifier_task(ws)
        check(r.reason == "enqueued",
              f"stale completion + changed candidates -> enqueued (got {r.reason})")
        check(bool(list((ws / "tasks").glob(f"{tw.CLASSIFIER_TASK_PREFIX}*"))),
              "stale path writes the classifier task file")

        # --- cooldown_seconds=0 disables the brake entirely
        ws = make_workspace(tmp, "disabled")
        write_state(ws, status="complete", snapshot_hash="aaaa",
                    enqueued_at=time.time() - 1, source_token="stale-token")
        r = tw.maybe_enqueue_classifier_task(ws, cooldown_seconds=0)
        check(r.reason == "enqueued",
              f"cooldown_seconds=0 -> gate ungated (got {r.reason})")

        # --- a future enqueued_at (clock step) never cools forever
        ws = make_workspace(tmp, "future")
        write_state(ws, status="complete", snapshot_hash="aaaa",
                    enqueued_at=time.time() + 9999, source_token="stale-token")
        r = tw.maybe_enqueue_classifier_task(ws)
        check(r.reason == "enqueued",
              f"future enqueued_at -> not cooling (got {r.reason})")

        # --- inflight state keeps its TTL semantics, cooldown does not apply
        ws = make_workspace(tmp, "inflight")
        st = tw._task_source_state(ws, {}, discover=True)
        write_state(ws, status="inflight", snapshot_hash="aaaa",
                    task_id="task-workstream-grouping-1", enqueued_at=time.time() - 60,
                    source_token=st[0], source_directories=list(st[1]))
        r = tw.classifier_status(ws)
        check(r.reason == "already-queued",
              f"fresh inflight -> already-queued, untouched by cooldown (got {r.reason})")

        # --- first-ever run (no state) proceeds
        ws = make_workspace(tmp, "first")
        r = tw.maybe_enqueue_classifier_task(ws)
        check(r.reason == "enqueued",
              f"no prior state -> enqueued (got {r.reason})")

        # --- busy core still wins over everything
        ws = make_workspace(tmp, "busy")
        (ws / "state" / "core-status.json").write_text(
            json.dumps({"status": "running", "ts": time.time()}))
        write_state(ws, status="complete", snapshot_hash="aaaa",
                    enqueued_at=time.time() - 60, source_token="x")
        r = tw.classifier_status(ws)
        check(r.reason == "core-busy",
              f"busy core short-circuits before cooldown (got {r.reason})")

    print(("FAIL: " + "; ".join(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
