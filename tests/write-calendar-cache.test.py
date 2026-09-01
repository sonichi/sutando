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


def test_missing_start_warns_and_present_start_is_quiet() -> None:
    """A payload with no `start` silently costs the briefing two lines, so the
    producer says so; a payload that carries one must stay quiet."""
    with tempfile.TemporaryDirectory() as td:
        for label, events, want in (
            ("no start", [{"raw": "09:00 Standup", "calendar": "primary"}], True),
            ("with start", [{"raw": "09:00 Standup", "calendar": "primary",
                             "start": "2026-08-25T09:00:00-07:00"}], False),
        ):
            err = io.StringIO()
            with unittest.mock.patch("sys.stderr", err):
                wcc.write_cache(events, path=Path(td) / f"{want}.json")
            warned = "carry no" in err.getvalue()
            ok(f"write_cache warns iff start missing ({label})", warned is want,
               f"stderr={err.getvalue()!r}")
        # The count must name the events that lack a start, not the whole list.
        err = io.StringIO()
        with unittest.mock.patch("sys.stderr", err):
            wcc.write_cache([{"raw": "A", "calendar": "", "start": "2026-08-25T09:00:00-07:00"},
                             {"raw": "B", "calendar": ""}], path=Path(td) / "mixed.json")
        ok("warning counts only the events missing a start",
           "1 of 2 event(s)" in err.getvalue(), err.getvalue())


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
test_missing_start_warns_and_present_start_is_quiet()
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


def _ev(summary, hh):
    return {"summary": summary,
            "start": {"dateTime": f"2026-08-04T{hh}:00:00-07:00"},
            "end": {"dateTime": f"2026-08-04T{hh}:30:00-07:00"}}


def test_from_gws_drains_calendar_list_pages():
    """A second PAGE of calendars must not vanish.

    `calendarList` returns 100 entries per page; beyond that Google sets
    `nextPageToken` and the request must be repeated. Reading page 1 only drops
    every later calendar silently — the day comes back short, exit 0, and gets
    written as authoritative. This control FAILS on the unpaginated code, which
    never issues the second calendarList request.
    """
    page1 = json.dumps({"items": [{"id": "a@x.com", "selected": True}], "nextPageToken": "CAL2"})
    page2 = json.dumps({"items": [{"id": "b@x.com", "selected": True}]})
    run, calls = _fake_gws([(0, page1), (0, page2),
                            (0, json.dumps({"items": [_ev("standup", "09")]})),
                            (0, json.dumps({"items": [_ev("retro", "14")]}))])
    with unittest.mock.patch("subprocess.run", run):
        got = wcc.events_from_gws()
    cals = {e["calendar"] for e in got}
    ok("a SECOND page of calendars is drained, not dropped",
       len(got) == 2 and cals == {"a@x.com", "b@x.com"},
       f"{len(got)} event(s) from {sorted(cals)} after {calls['n']} gws calls")


def test_from_gws_drains_event_pages():
    """Same for events: 250/page, then `nextPageToken`. FAILS unpaginated."""
    cal = json.dumps({"items": [{"id": "a@x.com", "selected": True}]})
    ev_page1 = json.dumps({"items": [_ev("standup", "09")], "nextPageToken": "EV2"})
    ev_page2 = json.dumps({"items": [_ev("retro", "14")]})
    run, _ = _fake_gws([(0, cal), (0, ev_page1), (0, ev_page2)])
    with unittest.mock.patch("subprocess.run", run):
        got = wcc.events_from_gws()
    ok("a SECOND page of events is drained, not dropped",
       len(got) == 2 and any("retro" in e["raw"] for e in got),
       f"got {[e['raw'] for e in got]}")


def test_from_gws_error_on_a_later_page_is_total_failure():
    """Page 1 fine, page 2 errors -> raise. A truncated day must never be written."""
    cal = json.dumps({"items": [{"id": "a@x.com", "selected": True}]})
    ev_page1 = json.dumps({"items": [_ev("standup", "09")], "nextPageToken": "EV2"})
    run, _ = _fake_gws([(0, cal), (0, ev_page1), (1, "page 2 exploded")])
    with unittest.mock.patch("subprocess.run", run):
        try:
            got = wcc.events_from_gws()
            ok("an error on page 2 raises", False,
               f"returned {len(got)} survivor(s) — a partial day written as whole")
        except wcc.GwsUnavailable:
            ok("an error on a LATER page is total failure (no partial day)", True)


def test_from_gws_repeated_page_token_refuses_to_loop():
    """A server echoing the same token must terminate as a failure, not spin."""
    page = json.dumps({"items": [{"id": "a@x.com", "selected": True}], "nextPageToken": "SAME"})
    run, _ = _fake_gws([(0, page)] * 6)
    with unittest.mock.patch("subprocess.run", run):
        try:
            wcc.events_from_gws()
            ok("a repeated pageToken raises", False, "drained forever or returned a partial list")
        except wcc.GwsUnavailable:
            ok("a repeated pageToken raises instead of looping", True)


