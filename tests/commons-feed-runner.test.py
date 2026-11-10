#!/usr/bin/env python3
"""Tests for skills/commons-feed-runner/scripts/feed_runner.py.

Replies are in the shapes the runner is handed: room.commons.feeds.list's grants as core-api
answers them (actions/commons/feeds.py on ag2space dev), Composio's GOOGLECALENDAR_EVENTS_LIST
reply (wrapped as the MCP returns it), and core-api's error envelope for a refused write.
The rows are held to the backend's own cell rules (room-collab commons_db_writes.normalize and
commons_feeds._desired): a passing run is one the backend accepts. Hermetic."""
from __future__ import annotations

import datetime
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "skills" / "commons-feed-runner" / "scripts" / "feed_runner.py"
spec = importlib.util.spec_from_file_location("feed_runner", _SCRIPT)
fr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fr)

passed = []


def check(name: str, ok: bool) -> None:
    assert ok, name
    passed.append(name)


ME = "@owner-desk.agent:ag2.space"
ROOM = "!hub:ag2.space"
NOW = int(datetime.datetime(2026, 11, 9, 18, 7, tzinfo=datetime.timezone.utc).timestamp() * 1000)
EVERY = 30
# The backend's cell rules for the columns a calendar feed has.
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")


def grant(**over) -> dict:
    g = {"grant_id": "gabc123def", "app": "a1", "database": "dfeed", "owner": "@qy:ag2.space",
         "agent": ME, "connector": "googlecalendar",
         "query": {"kind": "calendar.upcoming", "calendar": "primary", "window_days": 14},
         "fields": ["title", "start", "end", "meeting_link"], "every_minutes": EVERY,
         "max_rows": 200, "status": "active", "created_at": NOW - 86_400_000,
         "expires_at": NOW + 29 * 86_400_000, "revoked_at": None, "revoked_by": None,
         "paused_by": None, "last_run_key": None, "last_run_at": None, "last_rows": None}
    g.update(over)
    return g


def listed(*grants) -> dict:
    """room.action.read's reply for room.commons.feeds.list, as the MCP hands it back."""
    return {"content": [{"type": "text", "text": json.dumps(
        {"result": {"grants": list(grants)}, "action": "room.commons.feeds.list"})}]}


# ---- due ---------------------------------------------------------------------------------

plan = fr.due(ME, [(ROOM, listed(grant()))], NOW)
(run,) = plan["runs"]
slot = NOW // (EVERY * 60_000) * EVERY * 60_000
check("a due grant is one run under C3's slot key",
      run["run_key"] == f"cron:gabc123def:{slot}" and run["task"]["idempotency_key"] == run["run_key"]
      and run["task"]["source_kind"] == "proactive_cron" and run["room_id"] == ROOM)
call = run["connector"]
check("the run's connector call is the catalogue's one read-only action",
      (call["toolkit"], call["action"]) == ("googlecalendar", "GOOGLECALENDAR_EVENTS_LIST"))
args = call["arguments"]
check("it reads the primary calendar only, over the granted window, at most max_rows",
      args["calendarId"] == "primary" and args["timeMin"] == "2026-11-09T18:07:00Z"
      and args["timeMax"] == "2026-11-23T18:07:00Z" and args["maxResults"] == 200
      and args["singleEvents"] is True and args["showDeleted"] is False)
# Review (Susan, blocker 2): the selector is the grant's, not the catalogue's. The default grant
# (title, start, end, meeting_link) never fetches location, attendees or the organizer.
check("it fetches only what the granted fields are projected from",
      args["fields"] == "items(id,status,summary,start,end,hangoutLink,"
                        "conferenceData/entryPoints(entryPointType,uri))")
narrow = fr.due(ME, [(ROOM, listed(grant(fields=["title", "start"])))], NOW)["runs"][0]
check("a grant of title and start fetches title and start, and nothing else of an event",
      narrow["connector"]["arguments"]["fields"] == "items(id,status,summary,start)")
wide = fr.due(ME, [(ROOM, listed(grant(fields=list(fr.CALENDAR_FIELDS))))], NOW)["runs"][0]
check("only a grant of the organizer fetches the organizer, and never an event's link",
      "organizer(displayName,email)" in wide["connector"]["arguments"]["fields"]
      and "htmlLink" not in wide["connector"]["arguments"]["fields"]
      and "description" not in wide["connector"]["arguments"]["fields"])
check("the slot key is the same all through a slot",
      fr.slot_key("gabc123def", EVERY, slot) == fr.slot_key("gabc123def", EVERY, slot + 29 * 60_000)
      != fr.slot_key("gabc123def", EVERY, slot + 30 * 60_000))

