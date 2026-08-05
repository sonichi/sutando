#!/usr/bin/env python3
"""Offline unit tests for the meeting-scheduler helper.

Covers arg-parsing side-effect-freeness, when-parsing, conflict/overlap logic,
dedup, and the name→email pick heuristic — all WITHOUT touching the network
(no `gws` calls are made; only the pure functions + argparse are exercised).

Run: python3 tests/meeting-scheduler.test.py
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import subprocess
import types
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "meeting-scheduler" / "scripts" / "schedule_meeting.py"


def _load_module():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    spec = importlib.util.spec_from_file_location("schedule_meeting", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()


def test_parse_when():
    assert M.parse_when("2026-07-25T15:00") == dt.datetime(2026, 7, 25, 15, 0)
    assert M.parse_when("2026-07-25 09:30") == dt.datetime(2026, 7, 25, 9, 30)
    now = dt.datetime(2026, 7, 24, 8, 0)
    assert M.parse_when("tomorrow 3pm", now=now) == dt.datetime(2026, 7, 25, 15, 0)
    assert M.parse_when("today 14:00", now=now) == dt.datetime(2026, 7, 24, 14, 0)
    assert M.parse_when("tomorrow 12am", now=now) == dt.datetime(2026, 7, 25, 0, 0)
    for bad in ("gibberish", "next tuesday-ish", ""):
        try:
            M.parse_when(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass


def test_compute_end():
    start = dt.datetime(2026, 7, 25, 15, 0)
    assert M.compute_end(start, 30) == dt.datetime(2026, 7, 25, 15, 30)
    assert M.compute_end(start, 60) == dt.datetime(2026, 7, 25, 16, 0)


def test_find_conflicts():
    start = dt.datetime(2026, 7, 25, 15, 0)
    end = dt.datetime(2026, 7, 25, 15, 30)
    events = [
        {"summary": "Overlap",
         "start": {"dateTime": "2026-07-25T15:15:00-07:00"},
         "end": {"dateTime": "2026-07-25T15:45:00-07:00"}},
        {"summary": "Earlier",
         "start": {"dateTime": "2026-07-25T12:00:00-07:00"},
         "end": {"dateTime": "2026-07-25T13:00:00-07:00"}},
        {"summary": "Free-tagged", "transparency": "transparent",
         "start": {"dateTime": "2026-07-25T15:05:00-07:00"},
         "end": {"dateTime": "2026-07-25T15:20:00-07:00"}},
        {"summary": "Cancelled", "status": "cancelled",
         "start": {"dateTime": "2026-07-25T15:05:00-07:00"},
         "end": {"dateTime": "2026-07-25T15:20:00-07:00"}},
        {"summary": "All-day", "start": {"date": "2026-07-25"},
         "end": {"date": "2026-07-26"}},
    ]
    conflicts = M.find_conflicts(events, start, end)
    assert [c["summary"] for c in conflicts] == ["Overlap"], conflicts

    # back-to-back (half-open) is NOT a conflict, on either side
    before = [{"summary": "b", "start": {"dateTime": "2026-07-25T14:30:00-07:00"},
               "end": {"dateTime": "2026-07-25T15:00:00-07:00"}}]
    after = [{"summary": "a", "start": {"dateTime": "2026-07-25T15:30:00-07:00"},
              "end": {"dateTime": "2026-07-25T16:00:00-07:00"}}]
    assert M.find_conflicts(before, start, end) == []
    assert M.find_conflicts(after, start, end) == []


def test_find_duplicates():
    events = [
        {"summary": "Sync", "start": {"dateTime": "2026-07-25T09:00:00-07:00"}},
        {"summary": "  SYNC  ", "start": {"dateTime": "2026-07-25T16:00:00-07:00"}},
        {"summary": "Cancelled dup", "status": "cancelled"},
        {"summary": "Unrelated"},
    ]
    dups = M.find_duplicates(events, "sync")
    assert len(dups) == 2, dups  # case + whitespace insensitive, both non-cancelled
    assert M.find_duplicates(events, "Brand New Title") == []
    # a cancelled same-title event must not count
    cancelled_only = [{"summary": "Sync", "status": "cancelled"}]
    assert M.find_duplicates(cancelled_only, "Sync") == []


def test_pick_email_for_name():
    headers = [
        {"from": "Alice Adams <alice@example.com>", "to": "Dana <dana@x.com>"},
        {"from": "Random Person <noise@example.com>", "to": "alice@example.com"},
    ]
    picked = M.pick_email_for_name(headers, "Alice")
    assert picked["email"] == "alice@example.com", picked
    # nothing found → None (never a guess)
    assert M.pick_email_for_name([], "Nobody")["email"] is None
    # display-name token match beats a random address
    mixed = [{"from": "Bob Brown <bob@team.com>", "cc": "misc@team.com"}]
    assert M.pick_email_for_name(mixed, "Bob")["email"] == "bob@team.com"


def test_pick_email_fails_closed_on_no_match():
    # (a) headers exist but NONE of them match the requested name → the contract
    # is "None (never a guess)". Regression for the fail-OPEN bug where any
    # From/To/Cc address was returned as a "best" guess even with zero matches.
    nonmatch = [
        {"from": "Carol Manager <carol@example.com>", "to": "Frank Ops <frank@example.com>"},
        {"from": "Eve North <eve@example.com>"},
    ]
    got = M.pick_email_for_name(nonmatch, "Alice Example")
    assert got["email"] is None, got  # would have wrongly returned carol@/frank@/eve@


def test_pick_email_ambiguous_tie_not_auto_picked():
    # (b) two addresses tie at the top score → ambiguous. We must NOT auto-pick
    # one (that could email the invite to the wrong Dana); email stays None so
    # main() leaves the name unresolved and refuses to --send without --force.
    tie = [
        {"from": "Dana Green <dana.green@example.com>",
         "to": "Dana Blue <dana.blue@example.com>"},
    ]
    got = M.pick_email_for_name(tie, "Dana")
    assert got["email"] is None, got
    assert got.get("ambiguous") is True, got
    assert {c["email"] for c in got.get("candidates", [])} == {
        "dana.green@example.com", "dana.blue@example.com"}, got


def test_tz_offset_is_date_aware():
    # Regression: the offset must be computed at the TARGET date, not 'now'.
    # A summer LA date is PDT (-07:00); a winter LA date is PST (-08:00). The
    # old code computed both at 'now', so a December query built during July
    # emitted -07:00 and started the conflict window an hour late.
    summer = dt.datetime(2026, 7, 15, 0, 0)
    winter = dt.datetime(2026, 12, 15, 0, 0)
    assert M._tz_offset("America/Los_Angeles", summer) == "-07:00"
    assert M._tz_offset("America/Los_Angeles", winter) == "-08:00"


def test_argparse_is_side_effect_free():
    # --self-check and --help must not require --title/--when and must not hit net.
    parser = M.build_parser()
    ns = parser.parse_args(["--self-check"])
    assert ns.self_check is True
    # dry-run is the default (no --send)
    ns2 = parser.parse_args(["--title", "T", "--when", "2026-07-25T15:00"])
    assert ns2.send is False
    # --send and --dry-run are mutually exclusive
    try:
        parser.parse_args(["--send", "--dry-run"])
        raise AssertionError("expected mutually-exclusive error")
    except SystemExit:
        pass


def test_main_missing_args_returns_2():
    # No title/when → exit 2, and crucially no network call happens.
    assert M.main(["--duration-min", "30"]) == 2


def test_self_check_passes():
    assert M.self_check() == 0


# --------------------------------------------------------------------------- #
# gws plumbing — all subprocess calls are mocked; NO real `gws`, NO network.   #
# --------------------------------------------------------------------------- #
def _fake_proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _silent():
    """Swallow main()'s stdout so the test runner output stays readable."""
    return contextlib.redirect_stdout(io.StringIO())