def test_gws_strips_a_keyring_banner_before_the_json():
    """Some hosts print a keyring banner ahead of the payload; the parser must skip it.

    Both halves: a banner followed by JSON is recoverable, a banner with NO JSON at all
    is not and must raise rather than return an empty day.
    """
    run, _ = _fake_gws([(0, 'Unlocking keyring...\n{"items": []}')])
    with unittest.mock.patch("subprocess.run", run):
        ok("a keyring banner before the JSON is stripped",
           wcc._gws(["calendar", "calendarList", "list"]) == {"items": []})
    run, _ = _fake_gws([(0, "Unlocking keyring... no payload at all")])
    with unittest.mock.patch("subprocess.run", run):
        try:
            wcc._gws(["calendar", "x"]); ok("banner with NO json raises", False)
        except wcc.GwsUnavailable:
            ok("a banner with NO json at all raises (never an empty day)", True)


def test_gws_unparseable_json_raises():
    run, _ = _fake_gws([(0, "{not: valid json,,,")])
    with unittest.mock.patch("subprocess.run", run):
        try:
            wcc._gws(["calendar", "x"]); ok("unparseable JSON raises", False)
        except wcc.GwsUnavailable:
            ok("unparseable JSON raises rather than yielding no events", True)


def test_paging_rejects_a_non_object_page_and_an_endless_pager():
    """Two ways a pager can be wrong that are NOT an error response."""
    run, _ = _fake_gws([(0, "[1, 2, 3]")])          # a LIST where an object is required
    with unittest.mock.patch("subprocess.run", run):
        try:
            wcc._gws_all_pages(["calendar", "events", "list"]); ok("non-object page raises", False)
        except wcc.GwsUnavailable:
            ok("a non-object page raises instead of being treated as zero items", True)
    endless = json.dumps({"items": [], "nextPageToken": "T"})
    run, _ = _fake_gws([(0, endless)] * 60)
    with unittest.mock.patch("subprocess.run", run):
        try:
            wcc._gws_all_pages(["calendar", "events", "list"], max_pages=3)
            ok("an endless pager raises", False)
        except wcc.GwsUnavailable:
            ok("an endless pager stops at max_pages and REFUSES a partial read", True)


def test_no_selected_calendars_refuses_to_certify():
    run, _ = _fake_gws([(0, json.dumps({"items": [{"id": "a@x.com", "selected": False}]}))])
    with unittest.mock.patch("subprocess.run", run):
        try:
            wcc.events_from_gws(); ok("zero SELECTED calendars raises", False)
        except wcc.GwsUnavailable:
            ok("zero SELECTED calendars raises — an unread day is not an empty one", True)


def test_cancelled_events_are_dropped_and_location_is_rendered():
    cal = json.dumps({"items": [{"id": "a@x.com", "selected": True}]})
    evs = json.dumps({"items": [
        dict(_ev("standup", "09"), location="Room 4\nBuilding B"),
        dict(_ev("ghost", "10"), status="cancelled"),
    ]})
    run, _ = _fake_gws([(0, cal), (0, evs)])
    with unittest.mock.patch("subprocess.run", run):
        got = wcc.events_from_gws()
    ok("a cancelled event is dropped", len(got) == 1, f"{[e['raw'] for e in got]}")
    ok("a location renders on ONE line (a multi-line address must not break the raw)",
       got and "@ Room 4" in got[0]["raw"] and "\n" not in got[0]["raw"], got[0]["raw"] if got else "")


def test_from_gws_success_path_writes_the_cache_and_exits_zero():
    """The happy path of `main --from-gws` — previously only its failure path ran."""
    cal = json.dumps({"items": [{"id": "a@x.com", "selected": True}]})
    evs = json.dumps({"items": [_ev("standup", "09")]})
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "calendar-today.json"
        run, _ = _fake_gws([(0, cal), (0, evs)])
        with unittest.mock.patch("subprocess.run", run), \
             unittest.mock.patch.object(wcc, "cache_path", lambda: cache):
            rc = wcc.main(["--from-gws"])
        ok("--from-gws succeeds and exits 0", rc == 0, f"rc={rc}")
        ok("--from-gws actually wrote the cache", cache.exists())
        if cache.exists():
            d = json.loads(cache.read_text())
            ok("the written cache carries the event and today's date",
               len(d.get("events", [])) == 1 and d.get("date"), str(d)[:90])


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


