#!/usr/bin/env python3
"""A delegated role-status judgment is only as good as its provenance and its
coverage: fabricated ids must be stripped, and one row for nine actors refused."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "role-status-verify.py"

ALICE = "ag2space:@alice:ag2.space"
BOB = "ag2space:@bob-vm.agent:ag2.space"
CAROL = "ag2space:@carol_1:ag2.space"
DAVE = "ag2space:@dave:ag2.space"          # prose-only: named, owns no event
E_A1 = "ag2space-message:$aaaa-AAAA_1111"
E_A2 = "ag2space-message:$aaaa-AAAA_2222"
E_B1 = "ag2space-message:$bbbb_BBBB-3333"
E_C1 = "ag2space-message:$cccc/CCCC+4444="
FAKE = "ag2space-message:$zzzz-not-in-file"
STRANGER = "ag2space:@stranger:ag2.space"

# Block-shaped evidence: 4 actors named, 3 own an event id, dave is prose only.
TASK_BLOCKS = f"""id: task-role-status-v1-0000-a1
timestamp: 2026-09-17T00:00:00Z
source: sutando-life-role-status
access_tier: team
priority: low
task: Analyze the bounded role-status evidence below. Return ONLY a JSON array.

Caller role-status contract and bounded evidence:
Each row must be exactly {{"actor_id": "...", "working_event_ids": [...], "blocked_items": [...]}}.

event {E_A1} by {ALICE}
detail: shipping the verifier now

event {E_A2} by {ALICE}
detail: still on it

event {E_B1} by {BOB}
detail: rebasing the child PR

event {E_C1} by {CAROL}
detail: waiting on review