others = fr.due(ME, [(ROOM, listed(
    grant(grant_id="gother0001", agent="@someone-else.agent:ag2.space"),
    grant(grant_id="gpaused001", status="paused"),
    grant(grant_id="grevoked01", status="revoked"),
    grant(grant_id="gmail00001", query={"kind": "gmail.threads_matching", "label": "Clients"}),
    grant(grant_id="gnotdue001", last_run_at=NOW - 10 * 60_000, last_run_key="cron:x:1"),
    grant(grant_id="gwritten01", last_run_at=NOW - 40 * 60_000,
          last_run_key=f"cron:gwritten01:{slot}"),
    grant(grant_id="gshared001", query={"kind": "calendar.upcoming", "calendar": "bob@x.com"}),
    grant(grant_id="gnosuch001", query={"kind": "drive.files"}),
    grant(grant_id="BAD"),
))], NOW)
why = {s["grant_id"]: s["why"] for s in others["skipped"]}
check("nothing runs but this agent's due, active calendar grants", others["runs"] == [])
check("another agent's grant is not even listed", "gother0001" not in why)
check("a paused or revoked grant is skipped",
      why["gpaused001"] == "grant is paused" and why["grevoked01"] == "grant is revoked")
check("a mail feed waits for the consent dialog", "consent dialog" in why["gmail00001"])
check("a grant inside its cadence is not due, and says when it is",
      why["gnotdue001"] == "not due" and next(s for s in others["skipped"]
                                              if s["grant_id"] == "gnotdue001")["next_at"]
      == NOW - 10 * 60_000 + EVERY * 60_000 - 60_000)
check("a slot already written is not run again", why["gwritten01"] == "slot already written")
check("another calendar than the owner's primary is never read", "primary" in why["gshared001"])
check("a kind outside the catalogue is never run", "not in this runner's catalogue" in why["gnosuch001"])
check("a malformed grant id is ignored", "BAD" not in why)
check("a grant one minute early is due (the backend's scheduling slack)",
      len(fr.due(ME, [(ROOM, listed(grant(last_run_at=NOW - 29 * 60_000)))], NOW)["runs"]) == 1)

# ---- rows --------------------------------------------------------------------------------

EVENTS = [
    {"id": "ev1", "status": "confirmed", "summary": "Kickoff with Acme",
     "start": {"dateTime": "2026-11-10T09:00:00-08:00"}, "end": {"dateTime": "2026-11-10T10:00:00-08:00"},
     "hangoutLink": "https://meet.google.com/abc-defg-hij", "location": "HQ",
     "attendees": [{"responseStatus": "accepted"}] * 3,
     "organizer": {"displayName": "Qingyun", "email": "qy@x.com"},
     "htmlLink": "https://www.google.com/calendar/event?eid=ev1",
     "description": "the board deck password is hunter2"},
    {"id": "ev2", "summary": "Offsite", "start": {"date": "2026-11-12"}, "end": {"date": "2026-11-13"},
     "conferenceData": {"entryPoints": [{"entryPointType": "phone", "uri": "tel:+1"},
                                        {"entryPointType": "video", "uri": "https://zoom.us/j/1"}]},
     "htmlLink": "https://evil.example/calendar?eid=ev2"},
    {"id": "ev3", "status": "cancelled", "summary": "Gone"},
    {"id": "ev1", "summary": "duplicate"},
    {"summary": "no id"},
    {"id": "ev4", "summary": "   ", "start": {"dateTime": "2026-11-11T12:00:00Z"},
     "hangoutLink": "javascript:alert(1)"},
]


def composio(items, wrap="mcp"):
    data = {"data": {"items": items, "kind": "calendar#events"}, "successful": True, "error": None}
    if wrap == "mcp":
        return {"content": [{"type": "text", "text": json.dumps(data)}]}
    return data


out = fr.rows(run, composio(EVENTS))
by = {r["_id"]: r for r in out["rows"]}
check("the write names the run's grant and key", (out["grant_id"], out["run_key"]) ==
      (run["grant_id"], run["run_key"]))
check("a cancelled, id-less or repeated event makes no row", list(by) == ["ev1", "ev2", "ev4"])
check("a timed event is in UTC, an all-day one is a date",
      by["ev1"]["start"] == "2026-11-10T17:00" and by["ev1"]["end"] == "2026-11-10T18:00"
      and by["ev2"]["start"] == "2026-11-12" and by["ev4"]["start"] == "2026-11-11T12:00")
