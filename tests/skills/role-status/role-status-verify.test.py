#!/usr/bin/env python3
"""A delegated role-status judgment is only as good as its provenance and its
coverage: fabricated ids must be stripped, an id owned by another actor is
fabricated too, duplicate actor rows do not count, and one row for nine actors
is refused.

The verifier is imported and driven in-process (main(argv) under captured
stdout/stderr) so the coverage gate measures it; one subprocess case keeps the
`python3 skills/role-status/scripts/verify.py` entry point honest."""
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "skills" / "role-status" / "scripts" / "verify.py"

_spec = importlib.util.spec_from_file_location("role_status_verify", SCRIPT)
rsv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rsv)

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
CHECKS = 0


def check(cond, label):
    global CHECKS
    CHECKS += 1
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def run(task_text, judgment, *extra, out=None, subprocess_smoke=False):
    """Return (rc, stdout, stderr, written) for one verifier invocation.

    In-process by default (the coverage gate measures the imported module);
    subprocess_smoke=True runs the real entry point once."""
    with tempfile.TemporaryDirectory() as td:
        task = Path(td) / "task-role-status-v1-0000-a1.txt"
        task.write_text(task_text)
        jpath = Path(td) / "judgment.json"
        jpath.write_text(judgment if isinstance(judgment, str) else json.dumps(judgment))
        argv = [str(task), str(jpath), *extra]
        out_path = None
        if out == "dir":
            # --out naming an existing directory: the atomic replace must fail
            out_path = Path(td) / "verified.json"
            out_path.mkdir()
            argv += ["--out", str(out_path)]
        elif out:
            out_path = Path(td) / "verified.json"
            argv += ["--out", str(out_path)]
        if subprocess_smoke:
            p = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True)
            rc, so, se = p.returncode, p.stdout, p.stderr
        else:
            so_buf, se_buf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
                rc = rsv.main(argv)
            so, se = so_buf.getvalue(), se_buf.getvalue()
        written = out_path.read_text() if out_path and out_path.is_file() else None
        return rc, so, se, written


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
check(stats_line(so) == "rows=3 distinct_actors=3 actors=4 actors_with_events=3 min_rows=2",
      "clean judgment: stats line counts 4 actors, 3 with events, dave prose-only")
check(verified_of(so) == CLEAN, "clean judgment: rows pass through unchanged")
check(se == "", "clean judgment: nothing stripped, stderr silent")

# the same judgment through the real entry point (subprocess smoke)
rc, so, se, _ = run(TASK_BLOCKS, CLEAN, subprocess_smoke=True)
check(rc == 0 and verified_of(so) == CLEAN and se == "",
      "entry point: python3 skills/role-status/scripts/verify.py gives the same answer")

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
check(stats_line(so) == "rows=1 distinct_actors=1 actors=4 actors_with_events=3 min_rows=2",
      "under-coverage: stats line still printed")
check("refused" in se, "under-coverage: refusal on stderr")
check(written is None, "under-coverage: --out not written")

# (6) --min-coverage 0.3 accepts the same judgment
rc, so, se, written = run(TASK_BLOCKS, [row(ALICE, [E_A1], [])], "--min-coverage", "0.3", out=True)
check(rc == 0 and written is not None, "--min-coverage 0.3: exit 0, --out written")
check(json.loads(written) == [row(ALICE, [E_A1], [])], "--min-coverage 0.3: verified row written")
check(so.strip() == "rows=1 distinct_actors=1 actors=4 actors_with_events=3 min_rows=1",
      "--min-coverage 0.3: with --out only the stats line goes to stdout")

# (7) a missing blocked_items is filled with []
rc, so, se, _ = run(TASK_BLOCKS, [row(ALICE, [E_A1]), row(BOB, [E_B1]), row(CAROL, [E_C1])])
check(rc == 0 and all(r["blocked_items"] == [] for r in verified_of(so)),
      "missing blocked_items filled with []")

