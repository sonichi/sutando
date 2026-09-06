#!/usr/bin/env python3
"""Behavioral tests for the task-ID stamping hook + the per-day completions
report. Exercises the real code against a temp workspace (module globals
repointed), not mocks.

Covers:
  result_ready.alloc_task_id — increments, formats NNN, resets on a new day, persists history
  hook.main    — stamps a fresh unstamped result, skips already-stamped / stale
                 (mtime) / empty files; does NOT double-count skips
  report       — load_history reads the file + folds today's live counter;
                 render lists per-day counts newest-first with a total

Run: python3 tests/stamp-task-id.test.py    (exit 0 pass, 1 fail)
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _failed
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        _failed += 1


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _point(mod, ws: Path):
    """Repoint a freshly-loaded module's globals at a temp workspace."""
    (ws / "state").mkdir(parents=True, exist_ok=True)
    (ws / "results").mkdir(parents=True, exist_ok=True)
    mod.WS = ws
    mod.COUNTER = ws / "state" / "task-counter.json"
    if hasattr(mod, "HISTORY"):
        mod.HISTORY = ws / "state" / "task-completions-daily.json"
    if hasattr(mod, "RESULTS"):
        mod.RESULTS = ws / "results"


RR = _load(REPO / "src" / "delivery" / "readiness.py", "result_ready_owner")

TODAY = datetime.date.today().strftime("%Y%m%d")

# alloc_task_id — tested against its owner directly
with tempfile.TemporaryDirectory() as t:
    ws = Path(t)
    hook = _load(REPO / "hooks" / "stamp-task-id.py", "stamp_task_id")
    _point(hook, ws)

    a1 = RR.alloc_task_id(ws / "results")
    a2 = RR.alloc_task_id(ws / "results")
    check("alloc formats YYYYMMDD-001", a1 == f"{TODAY}-001", a1)
    check("alloc increments to -002", a2 == f"{TODAY}-002", a2)
    counter = json.load(open(hook.COUNTER))
    check("counter persists date+count", counter == {"date": TODAY, "count": 2}, str(counter))
    hist = json.load(open(hook.HISTORY))
    check("history records today's running total", hist.get(TODAY) == 2, str(hist))

# Counter recovery: a truncated/corrupt counter must not remint 001 over a day
# already in progress, nor roll today's history backwards (CR #2125, qingyun-wu).
for label, bad in (("empty", ""), ("corrupt", "{not json")):
    with tempfile.TemporaryDirectory() as t:
        ws = Path(t)
        hook = _load(REPO / "hooks" / "stamp-task-id.py", "stamp_task_id")
        _point(hook, ws)
        json.dump({TODAY: 37}, open(hook.HISTORY, "w"))
        hook.COUNTER.write_text(bad)
        got = RR.alloc_task_id(ws / "results")
        check(f"a {label} counter recovers from today's history, not 001",
              got == f"{TODAY}-038", got)
        hist = json.load(open(hook.HISTORY))
        check(f"a {label} counter does not roll history backwards",
              hist.get(TODAY) == 38, str(hist))

with tempfile.TemporaryDirectory() as t:
    ws = Path(t)
    hook = _load(REPO / "hooks" / "stamp-task-id.py", "stamp_task_id")
    _point(hook, ws)
    json.dump({"20260101": 99}, open(hook.HISTORY, "w"))
    got = RR.alloc_task_id(ws / "results")
    check("a NEW day is not inflated by an older day's history", got == f"{TODAY}-001", got)

with tempfile.TemporaryDirectory() as t:
    ws = Path(t)
    hook = _load(REPO / "hooks" / "stamp-task-id.py", "stamp_task_id")
    _point(hook, ws)
    json.dump({"date": TODAY, "count": 9}, open(hook.COUNTER, "w"))
    json.dump({TODAY: 3}, open(hook.HISTORY, "w"))
    got = RR.alloc_task_id(ws / "results")
    check("a healthy counter ahead of history still wins", got == f"{TODAY}-010", got)
    check("history rises to the counter", json.load(open(hook.HISTORY)).get(TODAY) == 10)

# daily reset preserves past days
with tempfile.TemporaryDirectory() as t:
    ws = Path(t)
    hook = _load(REPO / "hooks" / "stamp-task-id.py", "stamp_task_id2")
    _point(hook, ws)
    json.dump({"date": "20260101", "count": 7}, open(hook.COUNTER, "w"))
    json.dump({"20260101": 7}, open(hook.HISTORY, "w"))
    got = RR.alloc_task_id(ws / "results")
    check("alloc resets counter on a new day", got == f"{TODAY}-001", got)
    hist = json.load(open(hook.HISTORY))
    check("history keeps the prior day", hist.get("20260101") == 7, str(hist))
    check("history adds the new day", hist.get(TODAY) == 1, str(hist))

    # malformed counter/history files → alloc still succeeds (fail-open read)
    hook.COUNTER.write_text("corrupt")
    hook.HISTORY.write_text("corrupt")
    got = RR.alloc_task_id(ws / "results")
    check("alloc recovers from a corrupt counter", got == f"{TODAY}-001", got)
    check("history rebuilt after corruption", json.load(open(hook.HISTORY)).get(TODAY) == 1)