check("the meeting link is the event's video link, https only",
      by["ev1"]["meeting_link"] == "https://meet.google.com/abc-defg-hij"
      and by["ev2"]["meeting_link"] == "https://zoom.us/j/1" and "meeting_link" not in by["ev4"])
check("a row carries only granted fields: never the description, location or organizer",
      all(set(r) <= {"_id", "title", "start", "end", "meeting_link"}
          for r in out["rows"]) and "hunter2" not in json.dumps(out))
check("a blank title is the placeholder, as the title column needs one",
      by["ev4"]["title"] == "(no title)")
check("no row carries the event's link: Google's eid can encode the calendar's address",
      all("_source" not in r for r in out["rows"]))
check("every date cell fits the backend's date column",
      all(DATE_RE.match(r[f]) for r in out["rows"] for f in ("start", "end") if f in r))

wide = dict(run, fields=list(fr.CALENDAR_FIELDS))
full = {r["_id"]: r for r in fr.rows(wide, composio(EVENTS, wrap="plain"))["rows"]}
check("granted extra fields are projected, from a plain reply too",
      full["ev1"]["location"] == "HQ" and full["ev1"]["attendee_count"] == 3
      and full["ev1"]["organizer"] == "Qingyun" and "hunter2" not in json.dumps(full))
long = fr.rows(dict(run, max_rows=1), composio([{"id": "e", "summary": "x" * 5000}] + EVENTS))
check("at most max_rows, and a cell is cut to the backend's 4000 bytes",
      len(long["rows"]) == 1 and len(long["rows"][0]["title"].encode()) == 4000)
for bad, name in (({"data": {}, "successful": False, "error": "token expired"}, "a failed call"),
                  ({"items": "nope"}, "items that is not a list"),
                  ({"nothing": 1}, "a reply with no items")):
    try:
        fr.rows(run, bad)
        ok = False
    except fr.Refused:
        ok = True
    check(f"{name} is refused, nothing written", ok)
try:
    fr.rows(dict(run, fields=["title", "description"]), composio(EVENTS))
    ok = False
except fr.Refused:
    ok = True
check("a field outside the catalogue is refused", ok)

# ---- outcome -----------------------------------------------------------------------------

done = fr.outcome({"content": [{"type": "text", "text": json.dumps({"result": {
    "grant_id": "gabc123def", "database": "dfeed", "run_key": run["run_key"], "rows": 3,
    "as_of": NOW, "replayed": False}})}]})
check("a written snapshot closes the task done, naming only a count",
      done == {"status": "done", "task_status": "done", "outcome": "Refreshed the feed: 3 rows"})
again = fr.outcome({"result": {"run_key": "k", "rows": 3, "replayed": True}})
check("a replayed slot closes done with nothing new", again["status"] == "done"
      and "already written" in again["outcome"])
wait = fr.outcome({"code": "RATE_LIMITED", "message": "too_soon: at most every 30 minutes",
                   "details": {"retry_after_s": 540}})
check("too soon waits the backend's retry_after_s", wait["status"] == "wait"
      and wait["retry_after_s"] == 540 and wait["task_status"] == "no_action")
for state in ("revoked", "paused", "expired"):
    stop = fr.outcome({"code": "CONFLICT", "message": f"grant_{state}: this grant is {state}"})
    check(f"a {state} grant stops the run", stop["status"] == "stop" and state in stop["outcome"])
# Review (Susan, blocker 1): the backend's message can quote a row ('cell title="Dinner w/ Dr.
# X"'), and `reason` goes to the agent's log, where connector content never goes. Codes only.
fail = fr.outcome({"code": "INVALID_INPUT", "message": 'row 1: cell title="Dinner w/ Dr. X" '
                   "does not fit", "details": {"reason": "not_in_catalogue"}})
check("any other refusal fails the task, logging its codes and never the backend's message",
      fail["status"] == "fail" and fail["task_status"] == "failed"
      and fail["reason"] == "INVALID_INPUT not_in_catalogue"
      and "Dinner" not in json.dumps(fail) and "row 1" not in json.dumps(fail))
unreadable = fr.outcome('the reply was {"title": "Dinner w/ Dr. X"}')
check("a reply with no code at all logs no text of it either",
      unreadable["status"] == "fail" and "Dinner" not in json.dumps(unreadable))