def test_unrendered_api_object_is_refused():
    """A caller that pipes the calendar API event straight through must fail loudly.

    Observed 2026-08-09: `str()` on the API dict yields its repr, which passed the
    non-empty check and was delivered as
    `One meeting today: {'id': '35s817...', 'status': 'confirmed', ...}`.
    """
    api_event = {"id": "35s817abc", "status": "confirmed", "summary": "Standup",
                 "start": {"dateTime": "2026-08-20T08:30:00-07:00"},
                 "end": {"dateTime": "2026-08-20T09:30:00-07:00"}}
    raised = False
    try:
        wcc.normalize_events([{"raw": api_event, "calendar": "work"}])
    except TypeError:
        raised = True
    ok("an unrendered API dict as `raw` raises instead of shipping its repr", raised)

    raised_list = False
    try:
        wcc.normalize_events([{"raw": [api_event], "calendar": "work"}])
    except TypeError:
        raised_list = True
    ok("a LIST of events as `raw` is refused too", raised_list)

    # The rendered form the real producer emits must still pass untouched.
    rendered = wcc.event_to_raw(api_event)
    out = wcc.normalize_events([{"raw": rendered, "calendar": "work"}])
    ok("the rendered string the producer emits still normalizes",
       out == [{"raw": rendered, "calendar": "work"}], out)
    ok("and it is a real display string, not a repr", "8:30am" in rendered and "{" not in rendered,
       rendered)


def test_outer_element_shape_is_enforced():
    """The sibling branch: a non-dict OUTER element was `str()`-ified unchecked.

    The dict branch guarded `raw`, so the same connector object shipped its repr
    by arriving one level out — `[[api_event]]` — or as a bare scalar.
    """
    api_event = {"id": "35s817abc", "status": "confirmed", "summary": "Standup",
                 "start": {"dateTime": "2026-08-20T08:30:00-07:00"}}
    for label, payload in (
        ("a nested connector list", [[api_event]]),
        ("a bare dict-free scalar (int)", [7]),
        ("a bare bool", [True]),
    ):
        raised = False
        try:
            wcc.normalize_events(payload)
        except TypeError:
            raised = True
        ok(f"{label} as an outer element is refused", raised)

    for label, payload in (
        ("`raw`", [{"raw": 7}]),
        ("`calendar`", [{"raw": "8:30am Standup", "calendar": {"id": "x"}}]),
        ("`start`", [{"raw": "8:30am Standup", "start": [1]}]),
    ):
        raised = False
        try:
            wcc.normalize_events(payload)
        except TypeError:
            raised = True
        ok(f"a non-string {label} is refused", raised)

    # Both documented shapes, and the drop-the-blanks behaviour, must survive.
    out = wcc.normalize_events(["9am Standup", {"raw": "12:30 Sync", "calendar": "work"},
                                {"raw": "  "}, {}, "  "])
    ok("documented string+dict elements still normalize, blanks still dropped",
       out == [{"raw": "9am Standup", "calendar": ""},
               {"raw": "12:30 Sync", "calendar": "work"}], out)


def test_cli_refuses_nested_connector_list_and_keeps_prior_cache():
    """CLI level: the refusal must exit nonzero, not traceback, and certify nothing."""
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "calendar-today.json"
        prior = json.dumps({"date": "1999-01-01", "events": [{"raw": "OLD", "calendar": ""}]})
        cache.write_text(prior)
        nested = json.dumps([[{"id": "35s817abc", "status": "confirmed", "summary": "Standup"}]])
        err = io.StringIO()
        with unittest.mock.patch.object(wcc, "cache_path", lambda: cache), \
             unittest.mock.patch("sys.stderr", err):
            rc = wcc.main(["--events-json", nested])
        ok("a nested connector list exits nonzero at the CLI", rc != 0, f"rc={rc}")
        ok("and it does so by message, not an uncaught traceback",
           "must be a display string" in err.getvalue(), err.getvalue())
        ok("and the prior cache is left byte-identical",
           cache.read_text() == prior, f"cache was rewritten to {cache.read_text()}")


test_from_gws_binary_missing_raises()
test_from_gws_nonzero_raises()
test_from_gws_api_error_object_raises()
test_from_gws_partial_failure_is_total_failure()
test_from_gws_all_answered_zero_events_is_verified_empty()
test_from_gws_drains_calendar_list_pages()
test_from_gws_drains_event_pages()
test_from_gws_error_on_a_later_page_is_total_failure()
test_from_gws_repeated_page_token_refuses_to_loop()
test_gws_strips_a_keyring_banner_before_the_json()
test_gws_unparseable_json_raises()
test_paging_rejects_a_non_object_page_and_an_endless_pager()
test_no_selected_calendars_refuses_to_certify()
test_cancelled_events_are_dropped_and_location_is_rendered()
test_from_gws_success_path_writes_the_cache_and_exits_zero()
test_from_gws_cli_failure_leaves_prior_cache_untouched()
test_event_to_raw_shapes()
test_unrendered_api_object_is_refused()
test_outer_element_shape_is_enforced()
test_cli_refuses_nested_connector_list_and_keeps_prior_cache()

print()
if _failed:
    print(f"FAIL — {_failed} of {_passed + _failed}")
    sys.exit(1)
print(f"PASS — {_passed}/{_passed} write-calendar-cache producer tests")
sys.exit(0)