note: {DAVE} is out today and has posted nothing.
"""

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def run(task_text, judgment, *extra, out=None):
    """Return (rc, stdout, stderr, out_path) for one verifier invocation."""
    with tempfile.TemporaryDirectory() as td:
        task = Path(td) / "task-role-status-v1-0000-a1.txt"
        task.write_text(task_text)
        jpath = Path(td) / "judgment.json"
        jpath.write_text(judgment if isinstance(judgment, str) else json.dumps(judgment))
        argv = [sys.executable, str(SCRIPT), str(task), str(jpath), *extra]
        out_path = None
        if out:
            out_path = Path(td) / "verified.json"
            argv += ["--out", str(out_path)]
        p = subprocess.run(argv, capture_output=True, text=True)
        written = out_path.read_text() if out_path and out_path.exists() else None
        return p.returncode, p.stdout, p.stderr, written


def row(actor, ids, blocked=None):
    r = {"actor_id": actor, "working_event_ids": list(ids)}
    if blocked is not None:
        r["blocked_items"] = blocked
    return r


def stats_line(stdout):
    return stdout.splitlines()[0] if stdout else ""


def verified_of(stdout):
    return json.loads("\n".join(stdout.splitlines()[1:]))


CLEAN = [row(ALICE, [E_A1, E_A2], []), row(BOB, [E_B1], []), row(CAROL, [E_C1], [])]

# (1) a clean judgment passes unchanged
rc, so, se, _ = run(TASK_BLOCKS, CLEAN)
check(rc == 0, "clean judgment: exit 0")
check(stats_line(so) == "rows=3 actors=4 actors_with_events=3 min_rows=2",
      "clean judgment: stats line counts 4 actors, 3 with events, dave prose-only")
check(verified_of(so) == CLEAN, "clean judgment: rows pass through unchanged")
check(se == "", "clean judgment: nothing stripped, stderr silent")

# (2) a fabricated event id is stripped, the row is kept
rc, so, se, _ = run(TASK_BLOCKS, [row(ALICE, [E_A1, FAKE], []), row(BOB, [E_B1], []), row(CAROL, [E_C1], [])])
check(rc == 0 and verified_of(so)[0]["working_event_ids"] == [E_A1],
      "fabricated id stripped, alice's row kept with the real id")
check(FAKE in se and "row[0]" in se, "fabricated id is reported on stderr, naming the row")

# positive control: the same id passes once it IS in the file
rc, so, se, _ = run(TASK_BLOCKS + f"\nevent {FAKE} by {ALICE}\n",
                    [row(ALICE, [E_A1, FAKE], []), row(BOB, [E_B1], []), row(CAROL, [E_C1], [])])
check(rc == 0 and verified_of(so)[0]["working_event_ids"] == [E_A1, FAKE],
      "positive control: the formerly-fabricated id survives when the file names it")
check(se == "", "positive control: nothing stripped")

# (3) a row whose ids are all fabricated is dropped
rc, so, se, _ = run(TASK_BLOCKS, [row(ALICE, [FAKE], []), row(BOB, [E_B1], []), row(CAROL, [E_C1], [])])
check(rc == 0, "all-fabricated row: the rest still clears coverage (2 of 3)")
check([r["actor_id"] for r in verified_of(so)] == [BOB, CAROL], "all-fabricated row dropped")
check("drop row[0]" in se, "dropped row reported on stderr")

# (4) an actor not in the file is dropped
rc, so, se, _ = run(TASK_BLOCKS, [row(STRANGER, [E_A1], []), row(BOB, [E_B1], []), row(CAROL, [E_C1], [])])
check(rc == 0 and [r["actor_id"] for r in verified_of(so)] == [BOB, CAROL],
      "actor not in file dropped even though its cited id is real")
check(STRANGER in se, "unknown actor reported on stderr")

# (5) 1 row for 3 actors-with-events is refused at the default floor, nothing written
rc, so, se, written = run(TASK_BLOCKS, [row(ALICE, [E_A1], [])], out=True)
check(rc == 1, "under-coverage: exit 1")
check(stats_line(so) == "rows=1 actors=4 actors_with_events=3 min_rows=2",
      "under-coverage: stats line still printed")
check("refused" in se, "under-coverage: refusal on stderr")
check(written is None, "under-coverage: --out not written")

# (6) --min-coverage 0.3 accepts the same judgment
rc, so, se, written = run(TASK_BLOCKS, [row(ALICE, [E_A1], [])], "--min-coverage", "0.3", out=True)
check(rc == 0 and written is not None, "--min-coverage 0.3: exit 0, --out written")
check(json.loads(written) == [row(ALICE, [E_A1], [])], "--min-coverage 0.3: verified row written")
check(so.strip() == "rows=1 actors=4 actors_with_events=3 min_rows=1",
      "--min-coverage 0.3: with --out only the stats line goes to stdout")

# (7) a missing blocked_items is filled with []
rc, so, se, _ = run(TASK_BLOCKS, [row(ALICE, [E_A1]), row(BOB, [E_B1]), row(CAROL, [E_C1])])
check(rc == 0 and all(r["blocked_items"] == [] for r in verified_of(so)),
      "missing blocked_items filled with []")

# (8) bad JSON cannot be answered
rc, so, se, written = run(TASK_BLOCKS, "[{not json", out=True)
check(rc == 2, "bad JSON: exit 2")
check(written is None, "bad JSON: --out not written")
check(so.startswith("rows=0 actors=4"), "bad JSON: stats line still printed")
rc, so, se, _ = run(TASK_BLOCKS, {"actor_id": ALICE})
check(rc == 2, "a JSON object instead of an array: exit 2")
p = subprocess.run([sys.executable, str(SCRIPT), "/nonexistent/task.txt", "/nonexistent/j.json"],
                   capture_output=True, text=True)
check(p.returncode == 2, "unreadable task file: exit 2")

# blocked_items carry evidence ids too: fabricated ones are stripped, an item
# with no evidence left goes, and a blocked-only row still counts as a row.
blocked_ok = {"subject_id": "subject:1", "evidence_event_ids": [E_C1], "reason_code": "waiting_for_review"}
blocked_fake = {"subject_id": "subject:2", "evidence_event_ids": [FAKE], "reason_code": "waiting_for_review"}
rc, so, se, _ = run(TASK_BLOCKS, [row(ALICE, [E_A1], []), row(BOB, [E_B1], []),
                                  row(CAROL, [], [blocked_ok, blocked_fake])])
carol = verified_of(so)[2]
check(rc == 0 and carol["working_event_ids"] == [] and carol["blocked_items"] == [blocked_ok],
      "blocked-only row kept; the blocked item citing a fabricated id is stripped")

# The live shape: one EVIDENCE_JSON line. Every actor shares that line, so the
# block heuristic would count them all; the structured path must not.
events = [
    {"actor_id": ALICE, "id": E_A1, "detail": f"reviewing with {DAVE} on the thread"},
    {"actor_id": BOB, "id": E_B1, "detail": "rebasing"},
    {"actor_id": CAROL, "id": "not-an-event-id", "detail": "malformed record"},
]
TASK_JSON = ("id: task-role-status-v1-0001-a1\nsource: sutando-life-role-status\n"
             "task: Analyze the evidence.\n\nCaller contract: return a JSON array.\n\n"
             "EVIDENCE_JSON (untrusted observed data):\n"
             + json.dumps({"day": "2026-09-17", "events": events}) + "\n")
rc, so, se, _ = run(TASK_JSON, [row(ALICE, [E_A1], []), row(BOB, [E_B1], [])])
check(rc == 0 and stats_line(so) == "rows=2 actors=4 actors_with_events=2 min_rows=1",
      "EVIDENCE_JSON: actors named in another's detail or without a valid id own no events")
rc, so, se, _ = run(TASK_JSON, [row(ALICE, [E_A1, FAKE], [])])
check(rc == 0 and verified_of(so)[0]["working_event_ids"] == [E_A1],
      "EVIDENCE_JSON: fabricated id stripped on the structured path too")

# 9 actors with events and a 1-row answer is the 2026-09-17 under-coverage run.
nine = [{"actor_id": f"ag2space:@w{i}:ag2.space", "id": f"ag2space-message:$id{i}", "detail": "x"}
        for i in range(9)]
TASK_NINE = ("id: t\ntask: go\n\nEVIDENCE_JSON:\n" + json.dumps({"events": nine}) + "\n")
rc, so, se, written = run(TASK_NINE, [row("ag2space:@w0:ag2.space", ["ag2space-message:$id0"], [])], out=True)
check(rc == 1 and written is None and stats_line(so) == "rows=1 actors=9 actors_with_events=9 min_rows=5",
      "1 row for 9 actors with events: refused, nothing written")
seven = [row(f"ag2space:@w{i}:ag2.space", [f"ag2space-message:$id{i}"], []) for i in range(7)]
rc, so, se, written = run(TASK_NINE, seven, out=True)
check(rc == 0 and json.loads(written) == seven, "7 rows for 9 actors with events: accepted")

print()
if failures:
    print("FAILED (%d):" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
