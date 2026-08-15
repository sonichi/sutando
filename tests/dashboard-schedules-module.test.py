#!/usr/bin/env python3
"""Direct unit coverage for src/dashboard_schedules.py.

Exercises the module itself with temporary paths and fixed datetimes — no
dashboard import, no HTTP, no workspace resolution. The dashboard-facing
integration (that dashboard.py delegates here and the routes still behave) is
covered by tests/dashboard-editable-schedules.test.py and
tests/dashboard-schedules.test.py.
"""
import os
import sys
import json
import tempfile
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import dashboard_schedules as ds  # noqa: E402

_fails: list[str] = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok  {label}")
    else:
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))
        _fails.append(label)


def tmp_path(name="crons.json"):
    d = tempfile.mkdtemp(prefix="ds-test-")
    return Path(d) / name


# ── cron field matching ──────────────────────────────────────────────────────
print("── cron field matching ──")
check("'*' matches anything", ds.cron_field_match("*", 37))
check("'*/5' matches a multiple", ds.cron_field_match("*/5", 15))
check("'*/5' rejects a non-multiple", not ds.cron_field_match("*/5", 16))
check("'*/0' does not divide by zero", not ds.cron_field_match("*/0", 3))
check("range A-B matches inside", ds.cron_field_match("10-20", 15))
check("range A-B rejects outside", not ds.cron_field_match("10-20", 21))
check("comma list matches a member", ds.cron_field_match("1,4,9", 4))
check("comma list rejects a non-member", not ds.cron_field_match("1,4,9", 5))
check("plain integer matches", ds.cron_field_match("7", 7))
check("garbage token matches nothing", not ds.cron_field_match("foo", 7))

# ── cron field validation ────────────────────────────────────────────────────
print("── cron field validation ──")
check("'*' is valid", ds.cron_field_valid("*", 0, 59))
check("'*/15' is valid", ds.cron_field_valid("*/15", 0, 59))
check("'*/0' is rejected (zero step)", not ds.cron_field_valid("*/0", 0, 59))
check("in-range integer is valid", ds.cron_field_valid("59", 0, 59))
check("out-of-range integer is rejected", not ds.cron_field_valid("99", 0, 59))
check("inverted range is rejected", not ds.cron_field_valid("20-10", 0, 59))
check("valid range accepted", ds.cron_field_valid("10-20", 0, 59))
check("range past the ceiling is rejected", not ds.cron_field_valid("50-70", 0, 59))
check("non-numeric token is rejected", not ds.cron_field_valid("foo", 0, 59))
check("empty spec is rejected", not ds.cron_field_valid("", 0, 59))
check("whitespace spec is rejected", not ds.cron_field_valid("   ", 0, 59))
check("dow accepts 7 (Sunday alias)", ds.cron_field_valid("7", 0, 7))
check("bounds tuple is the documented five",
      ds.CRON_BOUNDS == ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)),
      str(ds.CRON_BOUNDS))

# ── next_run (deterministic, fixed now) ──────────────────────────────────────
print("── next_run ──")
_now = datetime(2026, 8, 3, 10, 0, 0)
check("every-5 lands on the next multiple",
      ds.next_run("*/5 * * * *", _now) == datetime(2026, 8, 3, 10, 5),
      str(ds.next_run("*/5 * * * *", _now)))
check("daily 06:57 rolls to tomorrow when today has passed",
      ds.next_run("57 6 * * *", _now) == datetime(2026, 8, 4, 6, 57),
      str(ds.next_run("57 6 * * *", _now)))
check("wrong arity returns None", ds.next_run("* * *", _now) is None)
check("six fields returns None", ds.next_run("* * * * * *", _now) is None)
check("no match inside horizon returns None",
      ds.next_run("0 0 30 2 *", _now) is None)
check("seconds/microseconds are rounded off",
      ds.next_run("*/5 * * * *", datetime(2026, 8, 3, 10, 0, 42, 7)).second == 0)
check("horizon is honoured (1-day horizon can't reach a 5-day-out cron)",
      ds.next_run("0 0 8 8 *", _now, horizon_days=1) is None)
# dow: 2026-08-03 is a Monday -> cron dow 1
check("day-of-week matches Monday as 1",
      ds.next_run("0 12 * * 1", _now) == datetime(2026, 8, 3, 12, 0),
      str(ds.next_run("0 12 * * 1", _now)))

# ── read_crons tolerance ─────────────────────────────────────────────────────
print("── read_crons ──")
p = tmp_path()
check("missing file reads as []", ds.read_crons(p) == [])
p.write_text("{not json")
check("malformed JSON reads as []", ds.read_crons(p) == [])
p.write_text('{"a": 1}')
check("non-list JSON reads as []", ds.read_crons(p) == [])
p.write_text('[{"name": "x"}]')
check("a list reads back", ds.read_crons(p) == [{"name": "x"}])

# ── write_crons atomicity + cleanup ──────────────────────────────────────────
print("── write_crons ──")
p2 = tmp_path()
ds.write_crons(p2, [{"name": "a"}])
check("write creates the parent directory and round-trips",
      json.loads(p2.read_text()) == [{"name": "a"}])
