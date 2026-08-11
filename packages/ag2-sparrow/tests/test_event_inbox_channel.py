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
    check(ch.health["status"] == "reconnecting" and ch.health["last_cursor"] == 8,
          "channel: after EOF health reports reconnecting (review fix) + last_cursor kept")


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


def test_inbox_prune_and_close():
    inbox = EventInbox(_tmpdb())
    for i in range(1, 6):
        inbox.insert(_ev(f"$c{i}", i))
    inbox.mark_consumed(["$c1", "$c2", "$c3"])
    # max_age_s=-1 → every consumed row counts as "old"; keep_last=1 keeps only
    # the most-recent cursor, so the 3 consumed rows below it get pruned.
    pruned = inbox.prune(max_age_s=-1, keep_last=1)
    check(pruned == 3, "inbox: prune drops old CONSUMED events")
    check([e["event_id"] for e in inbox.unconsumed()] == ["$c4", "$c5"],
          "inbox: prune NEVER removes unconsumed events")
    inbox.close()
    check(True, "inbox: close() is clean")


def test_channel_connect_error_is_retryable():
    inbox = EventInbox(_tmpdb())
    ch, retry = _run_once(inbox, open_error=urllib.error.URLError("boom"))
    check(retry is True and ch.health["status"] == "reconnecting",
          "channel: a connect URLError is retryable (reconnect, not fatal)")
    ch2, retry2 = _run_once(inbox, open_error=urllib.error.HTTPError("u", 500, "x", {}, None))
    check(retry2 is True and ch2.health["status"] == "reconnecting",
          "channel: a non-fatal HTTP (500) is retryable")


def test_channel_stream_drop_is_retryable():
    inbox = EventInbox(_tmpdb())

    class _BadStream:
        def __iter__(self):
            raise urllib.error.URLError("mid-stream drop")
        def close(self):
            pass
    ch, retry = _run_once(inbox, _BadStream())
    check(retry is True and ch.health["status"] == "reconnecting",
          "channel: a mid-stream drop is retryable")


