#!/usr/bin/env python3
"""Offline unit tests for the meeting-scheduler helper.

Covers arg-parsing side-effect-freeness, when-parsing, conflict/overlap logic,
dedup, and the name→email pick heuristic — all WITHOUT touching the network
(no `gws` calls are made; only the pure functions + argparse are exercised).

Run: python3 tests/meeting-scheduler.test.py
"""
import datetime as dt
import importlib.util
from pathlib import Path

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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS — {fn.__name__}")
    print(f"\nALL PASS — {len(fns)} tests")