# ---- the command line --------------------------------------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    feeds = Path(tmp) / "feeds.json"
    feeds.write_text(json.dumps(listed(grant())))
    ok = subprocess.run([sys.executable, str(_SCRIPT), "due", ME, f"{ROOM}={feeds}"],
                        capture_output=True, text=True)
    check("due runs from the command line", ok.returncode == 0
          and json.loads(ok.stdout)["runs"][0]["room_id"] == ROOM)
    bad = subprocess.run([sys.executable, str(_SCRIPT), "due", ME, str(feeds)],
                         capture_output=True, text=True)
    check("a reply not tagged with its room is refused", bad.returncode == 1
          and "room_id" in json.loads(bad.stdout)["refused"])
    usage = subprocess.run([sys.executable, str(_SCRIPT), "nope"], capture_output=True, text=True)
    check("an unknown command prints the usage", usage.returncode == 2)

# ---- fail ------------------------------------------------------------------------------

for reply, code in (
        ({"error": "connect_required: googlecalendar is not connected"}, "connector_not_connected"),
        ({"data": {}, "successful": False, "error": "429 Too Many Requests"}, "connector_rate_limited"),
        ({"data": {}, "successful": False, "error": "token for qy@x.com expired"}, "connector_error"),
        ({"nothing": 1}, "connector_error"),
        ({"data": {"items": [{"id": "e", "start": "not a date"}]}, "successful": True},
         "projection_error")):
    args = fr.failure(run, reply)
    check(f"a failed run reports {code}",
          args == {"grant_id": run["grant_id"], "run_key": run["run_key"], "code": code})
    check(f"the {code} report quotes nothing of the connector's answer",
          "qy@x.com" not in json.dumps(args) and "expired" not in json.dumps(args))
check("every reported code is one feeds.fail takes", set(fr.FAIL_CODES) == {
    "connector_not_connected", "connector_error", "connector_rate_limited", "projection_error"})

offered = fr.due(ME, [(ROOM, listed(grant(you_may=["pause", "write", "fail"]),
                                    grant(grant_id="gnowrite01", you_may=["pause"])))], NOW)
check("a grant the backend offers no write on is skipped, one it does is run",
      [r["grant_id"] for r in offered["runs"]] == ["gabc123def"]
      and {s["grant_id"]: s["why"] for s in offered["skipped"]}["gnowrite01"]
      == "the backend offers this agent no write on it")

with tempfile.TemporaryDirectory() as tmp:
    run_file, reply_file = Path(tmp) / "run.json", Path(tmp) / "connector.json"
    run_file.write_text(json.dumps(run))
    reply_file.write_text(json.dumps({"error": "connect_required"}))
    out = subprocess.run([sys.executable, str(_SCRIPT), "fail", str(run_file), str(reply_file)],
                         capture_output=True, text=True)
    check("fail runs from the command line", out.returncode == 0
          and json.loads(out.stdout)["code"] == "connector_not_connected")

# ---- capable ---------------------------------------------------------------------------

described = {"content": [{"type": "text", "text": json.dumps({
    "action": "room.commons.feeds.fail", "decision": "allow", "effect": "mutation"})}]}
check("a backend that describes feeds.fail takes the failure report",
      fr.capable(described) == {"report_failures": True})
for reply, name in (({"code": "NOT_FOUND", "message": "no such action"}, "an unknown Action"),
                    ({"action": "room.commons.feeds.fail", "decision": "deny"}, "a denied one"),
                    ({"nothing": 1}, "an unreadable reply")):
    check(f"{name} means a backend from before the report: log, never crash",
          fr.capable(reply) == {"report_failures": False})

# ---- review (sonichi-dot): the envelope's own failure, and the Python floor ---------------

failed_envelope = {"data": {"items": [], "kind": "calendar#events"}, "successful": False,
                   "error": "token for qy@x.com expired"}
for wrap, name in ((failed_envelope, "plain"),
                   ({"content": [{"type": "text", "text": json.dumps(failed_envelope)}]}, "MCP")):
    try:
        fr.rows(run, wrap)
        ok = False
    except fr.Refused as exc:
        ok = "qy@x.com" not in str(exc)
    check(f"a failed {name} envelope is refused, never written as an empty feed", ok)
    check(f"a failed {name} envelope is reported as a connector error",
          fr.failure(run, wrap)["code"] == "connector_error")
check("no 3.11-only datetime.UTC in the runner (Python 3.9 floor)",
      "datetime.UTC" not in _SCRIPT.read_text())

# Review (Susan, should-fix 3): the code comes from the envelope's status and error only. Event
# content that happens to say "429", "Room 429" or "not connected" changes nothing.
noisy = {"data": {"items": [{"id": "e1", "summary": "Room 429 at 14:29: rate limit talk",
                             "location": "the not connected lab"}]}, "successful": True,
         "error": None}
check("event text never picks the failure code: an envelope that succeeded is a projection error",
      fr.failure(run, noisy)["code"] == "projection_error")