# (8) bad JSON cannot be answered
rc, so, se, written = run(TASK_BLOCKS, "[{not json", out=True)
check(rc == 2, "bad JSON: exit 2")
check(written is None, "bad JSON: --out not written")
check(so.startswith("rows=0 distinct_actors=0 actors=4"), "bad JSON: stats line still printed")
rc, so, se, _ = run(TASK_BLOCKS, {"actor_id": ALICE})
check(rc == 2, "a JSON object instead of an array: exit 2")
so_buf, se_buf = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
    rc = rsv.main(["/nonexistent/task.txt", "/nonexistent/j.json"])
check(rc == 2 and "task file unreadable" in se_buf.getvalue(), "unreadable task file: exit 2")
rc, so, se, _ = run(TASK_BLOCKS, CLEAN, "--min-coverage", "1.5")
check(rc == 2 and "--min-coverage" in se, "--min-coverage outside [0, 1]: exit 2")
rc, so, se, written = run(TASK_BLOCKS, CLEAN, out="dir")
check(rc == 2 and "--out unwritable" in se and written is None,
      "--out naming a directory: exit 2, nothing written")

# malformed rows and fields are reported, never crash the verifier
rc, so, se, _ = run(TASK_BLOCKS, ["not a row",
                                  {"actor_id": ALICE, "working_event_ids": "not-a-list",
                                   "blocked_items": ["not an item"]},
                                  row(BOB, [E_B1], []), row(CAROL, [E_C1], [])])
check(rc == 0 and [r["actor_id"] for r in verified_of(so)] == [BOB, CAROL],
      "non-object row and a row whose fields are the wrong shape are dropped")
check("drop row[0]: not an object" in se and "blocked_items[0]: not an object" in se,
      "malformed row and blocked item reported on stderr")
# no `task:` header: the file names an event id but carries no evidence that
# owns it -- cannot answer, never an empty publish for a file that has events
rc, so, se, written = run("id: t\nsource: x\n" + f"event {E_A1} by {ALICE}\n", [row(ALICE, [E_A1], [])], out=True)
check(rc == 2 and written is None and "cannot answer: no event ownership resolvable" in se
      and stats_line(so) == "rows=1 distinct_actors=0 actors=0 actors_with_events=0 min_rows=0",
      "no task: header: an event id nobody owns is cannot answer, exit 2, nothing written")
# no event ids at all (nobody working): the empty array on a zero floor is the answer
NO_EVENTS = f"id: t\ntask: go\n\nnote: {ALICE} and {BOB} posted nothing today.\n"
rc, so, se, written = run(NO_EVENTS, [], out=True)
check(rc == 0 and json.loads(written) == [] and se == ""
      and stats_line(so) == "rows=0 distinct_actors=0 actors=2 actors_with_events=0 min_rows=0",
      "no event ids at all: [] on a zero floor, exit 0")
rc, so, se, written = run(NO_EVENTS, [row(ALICE, [FAKE], [])], out=True)
check(rc == 0 and json.loads(written) == [] and FAKE in se and "drop row[0]" in se,
      "no event ids at all: a row citing a fabricated id is dropped, [] still the answer")
# structured events present but none with a valid id: cannot answer as well
rc, so, se, written = run("id: t\ntask: go\n\nEVIDENCE_JSON:\n"
                          + json.dumps({"events": [{"actor_id": ALICE, "id": "not-an-event-id"}]}) + "\n",
                          [], out=True)
check(rc == 2 and written is None and "no event ownership resolvable" in se
      and stats_line(so) == "rows=0 distinct_actors=0 actors=1 actors_with_events=0 min_rows=0",
      "EVIDENCE_JSON with event records but no resolvable id: exit 2, nothing written")
rc, so, se, written = run("id: t\ntask: go\n\nEVIDENCE_JSON:\n" + json.dumps({"events": []}) + "\n", [], out=True)
check(rc == 0 and json.loads(written) == [] and "min_rows=0" in stats_line(so),
      "EVIDENCE_JSON with an empty events list: [] on a zero floor, exit 0")

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
             "{braces in prose are not a JSON object}\n"
             "EVIDENCE_JSON (untrusted observed data):\n"
             + json.dumps({"day": "2026-09-17", "events": events}) + "\n")
rc, so, se, _ = run(TASK_JSON, [row(ALICE, [E_A1], []), row(BOB, [E_B1], [])])
check(rc == 0 and stats_line(so) == "rows=2 distinct_actors=2 actors=4 actors_with_events=2 min_rows=1",
      "EVIDENCE_JSON: actors named in another's detail or without a valid id own no events")
