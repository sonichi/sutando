#!/usr/bin/env python3
"""Direct coverage for skills/meeting-scheduler/scripts/policy.py.

Pure policy, so every case is in-memory — no `gws`, no Google Workspace, no
network, no filesystem. The import itself is part of the contract: this module
must never reach for IO, and must never be able to create or send an event.
"""
import datetime as dt
import importlib.util
import io
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
POLICY = REPO / "skills" / "meeting-scheduler" / "scripts" / "policy.py"

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILS.append(label)


print("── purity ──")
_argv, sys.argv = sys.argv, ["policy-purity-probe", "--send", "--force"]
out, err = io.StringIO(), io.StringIO()
try:
    with redirect_stdout(out), redirect_stderr(err):
        spec = importlib.util.spec_from_file_location("ms_policy", POLICY)
        P = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(P)
finally:
    sys.argv = _argv
check("importing the policy prints nothing", out.getvalue() == "" and err.getvalue() == "",
      f"stdout={out.getvalue()!r}")
body = POLICY.read_text().split('"""', 2)[-1]   # strip docstring; it names what we avoid
for token, why in (("subprocess", "shells out"), ("run_gws", "calls gws"),
                   ("create_event", "can create an event"), ("requests", "does network IO"),
                   ("open(", "opens a file")):
    check(f"policy never {why}", token not in body, f"found {token!r}")

NOW = dt.datetime(2026, 8, 3, 9, 0)   # a Monday

print("── parse_when ──")
check("'tomorrow 3pm' resolves to the next day at 15:00",
      P.parse_when("tomorrow 3pm", NOW) == dt.datetime(2026, 8, 4, 15, 0),
      str(P.parse_when("tomorrow 3pm", NOW)))
check("an explicit date is honoured",
      P.parse_when("2026-08-05 14:00", NOW) == dt.datetime(2026, 8, 5, 14, 0),
      str(P.parse_when("2026-08-05 14:00", NOW)))
check("parse_when returns a NAIVE datetime (wall-clock contract)",
      P.parse_when("tomorrow 3pm", NOW).tzinfo is None)
raised = False
try:
    P.parse_when("bogus input", NOW)
except Exception:
    raised = True
check("unparseable input raises rather than guessing a time", raised)

print("── compute_end ──")
check("duration is added in minutes",
      P.compute_end(NOW, 30) == dt.datetime(2026, 8, 3, 9, 30))
check("a long duration crosses the hour correctly",
      P.compute_end(NOW, 90) == dt.datetime(2026, 8, 3, 10, 30))
check("end is never before start for a 0 duration",
      P.compute_end(NOW, 0) == NOW)

TIMED = {"summary": "Standup",
         "start": {"dateTime": "2026-08-03T09:00:00-07:00"},
         "end": {"dateTime": "2026-08-03T09:15:00-07:00"}}
ALLDAY = {"summary": "OOO", "start": {"date": "2026-08-03"}, "end": {"date": "2026-08-04"}}
FREE = {"summary": "Free", "transparency": "transparent",
        "start": {"dateTime": "2026-08-03T09:00:00-07:00"},
        "end": {"dateTime": "2026-08-03T10:00:00-07:00"}}
CANCELLED = {"summary": "Cancelled", "status": "cancelled",
             "start": {"dateTime": "2026-08-03T09:00:00-07:00"},
             "end": {"dateTime": "2026-08-03T10:00:00-07:00"}}
MALFORMED = {"summary": "Malformed"}

print("── _event_bounds / _is_blocking ──")
check("a timed event yields naive bounds",
      P._event_bounds(TIMED) == (dt.datetime(2026, 8, 3, 9, 0), dt.datetime(2026, 8, 3, 9, 15)),
      str(P._event_bounds(TIMED)))
check("an all-day event yields None (never blocks a timed slot)",
      P._event_bounds(ALLDAY) is None)
check("a malformed event yields None rather than raising",
      P._event_bounds(MALFORMED) is None)
check("a normal event blocks", P._is_blocking(TIMED) is True)
check("a transparent/free event does not block", P._is_blocking(FREE) is False)
check("a cancelled event does not block", P._is_blocking(CANCELLED) is False)

print("── find_conflicts ──")
EVENTS = [TIMED, ALLDAY, FREE, CANCELLED, MALFORMED]
hit = P.find_conflicts(EVENTS, dt.datetime(2026, 8, 3, 9, 0), dt.datetime(2026, 8, 3, 9, 30))
check("an overlapping busy event is a conflict",
      [e["summary"] for e in hit] == ["Standup"], str([e.get("summary") for e in hit]))
check("free / cancelled / all-day / malformed are all excluded", len(hit) == 1, str(len(hit)))
none = P.find_conflicts(EVENTS, dt.datetime(2026, 8, 3, 17, 0), dt.datetime(2026, 8, 3, 17, 30))
check("a non-overlapping window has no conflicts", none == [], str(none))
# half-open interval: an event ending exactly at the start must not conflict
abut = P.find_conflicts([TIMED], dt.datetime(2026, 8, 3, 9, 15), dt.datetime(2026, 8, 3, 9, 45))
check("an event ending exactly at the new start does NOT conflict (half-open)",
      abut == [], str([e.get("summary") for e in abut]))

print("── find_duplicates ──")
check("an exact title matches", [e["summary"] for e in P.find_duplicates(EVENTS, "Standup")] == ["Standup"])
check("matching is case-insensitive",
      [e["summary"] for e in P.find_duplicates(EVENTS, "STANDUP")] == ["Standup"])
check("an unrelated title matches nothing", P.find_duplicates(EVENTS, "nothing") == [])

print("── pick_email_for_name ──")
H = [{"from": "Jane Doe <jane@example.com>"},
     {"from": "Jane Doe <jane@example.com>"},
     {"from": "Bob <bob@example.com>"},
     {"from": "malformed-no-email"}]
r = P.pick_email_for_name(H, "Jane")
check("a clear match resolves to the address", r.get("email") == "jane@example.com", str(r))
check("no match resolves to no email", not P.pick_email_for_name(H, "Nobody").get("email"),
      str(P.pick_email_for_name(H, "Nobody")))
tie = P.pick_email_for_name(
    [{"from": "Jane Doe <jane@a.com>"}, {"from": "Jane Doe <jane@b.com>"}], "Jane")
check("an ambiguous tie is flagged rather than guessed",
      not tie.get("email") or tie.get("ambiguous") or len(tie.get("candidates", [])) > 1,
      str(tie))
check("a malformed From line does not raise",
      P.pick_email_for_name([{"from": "malformed"}], "x") is not None)

print("── delegation ──")
sched = (REPO / "skills" / "meeting-scheduler" / "scripts" / "schedule_meeting.py").read_text()
check("schedule_meeting imports the policy", "from policy import" in sched)
for dup in ("def parse_when(", "def compute_end(", "def find_conflicts(",
            "def find_duplicates(", "def pick_email_for_name(", "def _event_bounds("):
    check(f"schedule_meeting does not redefine {dup.split('(')[0][4:]}", dup not in sched)
check("the irreversible create/send stays in the CLI module", "def create_event(" in sched)
check("host timezone detection stays in the CLI module", "def detect_timezone(" in sched)

print()
if FAILS:
    print(f"FAIL — {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("PASS — meeting-scheduler policy")