check("write leaves no .tmp behind",
      not list(p2.parent.glob("*.tmp")))

p3 = tmp_path()
ds.write_crons(p3, [{"name": "seed"}])
_real_replace = os.replace
try:
    os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    raised = False
    try:
        ds.write_crons(p3, [{"name": "new"}])
    except OSError:
        raised = True
finally:
    os.replace = _real_replace
check("failed write re-raises OSError", raised)
check("failed write removes its temp file", not list(p3.parent.glob("*.tmp")))
check("failed write leaves the original intact",
      json.loads(p3.read_text()) == [{"name": "seed"}])

# ── validate_job ─────────────────────────────────────────────────────────────
print("── validate_job ──")
_ok = {"name": "n", "cron": "*/5 * * * *", "prompt_skill": "s"}
check("a well-formed job validates", ds.validate_job(_ok) is None)
check("a shell-command job validates",
      ds.validate_job({"name": "shell", "cron": "*/5 * * * *",
                       "shell_command": "bash scripts/poll.sh"}) is None)
check("shell-command conflicts with prompt",
      ds.validate_job({"name": "shell", "cron": "*/5 * * * *",
                       "shell_command": "bash scripts/poll.sh", "prompt": "x"})
      == "provide exactly one of shell_command, prompt or prompt_skill")
check("non-dict rejected", ds.validate_job("nope") == "job must be an object")
check("missing name rejected",
      ds.validate_job({**_ok, "name": ""}) == "name is required")
check("wrong arity cron rejected",
      ds.validate_job({**_ok, "cron": "* * *"})
      == "cron must be a 5-field expression (min hour dom month dow)")
check("garbage cron rejected by syntax, not by next_run",
      ds.validate_job({**_ok, "cron": "foo bar baz qux quux"})
      == "invalid cron expression: 'foo bar baz qux quux'")
check("out-of-range minute rejected",
      ds.validate_job({**_ok, "cron": "99 * * * *"}) is not None)
check("both prompt and skill rejected",
      ds.validate_job({**_ok, "prompt": "p"})
      == "provide exactly one of prompt or prompt_skill")
check("neither prompt nor skill rejected",
      ds.validate_job({"name": "n", "cron": "*/5 * * * *"})
      == "provide exactly one of prompt or prompt_skill")

# ── upsert / delete: exact response objects ──────────────────────────────────
print("── upsert / delete ──")
p4 = tmp_path()
code, resp = ds.upsert_schedule(p4, {"name": "j1", "cron": "*/5 * * * *",
                                     "prompt_skill": "morning-briefing"})
check("add returns 200", code == 200, str(code))
check("add response is byte-for-byte the documented object",
      resp == {"ok": True, "name": "j1", "count": 1,
               "note": "Saved. Takes effect on the next /schedule-crons run (restart)."},
      str(resp))

code, resp = ds.upsert_schedule(p4, {"name": 123})
check("non-string name returns 400 with the field named",
      (code, resp) == (400, {"error": "name must be a string"}), str(resp))
code, resp = ds.upsert_schedule(p4, "not a dict")
check("non-dict body returns 400",
      (code, resp) == (400, {"error": "malformed JSON body"}), str(resp))
code, resp = ds.upsert_schedule(p4, {"name": "   "})
check("blank name returns 400",
      (code, resp) == (400, {"error": "name is required"}), str(resp))

# cron-only edit inherits prompt_skill and preserves unknown scheduler fields
ds.write_crons(p4, [{"name": "j2", "cron": "0 9 * * *", "prompt_skill": "morning-briefing",
                     "launchd": True, "room": "!abc:ag2.space", "retry_minutes": 5}])
code, _ = ds.upsert_schedule(p4, {"name": "j2", "cron": "0 10 * * *"})
_j2 = next(j for j in ds.read_crons(p4) if j["name"] == "j2")
check("cron-only edit succeeds", code == 200)
check("cron-only edit inherits the existing skill",
      _j2.get("prompt_skill") == "morning-briefing", str(_j2))
check("cron-only edit applies the new cron", _j2.get("cron") == "0 10 * * *", str(_j2))
check("cron-only edit preserves unrecognized scheduler fields",
      _j2.get("launchd") is True and _j2.get("room") == "!abc:ag2.space"
      and _j2.get("retry_minutes") == 5, str(_j2))

# switching skill -> prompt drops the mutually exclusive field
code, _ = ds.upsert_schedule(p4, {"name": "j2", "prompt": "do the thing"})
_j2 = next(j for j in ds.read_crons(p4) if j["name"] == "j2")
check("switching to prompt removes prompt_skill",
      "prompt_skill" not in _j2 and _j2.get("prompt") == "do the thing", str(_j2))
# and back again
ds.upsert_schedule(p4, {"name": "j2", "prompt_skill": "morning-briefing"})
_j2 = next(j for j in ds.read_crons(p4) if j["name"] == "j2")
check("switching back to skill removes prompt",
      "prompt" not in _j2 and _j2.get("prompt_skill") == "morning-briefing", str(_j2))

