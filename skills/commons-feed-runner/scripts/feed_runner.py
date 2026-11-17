#!/usr/bin/env python3
"""The deterministic half of a Room Commons feed refresh (C4 E4): which grants are due, the one
connector call each may make, the rows it may write, and what the write's reply means.

The agent runs the calls (room.commons.feeds.list, composio_exec, room.commons.feeds.write) and
hands their replies to this script; the script decides everything that must not be left to
judgment. It never calls anything itself and holds no state.

  due     <me> <room_id>=<feeds.json>... the runs due now, one per active grant of mine
  rows    <run.json> <composio.json>    the feeds.write arguments for one run
  outcome <write.json>                  done, wait (retry_after_s), stop or fail
  fail    <run.json> <connector.json>   the feeds.fail arguments for a run that could not write
  capable <describe.json>               whether this backend takes room.commons.feeds.fail

Each JSON file is a tool's whole reply as it came back, wrapper included. The backend holds
the same rules (room-collab commons_feeds.write_plan): this script keeps a run from even asking
for what the grant does not cover.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from urllib.parse import urlsplit

# The catalogue the backend lists (room-collab commons_feeds.CATALOGUE) and, per kind, the one
# read-only connector action it maps to. A kind not here is never run.
CALENDAR = "calendar.upcoming"
GATED = {"gmail.threads_matching": "a mail feed waits for the consent dialog (C4 E5)"}
CALENDAR_FIELDS = ("title", "start", "end", "location", "meeting_link", "attendee_count",
                   "organizer")
# What each granted field is read from. The selector is built from the grant's fields, so
# nothing else of an event (its description, attendees' addresses, its link) is ever fetched.
CALENDAR_SOURCES = {
    "title": ("summary",), "start": ("start",), "end": ("end",), "location": ("location",),
    "meeting_link": ("hangoutLink", "conferenceData/entryPoints(entryPointType,uri)"),
    "attendee_count": ("attendees(responseStatus)",),
    "organizer": ("organizer(displayName,email)",),
}


def calendar_selector(fields: list) -> str:
    parts = ["id", "status"]  # which item, and whether it was cancelled
    for field in CALENDAR_FIELDS:
        if field in fields:
            parts += [p for p in CALENDAR_SOURCES[field] if p not in parts]
    return f"items({','.join(parts)})"
SCHEDULE_SLACK_MS = 60_000          # as the backend: a slot may land a minute early
EVERY_MINUTES_MIN = 15
ITEM_ID_MAX = 200
CELL_BYTES_MAX = 4000
MAX_ROWS_LIMIT = 500                # as the backend's grant bound


def _rows_bound(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_ROWS_LIMIT
GRANT_ID_RE = re.compile(r"g[a-z0-9]{9}")
CODE_RE = re.compile(r"[A-Za-z_]{1,64}")  # a refusal code: never free text
URL_RE = re.compile(r"^https?://\S+$")


# The datetime module's UTC constant is 3.11+; shipped skill scripts support Python 3.9.
UTC = datetime.timezone.utc


class Refused(Exception):
    """A reply this script cannot read, or a run it will not make: printed, exit 1."""


def _unwrap(reply, key: str):
    """The object holding `key` in a tool reply, whatever wrapper it came in: the MCP text
    content, a {"result": ...} envelope, or the object itself."""
    seen = [reply]
    while seen:
        node = seen.pop(0)
        if isinstance(node, str):
            try:
                node = json.loads(node)
            except ValueError:
                continue
        if isinstance(node, dict):
            if key in node:
                return node
            seen.extend(node.values())
        elif isinstance(node, list):
            seen.extend(node)
    raise Refused(f"the reply holds no {key!r}")


def _ms(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def slot_key(grant_id: str, every_minutes: int, now_ms: int) -> str:
    """C3's slot key for the grant's schedule: cron:<trigger>:<slot_ts>, the trigger being the
    grant. Derived from the schedule, never from when the run happens to start, so every retry
    of one slot carries the same key and the backend writes it once. Catch-up is `latest`: a
    runner that was down for several slots runs only the current one."""
    every_ms = every_minutes * 60_000
    return f"cron:{grant_id}:{now_ms // every_ms * every_ms}"


def calendar_call(grant: dict, now_ms: int) -> dict:
    query = grant["query"]
    if query.get("calendar", "primary") != "primary":
        raise Refused("a calendar feed reads the owner's primary calendar only")
    days = query.get("window_days", 14)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 60:
        raise Refused("window_days is 1 to 60")
    start = datetime.datetime.fromtimestamp(now_ms / 1000, UTC)
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    return {"toolkit": "googlecalendar", "action": "GOOGLECALENDAR_EVENTS_LIST",
            "arguments": {"calendarId": "primary", "timeMin": start.strftime(stamp),
                          "timeMax": (start + datetime.timedelta(days=days)).strftime(stamp),
                          "singleEvents": True, "orderBy": "startTime", "showDeleted": False,
                          "maxResults": grant["max_rows"],
                          "fields": calendar_selector(grant.get("fields") or [])}}


def due(me: str, replies: list, now_ms: int) -> dict:
    """{"runs": [...], "skipped": [...]} across (room id, feeds.list reply) pairs."""
    runs, skipped = [], []
    for room, reply in replies:
        for grant in _unwrap(reply, "grants")["grants"]:
            gid = grant.get("grant_id")
            if not isinstance(gid, str) or not GRANT_ID_RE.fullmatch(gid):
                continue
            why = None
            kind = (grant.get("query") or {}).get("kind")
            every = _ms(grant.get("every_minutes"))
            last = _ms(grant.get("last_run_at"))
            if grant.get("agent") != me:
                continue  # another agent's grant: not this runner's, and not worth a line
            moves = grant.get("you_may")
            if grant.get("status") != "active":
                why = f"grant is {grant.get('status')}"
            elif isinstance(moves, list) and "write" not in moves:
                # The backend's own word for this caller (feeds.list you_may), when it gives one.
                why = "the backend offers this agent no write on it"
            elif kind in GATED:
                why = GATED[kind]
            elif kind != CALENDAR:
                why = f"{kind!r} is not in this runner's catalogue"
            elif every is None or every < EVERY_MINUTES_MIN:
                why = "every_minutes is missing or under the 15-minute floor"
            elif not _rows_bound(grant.get("max_rows")):
                why = f"max_rows is not a whole number from 1 to {MAX_ROWS_LIMIT}"
            elif last is not None and now_ms - last < every * 60_000 - SCHEDULE_SLACK_MS:
                skipped.append({"room_id": room, "grant_id": gid, "why": "not due",
                                "next_at": last + every * 60_000 - SCHEDULE_SLACK_MS})
                continue
            if why is not None:
                skipped.append({"room_id": room, "grant_id": gid, "why": why})
                continue
            key = slot_key(gid, every, now_ms)
            if key == grant.get("last_run_key"):
                skipped.append({"room_id": room, "grant_id": gid, "why": "slot already written"})
                continue
            try:
                call = calendar_call(grant, now_ms)
            except Refused as exc:
                skipped.append({"room_id": room, "grant_id": gid, "why": str(exc)})
                continue
            runs.append({"room_id": room, "grant_id": gid, "run_key": key, "kind": kind,
                         "fields": list(grant.get("fields") or []),
                         "max_rows": grant["max_rows"], "connector": call,
                         "task": {"idempotency_key": key, "source_kind": "proactive_cron",
                                  "title": "Refresh a connector feed"}})
    return {"runs": runs, "skipped": skipped}


def _cut(text: str) -> str:
    raw = text.encode("utf-8")
    return text if len(raw) <= CELL_BYTES_MAX else raw[:CELL_BYTES_MAX].decode("utf-8", "ignore")


def _when(value) -> str | None:
    """A Google start or end as the date column takes it: an all-day date as YYYY-MM-DD, a
    timed one as YYYY-MM-DDTHH:MM in UTC (the zone the feed's Fetched column uses too)."""
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("date"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value["date"]):
        return value["date"]
    raw = value.get("dateTime")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


def _https(url, hosts=None) -> str | None:
    if not isinstance(url, str) or not URL_RE.match(url):
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or (hosts is not None and parts.hostname not in hosts):
        return None
    return url


def _envelope_failed(reply) -> bool:
    """Whether the connector's envelope says the call failed. Composio puts `successful` and
    `error` beside `data`, which holds `items`: read them where they are, never inside it."""
    try:
        envelope = _unwrap(reply, "successful")
    except Refused:
        envelope = None
    if envelope is not None and (envelope.get("successful") is False or envelope.get("error")):
        return True
    try:
        inner = _unwrap(reply, "items")
    except Refused:
        return False
    return inner.get("successful") is False or bool(inner.get("error"))


def calendar_row(event: dict, fields: list) -> dict | None:
    """One event as a feed row of only the granted fields; None for a cancelled or id-less one."""
    item = event.get("id")
    if (event.get("status") == "cancelled" or not isinstance(item, str)
            or not 0 < len(item) <= ITEM_ID_MAX):
        return None
    meeting = _https(event.get("hangoutLink"))
    if meeting is None:
        points = (event.get("conferenceData") or {}).get("entryPoints") or []
        meeting = next((_https(p.get("uri")) for p in points
                        if isinstance(p, dict) and p.get("entryPointType") == "video"
                        and _https(p.get("uri"))), None)
    organizer = event.get("organizer") or {}
    attendees = event.get("attendees")
    every = {
        "title": _cut(event["summary"]) if isinstance(event.get("summary"), str)
        and event["summary"].strip() else "(no title)",
        "start": _when(event.get("start")), "end": _when(event.get("end")),
        "location": _cut(event["location"]) if isinstance(event.get("location"), str) else None,
        "meeting_link": meeting,
        "attendee_count": len(attendees) if isinstance(attendees, list) else None,
        "organizer": _cut(organizer.get("displayName") or organizer.get("email") or "") or None,
    }
    row = {"_id": item}
    for field in fields:
        if field not in CALENDAR_FIELDS:
            raise Refused(f"{field!r} is not a calendar field")
        if every[field] is not None:
            row[field] = every[field]
    # No _source: the event's link (htmlLink, not fetched) can encode the calendar's address.
    return row


def rows(run: dict, reply) -> dict:
    """The room.commons.feeds.write arguments for one run's connector reply."""
    if run.get("kind") != CALENDAR:
        raise Refused(f"no projection for {run.get('kind')!r}")
    if _envelope_failed(reply):
        raise Refused("the connector call failed")  # its own text may name the account
    items = _unwrap(reply, "items")["items"]
    if not isinstance(items, list):
        raise Refused("the connector's items is not a list")
    out, seen = [], set()
    for event in items:
        row = calendar_row(event, run["fields"]) if isinstance(event, dict) else None
        if row is None or row["_id"] in seen:
            continue
        seen.add(row["_id"])
        out.append(row)
        if len(out) == run["max_rows"]:
            break
    return {"grant_id": run["grant_id"], "run_key": run["run_key"], "rows": out}


# feeds.fail's closed codes: the app shows the error plainly, and nothing of the connector's
# own text (an account, a token, a message) is ever passed on.
FAIL_CODES = ("connector_not_connected", "connector_error", "connector_rate_limited",
              "projection_error")


def _top(reply) -> dict:
    """The reply's own top object, out of the MCP text wrapper when it has one; never its data."""
    node = reply
    if isinstance(node, dict) and isinstance(node.get("content"), list):
        texts = [c.get("text") for c in node["content"] if isinstance(c, dict)]
        node = texts[0] if texts else {}
    if isinstance(node, str):
        try:
            node = json.loads(node)
        except ValueError:
            return {"error": node}
    # A {"result": ...} envelope (the MCP tool reply's shape) holds the connector's own envelope.
    while isinstance(node, dict) and set(node) == {"result"} and isinstance(node["result"], dict):
        node = node["result"]
    return node if isinstance(node, dict) else {}


def _status_text(reply) -> str:
    """The envelope's status and error fields, lowercased: what a failure is judged from. Never
    `data`, whose events can say anything ("Room 429", "14:29", "not connected")."""
    top = _top(reply)
    parts = [k for k in top if k == "connect_required" and top[k]]
    for source in (top, top.get("error") if isinstance(top.get("error"), dict) else {}):
        for key in ("error", "code", "status", "status_code", "message"):
            value = source.get(key)
            if value is not None and not isinstance(value, (dict, list)):
                parts.append(str(value))
    return " ".join(parts).lower()


def failure(run: dict, reply) -> dict:
    """The room.commons.feeds.fail arguments for a run whose connector reply could not become
    rows: why, as one of the closed codes, judged from the envelope's status and error fields
    alone and never quoted."""
    status = _status_text(reply)
    if "connect_required" in status or "not connected" in status or "no connected account" in status:
        code = "connector_not_connected"
    elif ("rate limit" in status or "rate_limit" in status or re.search(r"\b429\b", status)
          or "too many requests" in status):
        code = "connector_rate_limited"
    else:
        try:
            _unwrap(reply, "items")
            failed = _envelope_failed(reply)
        except Refused:
            failed = True
        code = "connector_error" if failed else "projection_error"
    return {"grant_id": run["grant_id"], "run_key": run["run_key"], "code": code}


def capable(reply) -> dict:
    """Whether the backend offers this agent room.commons.feeds.fail, from room.actions.describe's
    reply (an unknown Action answers a refusal). A backend from before it still takes writes:
    the runner then logs a failed run and closes its task, and reports nothing."""
    try:
        described = _unwrap(reply, "action")
    except Refused:
        return {"report_failures": False}
    ok = (described.get("action") == "room.commons.feeds.fail"
          and described.get("decision", "allow") == "allow" and "code" not in described)
    return {"report_failures": ok}


def outcome(reply) -> dict:
    """What a feeds.write reply means for the run and its task: done, wait (the backend's
    retry_after_s), stop (the grant is no longer active) or fail."""
    try:
        result = _unwrap(reply, "run_key")
        if "rows" in result and "code" not in result:
            replayed = result.get("replayed") is True
            return {"status": "done", "task_status": "done",
                    "outcome": ("This slot was already written; nothing new" if replayed
                                else f"Refreshed the feed: {result['rows']} rows")}
    except Refused:
        pass
    try:
        error = _unwrap(reply, "code")
    except Refused:
        error = {"code": None}  # unreadable: nothing of it is kept, it may be connector content
    message = str(error.get("message") or "")
    retry = next((v for k, v in _walk(error) if k == "retry_after_s"
                  and isinstance(v, int) and not isinstance(v, bool)), None)
    if error.get("code") == "RATE_LIMITED":
        return {"status": "wait", "retry_after_s": retry, "task_status": "no_action",
                "outcome": "Not due yet; the feed refreshes on its schedule"}
    state = re.search(r"grant_(revoked|paused|expired)", message)
    if state:
        return {"status": "stop", "task_status": "no_action",
                "outcome": f"The feed's grant is {state[1]}; nothing was written"}
    # Only the codes go to the log: the backend's message can quote the row it refused.
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    codes = [c for c in (error.get("code"), details.get("reason"))
             if isinstance(c, str) and CODE_RE.fullmatch(c)]
    return {"status": "fail", "task_status": "failed",
            "outcome": "The feed refresh was refused; nothing was written",
            "reason": " ".join(codes) or "unreadable refusal"}


def _walk(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _read(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("due", "rows", "outcome", "fail", "capable"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    now_ms = int(datetime.datetime.now(UTC).timestamp() * 1000)
    try:
        if argv[1] == "due" and len(argv) >= 4:
            pairs = [arg.split("=", 1) for arg in argv[3:]]
            if any(len(pair) != 2 or not pair[0].startswith("!") for pair in pairs):
                raise Refused("each reply is given as <room_id>=<file>")
            answer = due(argv[2], [(room, _read(path)) for room, path in pairs], now_ms)
        elif argv[1] == "rows" and len(argv) == 4:
            answer = rows(_read(argv[2]), _read(argv[3]))
        elif argv[1] == "outcome" and len(argv) == 3:
            answer = outcome(_read(argv[2]))
        elif argv[1] == "fail" and len(argv) == 4:
            answer = failure(_read(argv[2]), _read(argv[3]))
        elif argv[1] == "capable" and len(argv) == 3:
            answer = capable(_read(argv[2]))
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    except (Refused, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"refused": str(exc)}))
        return 1
    print(json.dumps(answer, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