def test_gws_env():
    ag2 = M._gws_env("ag2")
    assert ag2["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"].endswith("/.config/gws-ag2")
    assert "GOOGLE_WORKSPACE_CLI_CONFIG_DIR" not in M._gws_env("default")


def test_strip_keyring():
    noisy = 'keyring: unlocked\n{"ok": true}\nKEYRING backend ready'
    assert M._strip_keyring(noisy) == '{"ok": true}'


def test_run_gws_success_and_env():
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return _fake_proc(stdout='keyring: noise\n{"items": [1, 2]}')

    with mock.patch.object(M.subprocess, "run", fake_run):
        out = M.run_gws(["calendar", "events", "list"], "ag2")
    assert out == {"items": [1, 2]}
    assert captured["cmd"][0] == "gws"
    # ag2 account routes through its own config dir
    assert captured["env"]["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"].endswith("gws-ag2")


def test_run_gws_empty_output_is_empty_dict():
    with mock.patch.object(M.subprocess, "run", lambda cmd, **kw: _fake_proc(stdout="   ")):
        assert M.run_gws(["x"], "default") == {}


def test_run_gws_not_found():
    def boom(cmd, **kw):
        raise FileNotFoundError()

    with mock.patch.object(M.subprocess, "run", boom):
        try:
            M.run_gws(["x"], "default")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "gws` CLI not found" in str(e)


