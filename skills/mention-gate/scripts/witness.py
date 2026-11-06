#!/usr/bin/env python3
"""mention-gate live witness — three-case post-restart check against the LIVE bridge.

Run each case on the host where the restarted discord-bridge is serving. The
operator sends the test message from a NON-owner Discord account; this script
only observes the workspace (tasks/ + audit log) and prints a PASS/FAIL verdict.
It never toggles the gate itself — use the mention-gate CLI between cases.

    case1  gate OFF: owner-tagged msg in a requireMention channel -> NOT ingested
    case2  gate ON:  same message from an AUTHORIZED sender        -> task + 1 audit row
    case3  gate ON:  same message from an UNAUTHORIZED sender      -> no task, no audit

Usage (marker = any unique string you include in the test message):
    python3 skills/mention-gate/scripts/witness.py case1 --marker "witness-$(date +%s)"
    python3 skills/mention-gate/scripts/witness.py case2 --marker ...
    python3 skills/mention-gate/scripts/witness.py case3 --marker ...

Exit 0 = case PASSED, 1 = FAILED, 2 = precondition not met (wrong gate state).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from workspace_default import resolve_workspace  # noqa: E402
import mention_gate  # noqa: E402

CASES = {
    "case1": {"gate_on": False, "expect_task": False, "expect_audit": 0,
              "what": "gate OFF rejects an owner-tagged message"},
    "case2": {"gate_on": True, "expect_task": True, "expect_audit": 1,
              "what": "gate ON admits an authorized owner-tagged message"},
    "case3": {"gate_on": True, "expect_task": False, "expect_audit": 0,
              "what": "gate ON + unauthorized sender leaves neither task nor audit"},
}


def _tasks_with_marker(workspace: Path, marker: str) -> list:
    """Task files (live, processed, or archived) whose body carries the marker."""
    hits = []
    for pattern in ("task-*.txt", "processed/task-*.txt", "archive/*/task-*.txt"):
        for p in (workspace / "tasks").glob(pattern):
            try:
                if marker in p.read_text(encoding="utf-8", errors="replace"):
                    hits.append(p)
            except OSError:
                continue
    return hits


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mention-gate witness", description=__doc__)
    parser.add_argument("case", choices=sorted(CASES))
    parser.add_argument("--marker", required=True,
                        help="unique string the operator includes in the test message")
    parser.add_argument("--timeout", type=int, default=90,
                        help="seconds to watch for ingestion after Enter (default 90)")
    args = parser.parse_args(argv)
    spec = CASES[args.case]

    workspace = resolve_workspace()
    gate_on = mention_gate.owner_tag_triggers_ingest(workspace)
    if gate_on != spec["gate_on"]:
        want = "ON" if spec["gate_on"] else "OFF"
        print(f"PRECONDITION FAILED: {args.case} needs the gate {want}; it reads "
              f"{'ON' if gate_on else 'OFF'}. Toggle via the mention-gate CLI and rerun.")
        return 2

    audit_before = mention_gate.gated_ingest_count(workspace)
    print(f"{args.case}: {spec['what']}")
    print(f"workspace: {workspace}")
    print(f"audit rows before: {audit_before}")
    print(f"\nNow send the test message (it must @-tag the owner, NOT the bot, and "
          f"contain the marker text verbatim):\n    {args.marker}\n")
    input("Press Enter AFTER the message is sent...")

    deadline = time.time() + args.timeout
    hits = []
    while time.time() < deadline:
        hits = _tasks_with_marker(workspace, args.marker)
        if hits and spec["expect_task"]:
            break  # found what we were waiting for; negatives wait out the window
        time.sleep(2)

    audit_delta = mention_gate.gated_ingest_count(workspace) - audit_before
    task_ok = bool(hits) == spec["expect_task"]
    audit_ok = audit_delta == spec["expect_audit"]
    print(f"\ntask files with marker: {[str(p) for p in hits] or 'none'}")
    print(f"audit delta: {audit_delta} (expected {spec['expect_audit']})")
    if task_ok and audit_ok:
        print(f"{args.case}: PASS")
        return 0
    print(f"{args.case}: FAIL — "
          f"{'task presence wrong' if not task_ok else ''}"
          f"{' and ' if not task_ok and not audit_ok else ''}"
          f"{'audit delta wrong' if not audit_ok else ''}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