# A shell job is executable ONLY by the launchd runner; the session scheduler
# skips it. Unflagged, a newly posted one would never run at all.
ds.upsert_schedule(p4, {"name": "mech", "cron": "*/5 * * * *",
                        "shell_command": "echo hi"})
_m = next(j for j in ds.read_crons(p4) if j["name"] == "mech")
check("a new shell job is flagged launchd-owned", _m.get("launchd") is True, str(_m))

# Use a FRESH prompt job: one already carrying the launchd flag would pass
# whether or not the code claims ownership.
ds.upsert_schedule(p4, {"name": "conv", "cron": "0 9 * * *", "prompt": "plain"})
_pre = next(j for j in ds.read_crons(p4) if j["name"] == "conv")
check("fixture starts unflagged, so the next assertion can fail",
      "launchd" not in _pre, str(_pre))
ds.upsert_schedule(p4, {"name": "conv", "shell_command": "echo converted"})
_conv = next(j for j in ds.read_crons(p4) if j["name"] == "conv")
check("converting a prompt job to shell claims launchd ownership",
      _conv.get("launchd") is True and "prompt" not in _conv, str(_conv))
ds.delete_schedule(p4, "conv")

# restore the fixture for the assertions below (they assert an exact count)
ds.upsert_schedule(p4, {"name": "j2", "prompt_skill": "morning-briefing"})
ds.delete_schedule(p4, "mech")

# upsert replaces by name rather than duplicating
ds.upsert_schedule(p4, {"name": "j2", "cron": "0 11 * * *"})
check("upsert replaces by name (no duplicate)",
      [j["name"] for j in ds.read_crons(p4)].count("j2") == 1,
      str(ds.read_crons(p4)))

code, resp = ds.delete_schedule(p4, "j2")
check("delete returns 200 with the documented object",
      (code, resp) == (200, {"deleted": "j2", "count": 0,
                             "note": "Removed. Takes effect on the next /schedule-crons run (restart)."}),
      str(resp))
code, resp = ds.delete_schedule(p4, "ghost")
check("deleting a missing name returns the 404 object",
      (code, resp) == (404, {"error": "not found", "name": "ghost"}), str(resp))

# ── concurrency: no lost acknowledged writes ─────────────────────────────────
print("── concurrency ──")
p5 = tmp_path()
ds.write_crons(p5, [])
_N = 24
_bar = threading.Barrier(_N)
_codes = {}
_errs = []
# Widen the read→write window so an unlocked implementation reliably loses a
# write. Patch the module global — the transactions call read_crons through it
# precisely so this seam exists (see the module docstring).
_orig = ds.read_crons


def _slow(path):
    import time
    r = _orig(path)
    time.sleep(0.003)
    return r


ds.read_crons = _slow


def _w(i):
    try:
        _bar.wait(timeout=10)
        c, _ = ds.upsert_schedule(p5, {"name": f"job{i:02d}", "cron": "*/5 * * * *",
                                       "prompt_skill": "morning-briefing"})
        _codes[i] = c
    except Exception as e:
        _errs.append(f"{i}: {type(e).__name__}: {e}")


_ts = [threading.Thread(target=_w, args=(i,)) for i in range(_N)]
for t in _ts:
    t.start()
for t in _ts:
    t.join(timeout=15)
ds.read_crons = _orig

check("concurrent upserts: none raised", not _errs, "; ".join(_errs))
check("concurrent upserts: all acknowledged 200",
      len(_codes) == _N and all(c == 200 for c in _codes.values()), str(_codes))
check("concurrent upserts: every acknowledged write persisted",
      {j["name"] for j in ds.read_crons(p5)} == {f"job{i:02d}" for i in range(_N)},
      f"persisted {len(ds.read_crons(p5))}/{_N}")

# upsert racing a delete
p6 = tmp_path()
ds.write_crons(p6, [{"name": "keep", "cron": "*/5 * * * *", "prompt_skill": "s"},
                    {"name": "victim", "cron": "*/5 * * * *", "prompt_skill": "s"}])
_b2 = threading.Barrier(2)


def _up():
    _b2.wait(timeout=10)
    ds.upsert_schedule(p6, {"name": "added", "cron": "0 9 * * *", "prompt_skill": "s"})


def _del():
    _b2.wait(timeout=10)
    ds.delete_schedule(p6, "victim")


_t1, _t2 = threading.Thread(target=_up), threading.Thread(target=_del)
_t1.start(), _t2.start()
_t1.join(timeout=15), _t2.join(timeout=15)
check("upsert || delete serialize cleanly",
      {j["name"] for j in ds.read_crons(p6)} == {"keep", "added"},
      str(sorted(j["name"] for j in ds.read_crons(p6))))

print()
if _fails:
    print(f"FAIL — {len(_fails)}: {_fails}")
    sys.exit(1)
print("PASS — dashboard_schedules module")
