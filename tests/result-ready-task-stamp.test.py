#!/usr/bin/env python3
"""The delivery boundary stamps a task ID, so the PostToolUse hook is not a race.

The hook stamps after a tool call ends, but a bridge can read and post a visible
`results/task-*.txt` before that runs. Every delivery consumer funnels through
read_ready_result_for_delivery, so stamping there is what makes "no ordinary
result is delivered without an ID" structural rather than timing-dependent.
The plain reader stays PURE: an inspection caller (runtime-api views, the
orphan sweep) must never rewrite the result it is only enumerating.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "delivery" / "readiness.py"
_spec = importlib.util.spec_from_file_location("result_ready", _SRC)
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

FAILED = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" — {extra}"))
    if not cond:
        FAILED.append(name)


tmp = Path(tempfile.mkdtemp())
res = tmp / "results"
res.mkdir()

# The race itself: the file is already visible when the boundary reads it.
f = res / "task-1786700000000.txt"
f.write_text("here is your answer")
body = rr.read_ready_result_for_delivery(f)
check("a visible unstamped result cannot be delivered without an ID",
      body.startswith("[task "), body[:40])
check("the stamp is persisted, so archive and audit see what was sent",
      f.read_text().startswith("[task "))

# Bridge control markers only fire as the first non-empty line.
for marker in ("[no-send]", "[deduped: task-9]", "[REPLIED]", "[channel: 123]", "[dm-only]"):
    g = res / f"task-m{abs(hash(marker))}.txt"
    g.write_text(marker + " trailing")
    check(f"marker stays on line 1: {marker}",
          rr.read_ready_result(g).startswith(marker))

h = res / "task-already.txt"
h.write_text("[task 20260101-007]\n\nbody")
check("an already-stamped body is not stamped twice",
      rr.read_ready_result(h).count("[task ") == 1)

pr = res / "proactive-123.txt"
pr.write_text("morning briefing")
check("a proactive body is never task-stamped",
      rr.read_ready_result(pr) == "morning briefing")

ids = []
for i in range(3):
    q = res / f"task-90{i}.txt"
    q.write_text(f"reply {i}")
    ids.append(rr.read_ready_result_for_delivery(q).split("]")[0])
check("each delivered result gets a distinct ID", len(set(ids)) == 3, str(ids))

# The boundary this PR draws: the plain reader is pure. Without this, nothing
# fails if stamping creeps back into the read path.
pure = res / "task-1786700000001.txt"
pure.write_text("untouched by inspection")
check("the PLAIN reader does not stamp",
      rr.read_ready_result(pure) == "untouched by inspection")
check("and does not rewrite the file",
      pure.read_text() == "untouched by inspection")

empty = res / "task-empty.txt"
empty.write_text("   \n")
check("an empty file is still not ready (and mints no ID)",
      rr.read_ready_result(empty) is None)

# A reserved retry must reconcile the day's history. The count commits BEFORE
# the history write, so a failure between them stranded the row permanently.
import json as _json


def _stamp_env():
    root = Path(tempfile.mkdtemp())
    r, st = root / "results", root / "state"
    r.mkdir()
    st.mkdir()
    return r, st


def _agree(st, body, seq):
    """counter, stamped body and daily history all describe the same one ID."""
    ctr = _json.loads((st / "task-counter.json").read_text())
    hist_p = st / "task-completions-daily.json"
    if not hist_p.exists():
        return False, "history file absent"
    day = ctr.get("date")
    hist = _json.loads(hist_p.read_text())
    return (body.startswith(f"[task {day}-{seq:03d}]")
            and ctr.get("count") == seq
            and "pending" not in ctr
            and hist.get(day) == seq), f"ctr={ctr} hist={hist} body={body.splitlines()[0]!r}"


_r, _st = _stamp_env()
_p = _r / "task-hist-fail.txt"
_p.write_text("body\n")
(_st / "task-completions-daily.json.tmp").mkdir()          # force the history write to fail
_first = rr.stamp_result_file(_p)
_ctr = _json.loads((_st / "task-counter.json").read_text())
check("a failed history write fails the stamp closed, keeping the reservation",
      _first is None and _ctr.get("count") == 1
      and (_ctr.get("pending") or {}).get("task-hist-fail.txt", "").endswith("-001"),
      f"got {_first!r} ctr={_ctr}")
check("and today's history really is missing at that point",
      not (_st / "task-completions-daily.json").exists())
(_st / "task-completions-daily.json.tmp").rmdir()
_second = rr.stamp_result_file(_p)
_ok, _why = _agree(_st, _second or "", 1)
check("the reserved retry reconciles history — counter, result and history agree",
      _ok, _why)
check("and the retry spends no second ID",
      (_second or "").startswith("[task ") and _p.read_text().startswith(_second or "x"))

# Control: the same assertion on an unobstructed run, so a reconcile that wrote
# nothing could not pass the check above by accident.
_r2, _st2 = _stamp_env()
_p2 = _r2 / "task-clean.txt"
_p2.write_text("body\n")
_clean = rr.stamp_result_file(_p2)
_ok2, _why2 = _agree(_st2, _clean or "", 1)
check("control: an unobstructed stamp already agrees on all three", _ok2, _why2)

print("\n" + ("PASS — delivery-boundary task stamping" if not FAILED
              else f"FAIL — {len(FAILED)} check(s): {FAILED}"))
sys.exit(1 if FAILED else 0)
