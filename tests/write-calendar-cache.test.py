#!/usr/bin/env python3
"""Tests for the morning-briefing calendar-cache PRODUCER (PR #2256).

Chi's CR: the PR added the cache *reader* but "does not activate or produce the
trusted source it claims to fix." These tests exercise the producer
(`src/write_calendar_cache.py`) AND prove the round-trip: what the producer
writes is exactly what `morning-briefing.get_calendar_events()` reads back.

Run: python3 tests/write-calendar-cache.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest.mock
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

wcc = importlib.import_module("write_calendar_cache")

# morning-briefing.py is hyphenated → load via importlib to exercise the reader.
_spec = importlib.util.spec_from_file_location("morning_briefing", REPO / "src" / "morning-briefing.py")
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)

_passed = 0
_failed = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok  {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


TODAY = datetime.now().strftime("%Y-%m-%d")


def test_normalize_mixed_shapes() -> None:
    out = wcc.normalize_events(["9am Standup", {"raw": "12:30 Sync", "calendar": "work"}, {"raw": "  "}, {}, "  "])
    ok("normalize: keeps strings + dicts, drops blanks",
       out == [{"raw": "9am Standup", "calendar": ""}, {"raw": "12:30 Sync", "calendar": "work"}],
       str(out))


def test_write_schema_and_atomic() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "state" / "calendar-today.json"
        wcc.write_cache([{"raw": "9:00-9:30am 1:1 w/ Sam", "calendar": "work"}], path=p)
        data = json.loads(p.read_text())
        ok("write: date stamped to local today", data.get("date") == TODAY, str(data.get("date")))
        ok("write: events in {raw,calendar} schema",
           data.get("events") == [{"raw": "9:00-9:30am 1:1 w/ Sam", "calendar": "work"}], str(data.get("events")))
        ok("write: no leftover .tmp (atomic replace)", not (p.with_suffix(p.suffix + ".tmp")).exists())


def test_empty_is_verified_empty() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "state" / "calendar-today.json"
        wcc.write_cache([], path=p)
        data = json.loads(p.read_text())
        ok("empty day: events == [] (verified-empty, not missing)", data.get("events") == [], str(data))


def test_producer_feeds_reader_roundtrip() -> None:
    """THE load-bearing test for Chi's CR: producer output == what the briefing reads."""
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "state" / "calendar-today.json"
        events = [{"raw": "9:00-9:30am 1:1 w/ Sam Bretz", "calendar": "work"},
                  {"raw": "12:30-1:00pm Product daily sync", "calendar": "work"}]
        wcc.write_cache(events, path=cache)
        # Point the briefing reader at the same file and read it back.
        with unittest.mock.patch.object(mb, "CALENDAR_CACHE_FILE", cache):
            read = mb._read_calendar_cache()
        ok("round-trip: reader sees exactly what producer wrote",
           read == events, f"read={read}")
        # And the top-level get_calendar_events() prefers the cache (returns the 2 events, not a local read).
        with unittest.mock.patch.object(mb, "CALENDAR_CACHE_FILE", cache):
            got = mb.get_calendar_events()
        ok("get_calendar_events: prefers the produced Google cache",
           got == events, f"got={got}")


def test_stale_cache_ignored() -> None:
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "state" / "calendar-today.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"date": "2000-01-01", "events": [{"raw": "old"}]}))
        with unittest.mock.patch.object(mb, "CALENDAR_CACHE_FILE", cache):
            ok("stale (yesterday's) cache is ignored, not shown",
               mb._read_calendar_cache() is None)


def test_cli_empty_flag() -> None:
    with tempfile.TemporaryDirectory() as td:
        with unittest.mock.patch.object(wcc, "cache_path", lambda: Path(td) / "state" / "calendar-today.json"):
            rc = wcc.main(["--empty"])
        data = json.loads((Path(td) / "state" / "calendar-today.json").read_text())
        ok("CLI --empty: exit 0, verified-empty written", rc == 0 and data.get("events") == [], f"rc={rc}")


def test_cli_events_json_flag() -> None:
    """--events-json path: the agent hands the pulled Google events straight in."""
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "state" / "calendar-today.json"
        with unittest.mock.patch.object(wcc, "cache_path", lambda: cache):
            rc = wcc.main(["--events-json",
                           json.dumps([{"raw": "9am Standup", "calendar": "work"}, "3pm Review"])])
        data = json.loads(cache.read_text())
        ok("CLI --events-json: exit 0, mixed string+dict written in schema",
           rc == 0 and data.get("events") == [{"raw": "9am Standup", "calendar": "work"},
                                              {"raw": "3pm Review", "calendar": ""}],
           f"rc={rc} data={data}")