# Concurrent alloc_task_id must never mint a duplicate id or lose a count: each
# _alloc takes an exclusive flock on its own fd, serializing the read-modify-write.
import threading as _threading
with tempfile.TemporaryDirectory() as t:
    ws = Path(t)
    hookc = _load(REPO / "hooks" / "stamp-task-id.py", "stamp_task_id_conc")
    _point(hookc, ws)
    _ids: list[str] = []
    _ids_lock = _threading.Lock()

    def _spin():
        for _ in range(10):
            got = RR.alloc_task_id(ws / "results")
            with _ids_lock:
                _ids.append(got)

    _threads = [_threading.Thread(target=_spin) for _ in range(10)]
    for _th in _threads:
        _th.start()
    for _th in _threads:
        _th.join()
    check("concurrent alloc_task_id mints no duplicate ids (locked RMW)",
          len(set(_ids)) == len(_ids) == 100, f"{len(set(_ids))} unique of {len(_ids)}")
    check("concurrent alloc_task_id final counter == total allocations",
          json.load(open(hookc.COUNTER)).get("count") == 100, str(json.load(open(hookc.COUNTER))))

# hook.main — stamping behavior
with tempfile.TemporaryDirectory() as t:
    ws = Path(t)
    hook = _load(REPO / "hooks" / "stamp-task-id.py", "stamp_task_id3")
    _point(hook, ws)
    R = ws / "results"

    fresh = R / "task-1.txt"; fresh.write_text("Done with the thing.\n")
    stamped = R / "task-2.txt"; stamped.write_text(f"[task {TODAY}-005]\n\nAlready has an id.\n")
    empty = R / "task-3.txt"; empty.write_text("   \n")
    stale = R / "task-4.txt"; stale.write_text("Old undelivered result.\n")
    old = time.time() - 3600
    os.utime(stale, (old, old))  # backlog file, older than the freshness window

    try:
        hook.main()  # real hook fail-open-exits 0; swallow that for in-process testing
    except SystemExit:
        pass

    check("fresh unstamped result gets an id", hook.re.compile(r'^\s*\[task \d{8}-\d{3}').match(fresh.read_text()) is not None,
          fresh.read_text()[:40])
    check("already-stamped file unchanged",
          stamped.read_text() == f"[task {TODAY}-005]\n\nAlready has an id.\n")
    check("empty file left alone", empty.read_text() == "   \n")
    check("stale/backlog file NOT stamped (mtime guard)",
          hook.re.compile(r'^\s*\[task \d{8}-\d{3}').match(stale.read_text()) is None, stale.read_text()[:40])
    # only the one fresh file consumed a counter id
    counter = json.load(open(hook.COUNTER))
    check("only fresh file advanced the counter (=1)", counter.get("count") == 1, str(counter))

# Bridge control markers must be left unstamped: a prepended `[task …]` pushes the
# marker off line 1, which is the only place skip/redirect routing reads it.
with tempfile.TemporaryDirectory() as t:
    ws = Path(t)
    hook = _load(REPO / "hooks" / "stamp-task-id.py", "stamp_task_id_markers")
    _point(hook, ws)
    R = ws / "results"

    markers = {
        "no-send": "[no-send]\n",
        "deduped": "[deduped: task-123]\n\nSee the other task.\n",
        "replied": "[REPLIED]\n",
        "channel": "[channel: 1512996282140987452]\n\nRouted elsewhere.\n",
        "dm-only": "[dm-only]\nprivate.\n",
    }
    files = {}
    for i, (name, body) in enumerate(markers.items()):
        f = R / f"task-m{i}.txt"; f.write_text(body); files[name] = (f, body)

    try:
        hook.main()
    except SystemExit:
        pass

    for name, (f, body) in files.items():
        check(f"bridge marker [{name}] left unstamped (verbatim)", f.read_text() == body,
              f.read_text()[:50])
    check("no bridge-marker file consumed a counter id",
          not hook.COUNTER.exists() or json.load(open(hook.COUNTER)).get("count", 0) == 0)