def test_run_gws_timeout():
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 60)

    with mock.patch.object(M.subprocess, "run", boom):
        try:
            M.run_gws(["gmail", "list"], "default", timeout=60)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "timed out" in str(e)


def test_run_gws_nonzero_exit():
    with mock.patch.object(
        M.subprocess, "run",
        lambda cmd, **kw: _fake_proc(returncode=2, stderr="boom", stdout=""),
    ):
        try:
            M.run_gws(["x"], "default")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "gws failed (exit 2)" in str(e)


def test_run_gws_bad_json():
    with mock.patch.object(
        M.subprocess, "run", lambda cmd, **kw: _fake_proc(stdout="not json {")
    ):
        try:
            M.run_gws(["x"], "default")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "could not parse gws JSON" in str(e)


def test_detect_timezone():
    with mock.patch.object(
        M.os, "readlink",
        lambda p: "/var/db/timezone/zoneinfo/America/New_York",
    ):
        assert M.detect_timezone() == "America/New_York"
    # unreadable /etc/localtime → owner's Pacific default
    def boom(p):
        raise OSError()

    with mock.patch.object(M.os, "readlink", boom):
        assert M.detect_timezone() == M.DEFAULT_TZ


def test_event_bounds_unparseable_returns_none():
    bad = {"start": {"dateTime": "not-a-date"}, "end": {"dateTime": "also-bad"}}
    assert M._event_bounds(bad) is None


def test_tz_offset_fallback_on_bad_zone():
    # An unknown IANA zone can't be resolved → fall back to 'Z' (UTC).
    assert M._tz_offset("Not/AZone", dt.datetime(2026, 1, 1)) == "Z"


# --------------------------------------------------------------------------- #
# resolve_names — run_gws mocked (no Gmail, no network).                        #
# --------------------------------------------------------------------------- #
def test_resolve_names_happy_path():
    def fake_gws(args, account, timeout=60):
        if "list" in args:
            return {"messages": [{"id": "m1"}, {"id": "m2"}]}
        # a 'get' for message metadata headers
        return {"payload": {"headers": [
            {"name": "From", "value": "Alice Adams <alice@example.com>"},
            {"name": "To", "value": "Bob <bob@example.com>"},
        ]}}

    with mock.patch.object(M, "run_gws", fake_gws):
        got = M.resolve_names(["Alice", "  "], "default", 365)
    assert len(got) == 1  # blank name skipped
    assert got[0]["email"] == "alice@example.com"


def test_resolve_names_list_error_records_error():
    def fake_gws(args, account, timeout=60):
        raise RuntimeError("gws exploded")

    with mock.patch.object(M, "run_gws", fake_gws):
        got = M.resolve_names(["Alice"], "default", 365, verbose=True)
    assert got[0]["email"] is None
    assert "gws exploded" in got[0]["error"]


def test_resolve_names_get_error_skips_message():
    def fake_gws(args, account, timeout=60):
        if "list" in args:
            return {"messages": [{"id": "m1"}]}
        raise RuntimeError("get failed")  # every metadata fetch fails

    with mock.patch.object(M, "run_gws", fake_gws):
        got = M.resolve_names(["Alice"], "default", 365)
    # no headers gathered → nothing to match → None (never a guess)
    assert got[0]["email"] is None


