#!/usr/bin/env python3
"""The client merge must not restore a body the server explicitly cleared.

`agent-api` emits `result: ""` for a task whose live body is mid-write; a
`row.result || existing.result` merge treats that as absent and re-renders the
superseded answer, with reply controls, while `/result` says pending.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "src" / "web-client.ts").read_text()


def _merge_source() -> str:
    marker = "function mergeTaskRow"
    assert marker in SOURCE, "web-client has no mergeTaskRow"
    start = SOURCE.index(marker)
    end = SOURCE.index("\n}", start) + 2
    return SOURCE[start:end]


def _probe(existing: dict, row: dict) -> dict:
    # Run the EXACT browser source; stubs cover only its collapse bookkeeping.
    probe = r"""
const manuallyCollapsedTaskWorkstreams = new Set();
const collapsedTaskWorkstreams = new Set();
function persistTaskWorkstreamDisplayState() {}
function taskTimeFromRow(row, existing) { return (existing && existing.time) || 0; }
__MERGE__
console.log(JSON.stringify(mergeTaskRow(__EXISTING__, __ROW__)));
""".replace("__MERGE__", _merge_source()) \
   .replace("__EXISTING__", json.dumps(existing)) \
   .replace("__ROW__", json.dumps(row))
    out = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


failures = []


def check(ok: bool, msg: str) -> None:
    print(("ok: " if ok else "FAIL: ") + msg)
    if not ok:
        failures.append(msg)


OLD = {"status": "done", "result": "OLD ANSWER", "text": "t", "source": "chat"}

# the defect: server cleared the body it can no longer vouch for
got = _probe(OLD, {"id": "task-1", "status": "working", "result": ""})
check(got["result"] == "",
      f"working+explicit-empty clears the superseded body, got {got['result']!r}")

# control 1 — a real new body still lands (proves the assert above can fail)
got = _probe(OLD, {"id": "task-1", "status": "done", "result": "NEW ANSWER"})
check(got["result"] == "NEW ANSWER",
      f"completed+new body is kept, got {got['result']!r}")

# control 2 — an ABSENT result key must still preserve, or every partial row
# would wipe the body. This is the case `||` got right and must not regress.
got = _probe(OLD, {"id": "task-1", "status": "working"})
check(got["result"] == "OLD ANSWER",
      f"absent result key preserves the existing body, got {got['result']!r}")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