rc, so, se, _ = run(TASK_JSON, [row(ALICE, [E_A1, FAKE], [])])
check(rc == 0 and verified_of(so)[0]["working_event_ids"] == [E_A1],
      "EVIDENCE_JSON: fabricated id stripped on the structured path too")

# A cited id must be OWNED by the row's actor, not merely present in the file:
# alice citing only bob's event is an alice row with no evidence.
NOT_OWNED = f"strip row[0] {ALICE} working_event_ids: {E_B1!r} not owned by actor"
rc, so, se, _ = run(TASK_BLOCKS, [row(ALICE, [E_B1], []), row(BOB, [E_B1], []), row(CAROL, [E_C1], [])])
check(rc == 0 and [r["actor_id"] for r in verified_of(so)] == [BOB, CAROL],
      "alice citing only bob's event: id stripped, alice's row dropped, the rest clears coverage")
check(NOT_OWNED in se and f"drop row[0] {ALICE}: no verifiable event ids left" in se,
      "cross-actor citation names the row, the actor and the id on stderr")
rc, so, se, written = run(TASK_BLOCKS, [row(ALICE, [E_B1], []), row(BOB, [E_B1], [])], out=True)
check(rc == 1 and written is None
      and stats_line(so) == "rows=2 distinct_actors=1 actors=4 actors_with_events=3 min_rows=2",
      "alice citing only bob's event, 1 real row left of 3 actors: refused, nothing written")
# positive control: the same shape passes when alice cites her own event
rc, so, se, written = run(TASK_BLOCKS, [row(ALICE, [E_A1], []), row(BOB, [E_B1], [])], out=True)
check(rc == 0 and json.loads(written) == [row(ALICE, [E_A1], []), row(BOB, [E_B1], [])] and se == "",
      "positive control: alice citing her own event passes, nothing stripped")
# the same binding on the structured path and inside blocked_items
rc, so, se, _ = run(TASK_JSON, [row(ALICE, [E_B1], []), row(BOB, [E_B1], [])])
check(rc == 0 and [r["actor_id"] for r in verified_of(so)] == [BOB] and NOT_OWNED in se,
      "EVIDENCE_JSON: alice citing bob's event is stripped on the structured path too")
rc, so, se, _ = run(TASK_JSON, [row(ALICE, [E_A1], []), row(BOB, [E_B1], [])])
check(rc == 0 and len(verified_of(so)) == 2 and se == "",
      "EVIDENCE_JSON positive control: alice citing her own event passes")
blocked_bobs = {"subject_id": "subject:3", "evidence_event_ids": [E_B1], "reason_code": "waiting_for_review"}
rc, so, se, _ = run(TASK_BLOCKS, [row(ALICE, [E_A1], [blocked_bobs]), row(BOB, [E_B1], []), row(CAROL, [E_C1], [])])
check(rc == 0 and verified_of(so)[0]["blocked_items"] == []
      and f"strip row[0] {ALICE} blocked_items[0].evidence_event_ids: {E_B1!r} not owned by actor" in se,
      "a blocked item whose only evidence is another actor's event is stripped")

# 9 actors with events and a 1-row answer is the 2026-09-17 under-coverage run.
nine = [{"actor_id": f"ag2space:@w{i}:ag2.space", "id": f"ag2space-message:$id{i}", "detail": "x"}
        for i in range(9)]
TASK_NINE = ("id: t\ntask: go\n\nEVIDENCE_JSON:\n" + json.dumps({"events": nine}) + "\n")

# Block fallback fails closed on a multi-actor block (the reviewer's mutation): a
# `detail:` line merely MENTIONING bob in alice's block must not let bob cite it.
BOB2 = "ag2space:@bob:ag2.space"
E_X = "ag2space-message:$eventA"
BLOCK_CONTROL = ("id: t\ntask: go\n\n"
                 f"event {E_X} by {ALICE}\ndetail: shipping\n\n"
                 f"note: {BOB2} is around today.\n")
BLOCK_COMENTION = BLOCK_CONTROL.replace("detail: shipping\n",
                                        f"detail: shipping\ndetail: discussing with {BOB2}\n")