# --------------------------------------------------------------------------- #
# list_events_for_day + create_event — run_gws mocked; assert payloads.        #
# --------------------------------------------------------------------------- #
def test_list_events_for_day_builds_dst_correct_window():
    captured = {}

    def fake_gws(args, account, timeout=60):
        captured["args"] = args
        return {"items": [{"summary": "X"}]}

    with mock.patch.object(M, "run_gws", fake_gws):
        # July → PDT (-07:00)
        items = M.list_events_for_day(
            dt.datetime(2026, 7, 25, 15, 0), "default", "primary",
            "America/Los_Angeles", query="Sync")
    assert items == [{"summary": "X"}]
    params = json.loads(captured["args"][4])
    assert params["calendarId"] == "primary"
    assert params["timeMin"].endswith("-07:00")
    assert params["timeMax"].endswith("-07:00")
    assert params["q"] == "Sync"

    # December → PST (-08:00): the window offset tracks the target date, not now.
    with mock.patch.object(M, "run_gws", fake_gws):
        M.list_events_for_day(
            dt.datetime(2026, 12, 15, 15, 0), "default", "primary",
            "America/Los_Angeles")
    params = json.loads(captured["args"][4])
    assert params["timeMin"].endswith("-08:00")
    assert "q" not in params


def test_create_event_builds_payload():
    calls = []

    def fake_gws(args, account, timeout=60):
        calls.append(args)
        return {"htmlLink": "https://calendar.example/evt123"}

    start = dt.datetime(2026, 7, 25, 15, 0)
    end = dt.datetime(2026, 7, 25, 15, 30)
    with mock.patch.object(M, "run_gws", fake_gws):
        ev = M.create_event(
            "Sync", start, end, "America/Los_Angeles",
            ["a@x.com", "b@y.com"], "Room 1", "Agenda here",
            "default", "primary")
    assert ev["htmlLink"].endswith("evt123")
    args = calls[0]
    assert args[:3] == ["calendar", "events", "insert"]
    api_params = json.loads(args[4])
    assert api_params == {"calendarId": "primary", "sendUpdates": "all"}
    body = json.loads(args[6])
    assert body["summary"] == "Sync"
    assert body["start"] == {"dateTime": "2026-07-25T15:00:00", "timeZone": "America/Los_Angeles"}
    assert body["end"]["dateTime"] == "2026-07-25T15:30:00"
    assert body["attendees"] == [{"email": "a@x.com"}, {"email": "b@y.com"}]
    assert body["location"] == "Room 1"
    assert body["description"] == "Agenda here"


def test_create_event_omits_optional_fields():
    calls = []
    with mock.patch.object(
        M, "run_gws",
        lambda args, account, timeout=60: calls.append(args) or {"htmlLink": "x"},
    ):
        M.create_event(
            "Sync", dt.datetime(2026, 7, 25, 15, 0),
            dt.datetime(2026, 7, 25, 15, 30), "America/Los_Angeles",
            ["a@x.com"], None, None, "default", "primary")
    body = json.loads(calls[0][6])
    assert "location" not in body and "description" not in body


# --------------------------------------------------------------------------- #
# main() CLI flows — every network step patched; asserts on return codes and   #
# the payloads passed to create_event. NO real gws / calendar / email.         #
# --------------------------------------------------------------------------- #
_WHEN = "2026-07-25T15:00"


def test_main_self_check_flag():
    with _silent():
        assert M.main(["--self-check"]) == 0


def test_main_bad_when_returns_2():
    with _silent():
        assert M.main(["--title", "T", "--when", "not-a-time"]) == 2


def test_main_no_attendees_returns_2():
    # title + when present but no attendees and no --resolve → exit 2.
    with _silent():
        assert M.main(["--title", "T", "--when", _WHEN]) == 2


def test_main_dryrun_free_slot():
    # list_events_for_day → no events; dry-run returns 0 and never creates.
    with mock.patch.object(M, "list_events_for_day", lambda *a, **k: []), \
            mock.patch.object(M, "create_event",
                              mock.Mock(side_effect=AssertionError("must not create"))), \
            _silent():
        rc = M.main(["--title", "Sync", "--when", _WHEN,
                     "--attendees", "a@x.com,a@x.com,b@y.com",
                     "--location", "Room 1"])
    assert rc == 0