def test_cli_stdin_events() -> None:
    """No flag → read the JSON array from stdin (the piped-from-connector path)."""
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "state" / "calendar-today.json"
        with unittest.mock.patch.object(wcc, "cache_path", lambda: cache), \
             unittest.mock.patch.object(sys, "stdin", io.StringIO('[{"raw": "10am 1:1 w/ Sam"}]')):
            rc = wcc.main([])
        data = json.loads(cache.read_text())
        ok("CLI stdin: reads events array from stdin",
           rc == 0 and data.get("events") == [{"raw": "10am 1:1 w/ Sam", "calendar": ""}], f"rc={rc}")


def test_cli_error_paths() -> None:
    """Every bad-input branch returns 2 and writes nothing (never a blank cache)."""
    with unittest.mock.patch.object(sys, "stdin", io.StringIO("   ")):
        ok("CLI empty stdin → rc 2 (use --empty for a real empty day)", wcc.main([]) == 2)
    ok("CLI invalid JSON → rc 2", wcc.main(["--events-json", "{not json"]) == 2)
    ok("CLI non-list JSON → rc 2", wcc.main(["--events-json", '{"raw": "x"}']) == 2)


def test_nonempty_payload_no_usable_events_preserves_prior() -> None:
    """Honesty guard (#2256 CR): a non-empty payload that normalizes to zero events
    must NOT certify a verified-empty day — it fails nonzero and leaves any prior
    cache untouched. A Google-API-shaped item (no `raw` key) is the canonical case."""
    google_shaped = [{"summary": "Owner 1:1", "start": {"dateTime": "2026-07-24T09:00:00-07:00"}}]
    # (a) with a prior good cache: the bad write must not clobber it.
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "state" / "calendar-today.json"
        prior = {"date": TODAY, "events": [{"raw": "9am Standup", "calendar": "work"}]}
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps(prior))
        with unittest.mock.patch.object(wcc, "cache_path", lambda: cache):
            rc = wcc.main(["--events-json", json.dumps(google_shaped)])
        ok("guard: non-empty→zero-usable payload exits nonzero (no false-clear)", rc != 0, f"rc={rc}")
        ok("guard: prior cache left untouched (not overwritten to events:[])",
           json.loads(cache.read_text()) == prior, cache.read_text())
    # (b) with no prior cache: nothing is written (no blank verified-empty file).
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "state" / "calendar-today.json"
        with unittest.mock.patch.object(wcc, "cache_path", lambda: cache):
            rc = wcc.main(["--events-json", json.dumps(google_shaped)])
        ok("guard: no prior cache → nothing written, still nonzero",
           rc != 0 and not cache.exists(), f"rc={rc} exists={cache.exists()}")


def test_cache_path_default_location() -> None:
    """The real (un-mocked) cache_path resolves under <workspace>/state/."""
    p = wcc.cache_path()
    ok("cache_path → <workspace>/state/calendar-today.json",
       p.name == "calendar-today.json" and p.parent.name == "state", str(p))


test_normalize_mixed_shapes()
test_write_schema_and_atomic()
test_empty_is_verified_empty()
test_producer_feeds_reader_roundtrip()
test_stale_cache_ignored()
test_cli_empty_flag()
test_cli_events_json_flag()
test_cli_stdin_events()
test_cli_error_paths()
test_nonempty_payload_no_usable_events_preserves_prior()
test_cache_path_default_location()

# --- `--from-gws`: the calendar is UNKNOWN on failure, never empty ------------
# The briefing reported "couldn't read your calendar" on this host from
# 2026-07-30 to 2026-08-04 because the only documented producer was an
# agent-only connector and nothing invoked it. `gws` could reach the calendar
# the whole time. These pin the property that makes the new source safe to run
# unattended: a fetch that fails must leave the cache alone and exit nonzero.

def _fake_gws(responses):
    """responses: list of (returncode, stdout) consumed in call order."""
    calls = {"n": 0}
    def run(argv, capture_output=True, text=True):
        i = calls["n"]; calls["n"] += 1
        if i >= len(responses):
            raise AssertionError(f"unexpected extra gws call: {argv}")
        rc, out = responses[i]
        if out is FileNotFoundError:
            raise FileNotFoundError("gws")
        return unittest.mock.Mock(returncode=rc, stdout=out, stderr="")
    return run, calls


CAL_LIST = json.dumps({"items": [
    {"id": "a@x.com", "selected": True}, {"id": "b@x.com", "selected": True}]})


def test_from_gws_binary_missing_raises():
    run, _ = _fake_gws([(0, FileNotFoundError)])
    with unittest.mock.patch("subprocess.run", run):
        try:
            wcc.events_from_gws()
            ok("gws missing raises", False, "returned instead of raising")
        except wcc.GwsUnavailable:
            ok("gws missing raises GwsUnavailable", True)