rc, so, se, written = run(BLOCK_CONTROL, [row(BOB2, [E_X], [])], out=True)
check(rc == 1 and written is None
      and stats_line(so) == "rows=1 distinct_actors=0 actors=2 actors_with_events=1 min_rows=1",
      "block-control: bob citing alice's event is refused")
rc, so, se, written = run(BLOCK_COMENTION, [row(BOB2, [E_X], [])], out=True)
check(rc == 1 and written is None
      and stats_line(so) == "rows=1 distinct_actors=0 actors=2 actors_with_events=2 min_rows=1",
      "block-comention: a mention of bob in alice's block is still refused, floor not lowered")
check(f"strip row[0] {BOB2} working_event_ids: {E_X!r} ambiguous ownership" in se,
      "block-comention: the citation is stripped as ambiguous ownership")
# fail closed both ways: alice cannot cite her own event from the ambiguous block either
rc, so, se, written = run(BLOCK_COMENTION, [row(ALICE, [E_X], [])], out=True)
check(rc == 1 and written is None and "ambiguous ownership" in se,
      "block-comention: alice's own citation from the ambiguous block is refused too")
# positive control: alice citing her event in the single-actor control passes
rc, so, se, written = run(BLOCK_CONTROL, [row(ALICE, [E_X], [])], out=True)
check(rc == 0 and json.loads(written) == [row(ALICE, [E_X], [])] and se == "",
      "block-control positive: alice citing her own single-actor block passes")
# an id claimed from two single-actor blocks is ambiguous as well
TWO_CLAIMS = BLOCK_CONTROL + f"\nevent {E_X} by {BOB2}\ndetail: also mine\n"
rc, so, se, written = run(TWO_CLAIMS, [row(BOB2, [E_X], [])], out=True)
check(rc == 1 and written is None and "ambiguous ownership" in se
      and stats_line(so) == "rows=1 distinct_actors=0 actors=2 actors_with_events=2 min_rows=1",
      "an id claimed by two single-actor blocks is owned by nobody, both actors set the floor")

# Structured evidence is read ONLY from the object after the EVIDENCE_JSON marker
# (the reviewer's mutation): a valid `{"events": []}` decoy must not zero the floor.
STRUCT_CONTROL = ("id: t\ntask: go\n\nEVIDENCE_JSON (untrusted observed data):\n"
                  + json.dumps({"events": nine[:3]}) + "\n")
ONE_W0 = [row("ag2space:@w0:ag2.space", ["ag2space-message:$id0"], [])]
rc, so, se, written = run(STRUCT_CONTROL, ONE_W0, out=True)
check(rc == 1 and written is None
      and stats_line(so) == "rows=1 distinct_actors=1 actors=3 actors_with_events=3 min_rows=2",
      "structured-control: 1 row for 3 actors refused")
rc, so, se, written = run(STRUCT_CONTROL.replace("EVIDENCE_JSON", '{"events": []}\nEVIDENCE_JSON'),
                          ONE_W0, out=True)
check(rc == 1 and written is None
      and stats_line(so) == "rows=1 distinct_actors=1 actors=3 actors_with_events=3 min_rows=2",
      "structured-preamble: a decoy events object before the marker changes nothing")
rc, so, se, written = run(STRUCT_CONTROL + '{"events": []}\n', ONE_W0, out=True)
check(rc == 1 and written is None and "min_rows=2" in stats_line(so),
      "a decoy events object after the marked object changes nothing either")
# same-line form: the object may open on the marker line itself
rc, so, se, written = run("id: t\ntask: go\n\nEVIDENCE_JSON: " + json.dumps({"events": nine[:3]}) + "\n",
                          ONE_W0, out=True)
check(rc == 1 and written is None and "actors_with_events=3" in stats_line(so),
      "marker and object on one line: parsed the same")
# malformed object after the marker: cannot answer, never the block fallback
BAD_OBJ = STRUCT_CONTROL.replace(json.dumps({"events": nine[:3]}), '{"events": [')
rc, so, se, written = run(BAD_OBJ, ONE_W0, out=True)
check(rc == 2 and written is None and so == ""
      and se.startswith("cannot answer: structured evidence unreadable/ambiguous"),
      "malformed object after the marker: exit 2, nothing written")
