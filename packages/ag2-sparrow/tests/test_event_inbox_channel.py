"""Tests for the P0 event delivery channel — durable inbox + persistent SSE
consumer. Covers the friend's fault-recovery acceptance criteria: at-least-once
dedup, cursor recovery, crash-before-durable safety, channel isolation, fatal
auth stop. Self-contained (stdlib + a mocked urlopen). Exit 0/1."""
import io
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ag2_sparrow import event_channel as ec          # noqa: E402
from ag2_sparrow.event_inbox import EventInbox        # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _ev(eid, cursor, etype="message.created", room="!r:hs"):
    return {"event_id": eid, "cursor": cursor, "type": etype, "room_id": room,
            "content": {"text": "hi"}}


def _tmpdb():
    return os.path.join(tempfile.mkdtemp(), "events.db")


# ----- inbox -----
def test_inbox_dedup_and_cursors():
    inbox = EventInbox(_tmpdb())
    check(inbox.insert(_ev("$a", 1)) is True, "inbox: first insert is new")
    check(inbox.insert(_ev("$a", 1)) is False, "inbox: duplicate event_id ignored (at-least-once)")
    inbox.insert(_ev("$b", 2))
    check(inbox.durable_cursor() == 2, "inbox: durable_cursor = max written")
    un = inbox.unconsumed()
    check([e["event_id"] for e in un] == ["$a", "$b"], "inbox: unconsumed oldest-first")
    check(inbox.mark_consumed(["$a"]) == 1 and inbox.consumed_cursor() == 1,
          "inbox: mark_consumed advances consumed_cursor")
    check([e["event_id"] for e in inbox.unconsumed()] == ["$b"], "inbox: consumed events drop out of unconsumed")


def test_inbox_bad_envelope_never_advances():
    inbox = EventInbox(_tmpdb())
    check(inbox.insert({"event_id": "", "cursor": 5}) is False, "inbox: empty event_id rejected")
    check(inbox.insert({"event_id": "$x", "cursor": None}) is False, "inbox: non-int cursor rejected")
    check(inbox.durable_cursor() is None, "inbox: bad envelopes never advance the cursor")


def test_crash_before_durable_is_idempotent():
    # Simulate: events 1,2 written; "restart" (fresh inbox, same db); replay of
    # 1,2,3 after reconnect. 1,2 dedup, 3 lands — no loss, no duplicate.
    db = _tmpdb()
    a = EventInbox(db)
    a.insert(_ev("$1", 1)); a.insert(_ev("$2", 2))
    a.close()
    b = EventInbox(db)  # restart
    check(b.durable_cursor() == 2, "restart: resume anchor = last durable cursor")
    check(b.insert(_ev("$1", 1)) is False and b.insert(_ev("$2", 2)) is False,
          "restart: replayed events deduped (no duplicate)")
    check(b.insert(_ev("$3", 3)) is True and b.durable_cursor() == 3,
          "restart: new event past the resume point lands (no loss)")


# ----- channel (mocked SSE) -----
def _sse_resp(text):
    return io.BytesIO(text.encode())


def _run_once(inbox, monkey_resp=None, open_error=None):
    ch = ec.EventChannel(inbox, "https://gw", {"Authorization": "Bearer x"})
    orig = ec.urllib.request.urlopen

    def fake_open(req, timeout=None):
        if open_error:
            raise open_error
        return monkey_resp

    ec.urllib.request.urlopen = fake_open
    try:
        retryable = ch._consume_once()
    finally:
        ec.urllib.request.urlopen = orig
    return ch, retryable


def test_channel_consumes_to_inbox():
    inbox = EventInbox(_tmpdb())
    body = ('id: 7\ndata: {"event_id":"$e7","type":"message.created","room_id":"!r:hs"}\n\n'
            ': keepalive\n\n'
            'id: 8\ndata: {"event_id":"$e8","type":"artifact.updated","room_id":"!r:hs"}\n\n')
    ch, retry = _run_once(inbox, _sse_resp(body))
    check(retry is True, "channel: normal EOF is retryable (reconnect)")
    check(inbox.durable_cursor() == 8, "channel: cursor advanced via sticky id (7→8)")
    ids = [e["event_id"] for e in inbox.unconsumed()]
    check(ids == ["$e7", "$e8"], "channel: both events durably in inbox (keepalive ignored)")
    check(ch.health["status"] == "connected" and ch.health["last_cursor"] == 8,
          "channel: health reports connected + last_cursor")


def test_channel_dedup_on_replay():
    inbox = EventInbox(_tmpdb())
    inbox.insert(_ev("$e7", 7))
    # reconnect replays $e7 (already durable) + delivers $e9
    body = ('id: 7\ndata: {"event_id":"$e7","type":"message.created"}\n\n'
            'id: 9\ndata: {"event_id":"$e9","type":"message.created"}\n\n')
    _run_once(inbox, _sse_resp(body))
    rows = inbox.unconsumed()
    check(len(rows) == 2 and rows[-1]["event_id"] == "$e9",
          "channel: replayed event deduped, only new one added")


def test_channel_fatal_auth_stops():
    inbox = EventInbox(_tmpdb())
    err = urllib.error.HTTPError("https://gw", 403, "forbidden", {}, None)
    ch, retry = _run_once(inbox, open_error=err)
    check(retry is False, "channel: 403 is FATAL (not retryable — don't spin)")
    check(ch.health["status"] == "auth_failed", "channel: health reports auth_failed")


def test_channel_isolation_swallows_garbage():
    # A garbled frame + a bad-envelope event must not raise out of the channel.
    inbox = EventInbox(_tmpdb())
    body = ('data: {not valid json\n\n'
            'id: 5\ndata: {"event_id":"$ok","type":"message.created"}\n\n')
    try:
        ch, retry = _run_once(inbox, _sse_resp(body))
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    check(not raised, "channel: ISOLATION — garbage never raises out (task delivery safe)")
    check(inbox.durable_cursor() == 5, "channel: recovers past the garbled frame")


def test_channel_resumes_from_durable_cursor():
    inbox = EventInbox(_tmpdb())
    inbox.insert(_ev("$e3", 3))
    ch = ec.EventChannel(inbox, "https://gw", {"Authorization": "Bearer x"})
    seen = {}
    orig = ec.urllib.request.urlopen

    def fake_open(req, timeout=None):
        seen["last_event_id"] = req.headers.get("Last-event-id")
        return _sse_resp("")

    ec.urllib.request.urlopen = fake_open
    try:
        ch._consume_once()
    finally:
        ec.urllib.request.urlopen = orig
    check(seen.get("last_event_id") == "3",
          "channel: resumes with Last-Event-ID = durable_cursor (offline replay)")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"# {name}")
            fn()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})")
        return 1
    print("\nPASS — event inbox + channel P0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