def test_from_gws_nonzero_raises():
    run, _ = _fake_gws([(1, "boom")])
    with unittest.mock.patch("subprocess.run", run):
        try:
            wcc.events_from_gws(); ok("gws nonzero raises", False)
        except wcc.GwsUnavailable: ok("gws nonzero exit raises", True)


def test_from_gws_api_error_object_raises():
    run, _ = _fake_gws([(0, json.dumps({"error": {"code": 403, "message": "denied"}}))])
    with unittest.mock.patch("subprocess.run", run):
        try:
            wcc.events_from_gws(); ok("gws error object raises", False)
        except wcc.GwsUnavailable: ok("gws API error object raises", True)


def test_from_gws_partial_failure_is_total_failure():
    """One calendar OK, the next fails -> raise. A subset is the falsely-clear bug."""
    first_cal = json.dumps({"items": [{"summary": "standup",
                                       "start": {"dateTime": "2026-08-04T09:00:00-07:00"},
                                       "end": {"dateTime": "2026-08-04T09:15:00-07:00"}}]})
    run, _ = _fake_gws([(0, CAL_LIST), (0, first_cal), (1, "second calendar exploded")])
    with unittest.mock.patch("subprocess.run", run):
        try:
            wcc.events_from_gws()
            ok("partial calendar failure raises", False,
               "returned the survivors — a 1-event day that is really 2 calendars deep")
        except wcc.GwsUnavailable:
            ok("partial calendar failure raises (subset never written)", True)


def test_from_gws_all_answered_zero_events_is_verified_empty():
    """The ONE case this source may certify empty: every calendar answered."""
    empty = json.dumps({"items": []})
    run, _ = _fake_gws([(0, CAL_LIST), (0, empty), (0, empty)])
    with unittest.mock.patch("subprocess.run", run):
        ok("all calendars answered, zero events -> [] (verified empty)",
           wcc.events_from_gws() == [])


def test_from_gws_cli_failure_leaves_prior_cache_untouched():
    """The durability property: yesterday's cache must survive a failed fetch."""
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "calendar-today.json"
        cache.write_text(json.dumps({"date": "1999-01-01", "events": [{"raw": "OLD", "calendar": ""}]}))
        run, _ = _fake_gws([(0, FileNotFoundError)])
        with unittest.mock.patch("subprocess.run", run), \
             unittest.mock.patch.object(wcc, "cache_path", lambda: cache):
            rc = wcc.main(["--from-gws"])
        ok("--from-gws failure exits nonzero", rc != 0, f"rc={rc}")
        after = json.loads(cache.read_text())
        ok("--from-gws failure leaves the prior cache byte-identical",
           after["events"] == [{"raw": "OLD", "calendar": ""}] and after["date"] == "1999-01-01",
           f"cache was rewritten to {after}")


def test_event_to_raw_shapes():
    ev = {"summary": "1:1", "start": {"dateTime": "2026-08-04T07:30:00-07:00"},
          "end": {"dateTime": "2026-08-04T08:00:00-07:00"}}
    ok("timed event renders as a time range", wcc.event_to_raw(ev) == "7:30am-8am 1:1",
       wcc.event_to_raw(ev))
    allday = {"summary": "Holiday", "start": {"date": "2026-08-04"}, "end": {"date": "2026-08-05"}}
    ok("all-day event says all-day (no fake time)", wcc.event_to_raw(allday) == "all-day Holiday",
       wcc.event_to_raw(allday))
    ok("no summary is labelled, not blank", "(no title)" in wcc.event_to_raw(
        {"start": {"date": "2026-08-04"}}))
    dec = dict(ev, attendees=[{"self": True, "responseStatus": "declined"}])
    ok("a DECLINED invite is MARKED, not dropped", wcc.event_to_raw(dec).endswith("[DECLINED]"),
       wcc.event_to_raw(dec))
    acc = dict(ev, attendees=[{"self": True, "responseStatus": "accepted"}])
    ok("an accepted invite is NOT marked declined", "[DECLINED]" not in wcc.event_to_raw(acc))


test_from_gws_binary_missing_raises()
test_from_gws_nonzero_raises()
test_from_gws_api_error_object_raises()
test_from_gws_partial_failure_is_total_failure()
test_from_gws_all_answered_zero_events_is_verified_empty()
test_from_gws_cli_failure_leaves_prior_cache_untouched()
test_event_to_raw_shapes()

print()
if _failed:
    print(f"FAIL — {_failed} of {_passed + _failed}")
    sys.exit(1)
print(f"PASS — {_passed}/{_passed} write-calendar-cache producer tests")
sys.exit(0)