rc, so, se, written = run(STRUCT_CONTROL.replace(json.dumps({"events": nine[:3]}), '{"day": "x"}'),
                          ONE_W0, out=True)
check(rc == 2 and written is None and "no `events` list" in se,
      "an object without an events list after the marker: exit 2")
rc, so, se, written = run(STRUCT_CONTROL.replace(json.dumps({"events": nine[:3]}), "prose, no object"),
                          ONE_W0, out=True)
check(rc == 2 and written is None, "prose instead of an object after the marker: exit 2")
# two marker lines: ambiguous, cannot answer -- even when both objects agree
rc, so, se, written = run(STRUCT_CONTROL + STRUCT_CONTROL.split("\n\n", 1)[1], ONE_W0, out=True)
check(rc == 2 and written is None and "2 EVIDENCE_JSON marker lines" in se,
      "two EVIDENCE_JSON markers: exit 2, nothing written")
# a malformed marker cannot be rescued by a clean judgment: the refusal is on the evidence
rc, so, se, written = run(BAD_OBJ, [row(f"ag2space:@w{i}:ag2.space", [f"ag2space-message:$id{i}"], [])
                                    for i in range(3)], out=True)
check(rc == 2 and written is None, "malformed marker with a full judgment: still exit 2")

rc, so, se, written = run(TASK_NINE, [row("ag2space:@w0:ag2.space", ["ag2space-message:$id0"], [])], out=True)
check(rc == 1 and written is None
      and stats_line(so) == "rows=1 distinct_actors=1 actors=9 actors_with_events=9 min_rows=5",
      "1 row for 9 actors with events: refused, nothing written")
seven = [row(f"ag2space:@w{i}:ag2.space", [f"ag2space-message:$id{i}"], []) for i in range(7)]
rc, so, se, written = run(TASK_NINE, seven, out=True)
check(rc == 0 and json.loads(written) == seven, "7 rows for 9 actors with events: accepted")

# Coverage counts DISTINCT actors: five copies of w0's row are one actor.
w0 = row("ag2space:@w0:ag2.space", ["ag2space-message:$id0"], [])
rc, so, se, written = run(TASK_NINE, [w0] * 5, out=True)
check(rc == 1 and written is None
      and stats_line(so) == "rows=5 distinct_actors=1 actors=9 actors_with_events=9 min_rows=5",
      "five identical w0 rows for 9 actors: refused, distinct_actors=1")
check(all(f"drop row[{i}] ag2space:@w0:ag2.space: duplicate actor row" in se for i in range(1, 5))
      and "drop row[0]" not in se, "duplicates 1-4 dropped on stderr, the first kept")
# positive control: five DISTINCT rows clear the same floor
five = [row(f"ag2space:@w{i}:ag2.space", [f"ag2space-message:$id{i}"], []) for i in range(5)]
rc, so, se, written = run(TASK_NINE, five, out=True)
check(rc == 0 and json.loads(written) == five and se == ""
      and stats_line(so) == "rows=5 distinct_actors=5 actors=9 actors_with_events=9 min_rows=5",
      "positive control: five distinct rows for 9 actors: accepted, distinct_actors=5")

# Whole-token identities (the reviewer's extended-actor mutation): an id touching
# another id character on either side is a different token, never this id.
TWO_OWNERS = ("id: t\ntask: go\n\n"
              f"event {E_X} by {ALICE}\ndetail: shipping\n\n"
              f"event {E_B1} by {BOB2}\ndetail: rebasing\n")
rc, so, se, written = run(TWO_OWNERS, [row(ALICE, [E_X], [])], out=True)
check(rc == 0 and json.loads(written) == [row(ALICE, [E_X], [])] and se == ""
      and stats_line(so) == "rows=1 distinct_actors=1 actors=2 actors_with_events=2 min_rows=1",
      "token-control: alice's exact token owns her event, 1 of 2 clears the floor")
rc, so, se, written = run(TWO_OWNERS.replace(ALICE, ALICE + "x"), [row(ALICE, [E_X], [])], out=True)
check(rc == 1 and written is None and f"drop row[0]: actor_id {ALICE!r} not in task file" in se
      and stats_line(so) == "rows=1 distinct_actors=0 actors=1 actors_with_events=1 min_rows=1",
      "extended-actor: `ag2.spacex` in the evidence is not alice; her row drops, refused")