def test_channel_auth_retry_survives_401():
    # auth_retry=True (bridge has token-rotation recovery armed): 401/403 is a
    # recovery WINDOW — retryable, health=auth_failed — so a rotated bearer in
    # the SHARED header dict is picked up on a later reconnect instead of the
    # channel dying before the rotation lands. 404 stays fatal either way.
    import urllib.error
    inbox = EventInbox(_tmpdb())
    headers = {"Authorization": "Bearer stale"}
    ch = ec.EventChannel(inbox, "https://gw", headers, auth_retry=True)
    orig = ec.urllib.request.urlopen
    ec.urllib.request.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(
        urllib.error.HTTPError("https://gw", 401, "unauthorized", None, None))
    try:
        retry = ch._consume_once()
    finally:
        ec.urllib.request.urlopen = orig
    check(retry is True and ch.health["status"] == "auth_failed",
          "channel: 401 is retryable when auth_retry is armed")
    # The reconnect reads the SHARED dict — a bridge-side rotation is visible.
    headers["Authorization"] = "Bearer rotated"
    seen = {}
    def _capture(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        raise urllib.error.HTTPError("https://gw", 401, "unauthorized", None, None)
    ec.urllib.request.urlopen = _capture
    try:
        ch._consume_once()
    finally:
        ec.urllib.request.urlopen = orig
    check(seen.get("auth") == "Bearer rotated",
          "channel: reconnect carries the rotated bearer from the shared dict")
    # 404 remains fatal even with auth_retry (rotation cannot add a route).
    ch404 = ec.EventChannel(inbox, "https://gw", {}, auth_retry=True)
    ec.urllib.request.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(
        urllib.error.HTTPError("https://gw", 404, "nope", None, None))
    try:
        retry404 = ch404._consume_once()
    finally:
        ec.urllib.request.urlopen = orig
    check(retry404 is False, "channel: 404 stays fatal with auth_retry")


def test_channel_run_loop_reconnects_then_stops():
    inbox = EventInbox(_tmpdb())
    ch = ec.EventChannel(inbox, "https://gw", {}, max_backoff=0.01)
    calls = {"n": 0}

    def _fake():
        calls["n"] += 1
        ch._set(last_cursor=calls["n"])   # progress each round → resets backoff ladder
        return True
    ch._consume_once = _fake
    orig_sleep = ec.time.sleep
    ec.time.sleep = lambda s: None
    try:
        ch.run(stop=lambda: calls["n"] >= 2)   # stop after 2 reconnect rounds
    finally:
        ec.time.sleep = orig_sleep
    check(calls["n"] >= 2, "channel: run() loops + reconnects until stop()")
    check(ch.health["status"] == "stopped" and ch.health["retry_count"] >= 1,
          "channel: run() sets stopped + counts retries")


def test_channel_run_stops_on_fatal():
    inbox = EventInbox(_tmpdb())
    ch = ec.EventChannel(inbox, "https://gw", {})
    ch._consume_once = lambda: False   # fatal
    ch.run(stop=lambda: False)
    check(ch.health["status"] != "stopped",
          "channel: run() returns immediately on fatal (does not spin to 'stopped')")


def test_channel_nonnumeric_id_and_close_error_swallowed():
    inbox = EventInbox(_tmpdb())

    class _Resp:
        def __init__(self, data):
            self._b = io.BytesIO(data.encode())
        def __iter__(self):
            return iter(self._b)
        def close(self):
            raise OSError("close blew up")   # exercises the finally-except

    # non-numeric SSE id → int(sse_id) raises → the cursor-derive is skipped, not fatal
    body = 'id: notanumber\ndata: {"event_id":"$z","type":"message.created"}\n\n'
    ch, retry = _run_once(inbox, _Resp(body))
    check(retry is True and ch.health["status"] == "reconnecting",
          "channel: non-numeric id + close() OSError swallowed → retryable, EOF→reconnecting")


def test_channel_generic_error_mid_stream_isolated():
    inbox = EventInbox(_tmpdb())

    def _boom(_ev):
        raise RuntimeError("insert kaboom")   # a NON-URLError, mid-stream
    inbox.insert = _boom
    body = 'id: 4\ndata: {"event_id":"$q","type":"message.created"}\n\n'
    ch, retry = _run_once(inbox, _sse_resp(body))
    check(retry is True and ch.health["status"] == "reconnecting",
          "channel: a generic mid-stream error is swallowed (isolation) → reconnect")


def test_inbox_concurrent_threads_safe():
    # Review blocker: one sqlite3.Connection shared by the channel (writer)
    # thread and the drain (reader) thread segfaulted/corrupted without app
    # locks. This is the PR's own opt-in topology — writer inserting while the
    # consumer reads + marks. Must complete with no exception and full counts.
    import threading
    inbox = EventInbox(_tmpdb())
    N = 300
    errs = []

    def writer():
        try:
            for i in range(1, N + 1):
                inbox.insert(_ev(f"$w{i}", i))
        except Exception as e:  # noqa: BLE001
            errs.append(f"writer: {e}")

    def reader():
        try:
            for _ in range(200):
                batch = inbox.unconsumed(20)
                inbox.mark_consumed([e["event_id"] for e in batch])
                inbox.durable_cursor(); inbox.consumed_cursor()
        except Exception as e:  # noqa: BLE001
            errs.append(f"reader: {e}")

    threads = [threading.Thread(target=writer)] + \
              [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(errs == [], f"inbox: concurrent writer+readers raise nothing ({errs[:1]})")
    check(inbox.durable_cursor() == N,
          "inbox: all concurrent inserts landed (no lost writes)")


def test_channel_clean_eof_reports_reconnecting():
    # Review blocker: after a clean EOF, health stayed "connected" while run()
    # slept in backoff — supervisors showed a dead stream as healthy.
    inbox = EventInbox(_tmpdb())
    ch, retry = _run_once(inbox, _sse_resp(""))   # zero-byte stream = clean EOF
    check(retry is True and ch.health["status"] == "reconnecting",
          "channel: clean EOF flips health to reconnecting (never 'connected' in backoff)")


def test_channel_non_dict_frame_does_not_poison_stream():
    # Review blocker: `data: []` is valid JSON but not an event object; it used
    # to reach insert(), raise, close the stream, and REPLAY from the same
    # cursor forever — blocking every later valid event.
    inbox = EventInbox(_tmpdb())
    body = ('data: []\n\n'
            'data: 42\n\n'
            'id: 6\ndata: {"event_id":"$after","type":"message.created"}\n\n')
    ch, retry = _run_once(inbox, _sse_resp(body))
    check(retry is True, "channel: non-dict frames are retryable, not fatal")
    check(inbox.durable_cursor() == 6
          and [e["event_id"] for e in inbox.unconsumed()] == ["$after"],
          "channel: stream continues PAST non-dict frames — later events land")


def test_inbox_duplicate_leaves_no_open_transaction():
    # Review P1: a plain INSERT raising IntegrityError left the shared
    # connection inside an open write transaction — and duplicate replay is
    # the NORMAL at-least-once path on every reconnect, so one replay locked
    # the WAL db for every other connection. INSERT OR IGNORE + commit must
    # leave the connection clean and the db writable cross-connection.
    import sqlite3
    db = _tmpdb()
    inbox = EventInbox(db)
    inbox.insert(_ev("$dup", 1))
    check(inbox.insert(_ev("$dup", 1)) is False, "duplicate still reports False")
    check(inbox._db.in_transaction is False,
          "TRANSACTION-CLEAN — duplicate replay leaves no open transaction")
    other = sqlite3.connect(db, timeout=2)
    other.execute("INSERT INTO event_inbox(event_id,cursor,payload,received_at)"
                  " VALUES ('$x2',2,'{}',0)")
    other.commit(); other.close()
    check(inbox.durable_cursor() == 2,
          "cross-connection write succeeds after a duplicate (no WAL lock held)")


def test_channel_sends_gateway_user_agent():
    # Review P1: Cloudflare 403s urllib's default UA and the channel treats
    # 403 as FATAL — without an explicit client UA the channel would stop
    # permanently on first real-gateway connect.
    inbox = EventInbox(_tmpdb())
    ch = ec.EventChannel(inbox, "https://gw", {"Authorization": "Bearer x"})
    seen = {}
    orig = ec.urllib.request.urlopen

    def fake_open(req, timeout=None):
        seen["ua"] = req.headers.get("User-agent")
        return _sse_resp("")

    ec.urllib.request.urlopen = fake_open
    try:
        ch._consume_once()
    finally:
        ec.urllib.request.urlopen = orig
    check(seen.get("ua") == "sutando-gateway-client/1.0",
          "channel: SSE request carries the explicit gateway client UA")


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