# report — load_history + render
with tempfile.TemporaryDirectory() as t:
    ws = Path(t)
    rep = _load(REPO / "scripts" / "task-completions.py", "task_completions")
    _point(rep, ws)
    # Use fixed PAST dates (not today) so the seed never collides with the run day.
    json.dump({"20260101": 37, "20260102": 49}, open(rep.HISTORY, "w"))
    json.dump({"date": TODAY, "count": 3}, open(rep.COUNTER, "w"))

    hist = rep.load_history()
    check("load_history reads recorded days", hist.get("20260102") == 49, str(hist))
    check("load_history folds today's live counter", hist.get(TODAY) == 3, str(hist))

    # --all (days=None) shows every recorded day, including months-old ones.
    out_all = rep.render(hist, days=None)
    check("render --all lists an old recorded day", "2026-01-02" in out_all and ": 49" in out_all, out_all)
    check("render marks today", "today: 3" in out_all, out_all)
    check("render shows a total line", "total" in out_all, out_all)

    # A bounded --days window is CALENDAR days: the months-old fixtures must fall
    # outside a 14-day window even though few days are recorded.
    out14 = rep.render(hist, days=14)
    check("render --days 14 excludes months-old days (calendar window)",
          "2026-01-02" not in out14 and "2026-01-01" not in out14, out14)
    # A window wide enough to reach the Jan fixtures includes them again.
    span = (datetime.date.today() - datetime.date(2026, 1, 1)).days + 1
    out_wide = rep.render(hist, days=span)
    check("render --days <wide> reaches the Jan fixtures", "2026-01-02" in out_wide, out_wide)

    # counter ahead of a stale history entry wins (mid-day report reflects live count)
    json.dump({TODAY: 1}, open(rep.HISTORY, "w"))
    json.dump({"date": TODAY, "count": 9}, open(rep.COUNTER, "w"))
    check("live counter overrides a lagging history entry", rep.load_history().get(TODAY) == 9)

    # empty / malformed inputs → graceful
    check("render on empty history", rep.render({}, days=14) == "No task completions recorded yet.")
    check("_fmt_day passes through a non-date", rep._fmt_day("notadate") == "notadate")
    rep.HISTORY.write_text("this is not json")       # malformed history
    rep.COUNTER.write_text("also not json")          # malformed counter
    check("load_history tolerates malformed files", rep.load_history() == {})
    # a history file that is a JSON list, plus a bad-value entry → ignored, not crash
    json.dump(["nope"], open(rep.HISTORY, "w"))
    check("load_history ignores non-dict json", rep.load_history() == {})
    json.dump({"20260101": "NaN", "20260103": 4}, open(rep.HISTORY, "w"))
    json.dump({"date": TODAY, "count": 2}, open(rep.COUNTER, "w"))
    h = rep.load_history()
    check("load_history skips a non-int value", "20260101" not in h and h.get("20260103") == 4, str(h))

    # main() entrypoint — default, --all, --json, --days
    import contextlib
    import io

    def run_main(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = rep.main(argv)
        return rc, buf.getvalue()

    rc, out = run_main([])
    check("main() default exits 0 + prints a report", rc == 0 and "completions by day" in out, out[:60])
    rc, out = run_main(["--all"])
    check("main() --all lists recorded day", rc == 0 and "2026-01-03" in out, out[:80])
    rc, out = run_main(["--json"])
    check("main() --json emits parseable {day:count}", json.loads(out).get("20260103") == 4, out[:80])
    rc, out = run_main(["--days", "1"])
    check("main() --days 1 shows a single day", rc == 0 and out.count(":") >= 1, out[:80])

# hook.main vs the delivery path: the two writers must not both mint an ID for one
# completion, or the ID on the wire disagrees with the one left on disk.
_TRIALS = 30
_double = _mismatch = 0
_idre = __import__("re").compile(r"\[task (\d{8}-\d{3})\]")
for _i in range(_TRIALS):
    with tempfile.TemporaryDirectory() as t:
        ws = Path(t)
        _rr = _load(REPO / "src" / "delivery" / "readiness.py", f"rr_race{_i}")
        _hk = _load(REPO / "hooks" / "stamp-task-id.py", f"hk_race{_i}")
        _point(_hk, ws)
        f = ws / "results" / "task-9.txt"
        f.write_text("One completion.\n")
        _wire: list[str | None] = []

        def _deliver() -> None:
            _wire.append(_rr.read_ready_result(f))

        def _stamp() -> None:
            try:
                _hk.main()
            except SystemExit:
                pass

        _ta, _tb = _threading.Thread(target=_stamp), _threading.Thread(target=_deliver)
        _ta.start(); _tb.start(); _ta.join(); _tb.join()

        if json.load(open(_hk.COUNTER)).get("count", 0) > 1:
            _double += 1
        _d = _idre.match(f.read_text().strip())
        _w = _idre.match((_wire[0] or "").strip())
        if _d and _w and _d.group(1) != _w.group(1):
            _mismatch += 1

check(f"concurrent hook+delivery mints ONE id per completion ({_TRIALS} trials)",
      _double == 0, f"{_double}/{_TRIALS} trials burned two counter ids")
check(f"id on disk == id delivered on the wire ({_TRIALS} trials)",
      _mismatch == 0, f"{_mismatch}/{_TRIALS} trials disagreed")

print()
if _failed:
    print(f"FAIL — {_failed} check(s) failed")
    raise SystemExit(1)
print("PASS — stamp-task-id + task-completions")