# the reviewer's exact shape (single-actor control + prose-only bob): the file has
# an event id that nobody resolvable owns -- cannot answer, never an empty publish
rc, so, se, written = run(BLOCK_CONTROL.replace(ALICE, ALICE + "x"), [row(ALICE, [E_X], [])], out=True)
check(rc == 2 and written is None and "cannot answer: no event ownership resolvable" in se
      and stats_line(so) == "rows=1 distinct_actors=0 actors=1 actors_with_events=0 min_rows=0",
      "extended-actor on the single-actor control: exit 2, nothing written")
rc, so, se, written = run(TWO_OWNERS.replace(E_B1, E_B1 + "x"), [row(BOB2, [E_B1], [])], out=True)
check(rc == 1 and written is None and f"strip row[0] {BOB2} working_event_ids: {E_B1!r} not in task file" in se
      and stats_line(so) == "rows=1 distinct_actors=0 actors=2 actors_with_events=2 min_rows=1",
      "extended-event: `...-3333x` in the evidence is not `...-3333`; the citation is stripped, refused")
rc, so, se, written = run(TWO_OWNERS.replace(E_B1, E_B1 + "x"), [row(BOB2, [E_B1 + "x"], [])], out=True)
check(rc == 0 and json.loads(written) == [row(BOB2, [E_B1 + "x"], [])] and se == "",
      "extended-event positive: citing the whole extended token passes")
# glued on the left, or two ids glued by an alphabet character: no whole token at all
rc, so, se, written = run(TWO_OWNERS.replace(ALICE, "x" + ALICE), [row(ALICE, [E_X], [])], out=True)
check(rc == 1 and written is None and f"drop row[0]: actor_id {ALICE!r} not in task file" in se
      and stats_line(so) == "rows=1 distinct_actors=0 actors=1 actors_with_events=1 min_rows=1",
      "prefixed-actor: `xag2space:@alice…` is not alice; her row drops, refused")
rc, so, se, written = run(TWO_OWNERS.replace(E_X, "x" + E_X), [row(ALICE, [E_X], [])], out=True)
check(rc == 1 and written is None and f"strip row[0] {ALICE} working_event_ids: {E_X!r} not in task file" in se,
      "prefixed-event: `xag2space-message:$eventA` is not $eventA; the citation is stripped, refused")
GLUED = f"id: t\ntask: go\n\nevent {E_X}:{E_B1} by {ALICE}\ndetail: shipping\n\nevent {E_A2} by {BOB2}\n"
rc, so, se, written = run(GLUED, [row(ALICE, [E_X, E_B1], [])], out=True)
check(rc == 1 and written is None and f"{E_X!r} not in task file" in se and f"{E_B1!r} not in task file" in se
      and stats_line(so) == "rows=1 distinct_actors=0 actors=2 actors_with_events=1 min_rows=1",
      "glued events `$eventA:ag2space-message:$bbbb…`: neither half is a token, both citations stripped")
rc, so, se, written = run(GLUED.replace(f"{E_X}:{E_B1}", f"{E_X} {E_B1}"), [row(ALICE, [E_X, E_B1], [])], out=True)
check(rc == 0 and json.loads(written) == [row(ALICE, [E_X, E_B1], [])] and se == "",
      "glued-events positive: the same two ids separated by a space are both owned")
# a truncated citation never equals a whole token on the structured path either
rc, so, se, _ = run(TASK_JSON.replace(E_A1, E_A1 + "x"), [row(ALICE, [E_A1], []), row(BOB, [E_B1], [])])
check(rc == 0 and [r["actor_id"] for r in verified_of(so)] == [BOB]
      and f"strip row[0] {ALICE} working_event_ids: {E_A1!r} not in task file" in se,
      "EVIDENCE_JSON extended-event: the truncated citation is stripped, alice's row drops")
rc, so, se, _ = run(TASK_JSON.replace(E_A1, E_A1 + "x"), [row(ALICE, [E_A1 + "x"], []), row(BOB, [E_B1], [])])
check(rc == 0 and len(verified_of(so)) == 2 and se == "",
      "EVIDENCE_JSON extended-event positive: the whole token is owned and passes")