check("the envelope's own error still picks it",
      fr.failure(run, {"data": {}, "successful": False, "error": "HTTP 429 Too Many Requests"})[
          "code"] == "connector_rate_limited"
      and fr.failure(run, {"error": "connect_required: googlecalendar"})["code"]
      == "connector_not_connected")

# Review (Susan, should-fix 4): one malformed grant is skipped on its own; the rest still run.
bad = dict(grant(grant_id="gnomax0001"))
del bad["max_rows"]
mixed = fr.due(ME, [(ROOM, listed(bad, grant(grant_id="gzeromax01", max_rows=0),
                                  grant(grant_id="gboolmax01", max_rows=True),
                                  grant(grant_id="gnoquery01", query=None), grant()))], NOW)
why = {s["grant_id"]: s["why"] for s in mixed["skipped"]}
check("a grant without a usable max_rows or query is skipped, and the good one still runs",
      [r["grant_id"] for r in mixed["runs"]] == ["gabc123def"]
      and {"gnomax0001", "gzeromax01", "gboolmax01", "gnoquery01"} <= set(why))

# ---- edges, and the command line in-process (so a coverage run sees main) ----------------

edge = fr.due(ME, [(ROOM, listed(
    grant(grant_id="gwindow001", query={"kind": "calendar.upcoming", "window_days": 90}),
    grant(grant_id="gfast00001", every_minutes=5)))], NOW)
why = {s["grant_id"]: s["why"] for s in edge["skipped"]}
check("a window past 60 days, or a cadence under the floor, is skipped with its reason",
      why == {"gwindow001": "window_days is 1 to 60",
              "gfast00001": "every_minutes is missing or under the 15-minute floor"})
check("a start or end that is not a Google time is left out, never guessed",
      fr._when("2026-11-10") is None and fr._when({"dateTime": "soon"}) is None
      and fr._when({"dateTime": "2026-11-10T09:00:00"}) is None and fr._when({}) is None)
check("a meeting link is https or nothing", fr._https("http://meet.example/x") is None
      and fr._https(42) is None)
try:
    fr.rows({**run, "kind": "gmail.threads_matching"}, composio(EVENTS))
    ok = False
except fr.Refused:
    ok = True
check("a kind the runner cannot project is refused", ok)
check("a plain-text reply is read as the envelope's error", fr._top("connect_required") ==
      {"error": "connect_required"} and fr.failure(run, "connect_required")["code"]
      == "connector_not_connected")
check("retry_after_s is found wherever the envelope nests it",
      fr.outcome({"code": "RATE_LIMITED", "details": [{"retry_after_s": 30}]})["retry_after_s"]
      == 30)

import contextlib
import io

with tempfile.TemporaryDirectory() as tmp:
    files = {"feeds": listed(grant()), "run": run, "connector": composio(EVENTS),
             "write": {"result": {"run_key": "k", "rows": 1}},
             "describe": {"action": "room.commons.feeds.fail", "decision": "allow"},
             "broken": {"error": "connect_required"}}
    for name, body in files.items():
        (Path(tmp) / f"{name}.json").write_text(json.dumps(body))
    f = {name: str(Path(tmp) / f"{name}.json") for name in files}
    calls = [(["due", ME, f"{ROOM}={f['feeds']}"], 0), (["rows", f["run"], f["connector"]], 0),
             (["outcome", f["write"]], 0), (["fail", f["run"], f["broken"]], 0),
             (["capable", f["describe"]], 0), (["rows", f["run"], f["broken"]], 1),
             (["due", ME, f["feeds"]], 1), (["rows", f["run"]], 2), (["nope"], 2), ([], 2)]
    for args, code in calls:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            got = fr.main(["feed_runner.py", *args])
        check(f"main {' '.join(args[:1]) or '(no command)'} exits {code}", got == code)

# Review (Susan, nit at 48560f6f): an envelope inside {"result": ...}, plain or MCP-wrapped, is
# still the envelope: its rate limit is a rate limit, its missing connection a missing one.
limited = {"data": {}, "successful": False, "error": "429 Too Many Requests"}
for wrapped, name in (({"result": limited}, "result-wrapped"),
                      ({"content": [{"type": "text", "text": json.dumps({"result": limited})}]},
                       "MCP and result-wrapped")):
    check(f"a {name} rate limit is connector_rate_limited",
          fr.failure(run, wrapped)["code"] == "connector_rate_limited")
check("a result-wrapped missing connection is connector_not_connected",
      fr.failure(run, {"result": {"error": "connect_required"}})["code"]
      == "connector_not_connected")

print(f"commons-feed-runner: {len(passed)} passed")