def test_main_dryrun_resolves_names():
    resolved = [
        {"name": "Alice", "email": "alice@example.com",
         "alternates": [{"email": "alice2@example.com"}]},
        {"name": "Ghost", "email": None, "alternates": [], "error": "no match"},
    ]
    with mock.patch.object(M, "resolve_names", lambda *a, **k: resolved), \
            mock.patch.object(M, "list_events_for_day", lambda *a, **k: []), \
            _silent():
        rc = M.main(["--title", "Sync", "--when", _WHEN, "--resolve", "Alice, Ghost"])
    # Alice resolved → we have >=1 attendee → dry-run reports and returns 0.
    assert rc == 0


def test_main_calendar_read_failure_returns_1():
    with mock.patch.object(M, "list_events_for_day",
                           mock.Mock(side_effect=RuntimeError("calendar down"))), \
            _silent():
        rc = M.main(["--title", "Sync", "--when", _WHEN, "--attendees", "a@x.com"])
    assert rc == 1


def _conflict_event():
    return {"summary": "Standup",
            "start": {"dateTime": "2026-07-25T15:15:00-07:00"},
            "end": {"dateTime": "2026-07-25T15:45:00-07:00"}}


def _dup_event(title="Sync"):
    return {"summary": title,
            "start": {"dateTime": "2026-07-25T09:00:00-07:00"},
            "htmlLink": "https://cal/existing"}


def test_main_send_refuses_on_conflict():
    with mock.patch.object(M, "list_events_for_day", lambda *a, **k: [_conflict_event()]), \
            mock.patch.object(M, "create_event",
                              mock.Mock(side_effect=AssertionError("must not create"))), \
            _silent():
        rc = M.main(["--title", "Sync", "--when", _WHEN,
                     "--attendees", "a@x.com", "--send"])
    assert rc == 3  # blocked by conflict, no --force


def test_main_send_refuses_on_unresolved_name():
    resolved = [{"name": "Ghost", "email": None, "alternates": []}]
    with mock.patch.object(M, "resolve_names", lambda *a, **k: resolved), \
            mock.patch.object(M, "list_events_for_day", lambda *a, **k: []), \
            _silent():
        # give one explicit email so we pass the "no attendees" guard, but the
        # unresolved name still blocks --send.
        rc = M.main(["--title", "Sync", "--when", _WHEN,
                     "--attendees", "real@x.com", "--resolve", "Ghost", "--send"])
    assert rc == 3


def test_main_send_creates_when_clear():
    created = {}

    def fake_create(title, start, end, tz, emails, location, description, account, calendar):
        created.update(title=title, emails=emails, tz=tz)
        return {"htmlLink": "https://calendar.example/new"}

    with mock.patch.object(M, "list_events_for_day", lambda *a, **k: []), \
            mock.patch.object(M, "create_event", fake_create), \
            _silent():
        rc = M.main(["--title", "Sync", "--when", _WHEN,
                     "--attendees", "a@x.com", "--send"])
    assert rc == 0
    assert created["title"] == "Sync"
    assert created["emails"] == ["a@x.com"]


def test_main_send_force_overrides_dup_and_creates():
    created = {}

    def fake_create(*a, **k):
        created["called"] = True
        return {"htmlLink": "https://calendar.example/forced"}

    with mock.patch.object(M, "list_events_for_day", lambda *a, **k: [_dup_event("Sync")]), \
            mock.patch.object(M, "create_event", fake_create), \
            _silent():
        rc = M.main(["--title", "Sync", "--when", _WHEN,
                     "--attendees", "a@x.com", "--send", "--force"])
    assert rc == 0
    assert created.get("called") is True


def test_main_send_create_failure_returns_1():
    with mock.patch.object(M, "list_events_for_day", lambda *a, **k: []), \
            mock.patch.object(M, "create_event",
                              mock.Mock(side_effect=RuntimeError("insert failed"))), \
            _silent():
        rc = M.main(["--title", "Sync", "--when", _WHEN,
                     "--attendees", "a@x.com", "--send"])
    assert rc == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS — {fn.__name__}")
    print(f"\nALL PASS — {len(fns)} tests")