# punctuation outside the id alphabet (quotes, commas, brackets, parens, whitespace)
# still delimits a token
PUNCT = ("id: t\ntask: go\n\n"
         f'event "{E_X}", by ({ALICE}).\n\n'
         f"event [{E_B1}]; by <{BOB2}>\n")
rc, so, se, written = run(PUNCT, [row(ALICE, [E_X], []), row(BOB2, [E_B1], [])], "--min-coverage", "1.0", out=True)
check(rc == 0 and json.loads(written) == [row(ALICE, [E_X], []), row(BOB2, [E_B1], [])] and se == ""
      and stats_line(so) == "rows=2 distinct_actors=2 actors=2 actors_with_events=2 min_rows=2",
      "punctuation-delimited ids are whole tokens: both rows pass at full coverage")

# Structured multi-claimant (the reviewer's $same mutation): one event id under
# two actor_ids is owned by nobody; both claimants still set the floor.
E_SAME = "ag2space-message:$same"
E_OTHER = "ag2space-message:$other"
def structured(*pairs):
    return ("id: t\ntask: go\n\nEVIDENCE_JSON:\n"
            + json.dumps({"events": [{"actor_id": a, "id": e, "detail": "x"} for a, e in pairs]}) + "\n")
SHARED = structured((ALICE, E_SAME), (BOB2, E_SAME))
rc, so, se, written = run(SHARED, [row(ALICE, [E_SAME], []), row(BOB2, [E_SAME], [])], "--min-coverage", "1.0", out=True)
check(rc == 1 and written is None
      and stats_line(so) == "rows=2 distinct_actors=0 actors=2 actors_with_events=2 min_rows=2",
      "structured two-claimant at full coverage: both rows drop, floor still 2, refused")
check(f"strip row[0] {ALICE} working_event_ids: {E_SAME!r} ambiguous ownership" in se
      and f"strip row[1] {BOB2} working_event_ids: {E_SAME!r} ambiguous ownership" in se
      and f"drop row[0] {ALICE}: no verifiable event ids left" in se
      and f"drop row[1] {BOB2}: no verifiable event ids left" in se,
      "structured two-claimant: each citation stripped as ambiguous ownership, each row dropped")
rc, so, se, written = run(SHARED, [row(ALICE, [E_SAME], []), row(BOB2, [E_SAME], [])], out=True)
check(rc == 1 and written is None and "min_rows=1" in stats_line(so),
      "structured two-claimant at the default floor: still refused (ambiguity never lowers the floor)")
# same-owner positive controls
rc, so, se, written = run(structured((ALICE, E_SAME), (ALICE, E_OTHER)), [row(ALICE, [E_SAME, E_OTHER], [])], "--min-coverage", "1.0", out=True)
check(rc == 0 and json.loads(written) == [row(ALICE, [E_SAME, E_OTHER], [])] and se == "",
      "structured positive: one actor with two events cites both")
rc, so, se, written = run(structured((ALICE, E_SAME), (BOB2, E_OTHER)),
                          [row(ALICE, [E_SAME], []), row(BOB2, [E_OTHER], [])], "--min-coverage", "1.0", out=True)
check(rc == 0 and json.loads(written) == [row(ALICE, [E_SAME], []), row(BOB2, [E_OTHER], [])] and se == "",
      "structured positive: two actors with distinct events pass at full coverage")
# ambiguity strips only the shared id: a claimant's own other event still counts
rc, so, se, written = run(structured((ALICE, E_SAME), (BOB2, E_SAME), (BOB2, E_OTHER)),
                          [row(ALICE, [E_SAME], []), row(BOB2, [E_SAME, E_OTHER], [])], out=True)
check(rc == 0 and json.loads(written) == [row(BOB2, [E_OTHER], [])]
      and f"strip row[1] {BOB2} working_event_ids: {E_SAME!r} ambiguous ownership" in se
      and stats_line(so) == "rows=2 distinct_actors=1 actors=2 actors_with_events=2 min_rows=1",
      "structured: the shared id is stripped for both, bob's own other event keeps his row")

print()
print("checks: %d" % CHECKS)
if failures:
    print("FAILED (%d):" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
