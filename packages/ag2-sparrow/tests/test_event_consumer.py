"""Tests for event_consumer (AWP P1) — inbox → taskify → tasks/. Covers ambient
trust boundary, held-events-not-consumed (no loss), idempotent re-drain, and
skip-settles-immediately. Self-contained; exit 0/1."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ag2_sparrow.event_consumer import EventConsumer, TaskifyHandler   # noqa: E402
from ag2_sparrow.event_inbox import EventInbox                          # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _ev(eid, cursor, etype="message.created", actor="@u:hs", room="!r:hs"):
    return {"event_id": eid, "cursor": cursor, "type": etype, "actor_id": actor, "room_id": room}


def _inbox_with(events):
    inbox = EventInbox(os.path.join(tempfile.mkdtemp(), "e.db"))
    for e in events:
        inbox.insert(e)
    return inbox


def test_taskify_promotes_ambient_task():
    d = tempfile.mkdtemp()
    inbox = _inbox_with([_ev(f"$m{i}", i) for i in range(1, 4)])
    h = TaskifyHandler(d, agent_mxid="@me:hs", threshold=3)
    r = EventConsumer(inbox, h).drain()
    check(len(r["promoted"]) == 1, "consumer: threshold reached → 1 task promoted")
    body = open(r["promoted"][0]).read()
    check("access_tier: guest" in body, "consumer: promoted task is guest tier (never owner)")
    check("origin: promoted" in body, "consumer: provenance rides its own header")
    check("access_tier: ambient" not in body, "consumer: the retired ambient spelling is not stamped")
    check("SUTANDO SYSTEM INSTRUCTIONS" in body and "source: events-promotion" in body,
          "consumer: in-band DiD block + events-promotion provenance present")
    check(os.path.basename(r["promoted"][0]).startswith("task-taskify-"),
          "consumer: deterministic taskify id")
    check(inbox.consumed_cursor() == 3, "consumer: flushed-batch events marked consumed")


def test_held_events_not_consumed():
    # 2 meaningful events, threshold 3 → nothing flushes → NOTHING marked consumed
    # (they must survive a crash and re-drain), then a 3rd flushes the batch.
    inbox = _inbox_with([_ev("$a", 1), _ev("$b", 2)])
    d = tempfile.mkdtemp()
    h = TaskifyHandler(d, agent_mxid="@me:hs", threshold=3)
    c = EventConsumer(inbox, h)
    r1 = c.drain()
    check(r1["promoted"] == [] and r1["consumed"] == 0,
          "consumer: sub-threshold batch promotes nothing AND consumes nothing (held)")
    check(h.has_pending() is True,
          "consumer: handler reports a pending (un-flushed) batch")
    check([e["event_id"] for e in inbox.unconsumed()] == ["$a", "$b"],
          "consumer: held events stay unconsumed (survive restart)")
    inbox.insert(_ev("$c", 3))
    r2 = c.drain()  # re-drains $a,$b (deduped in handler) + new $c → flush
    check(len(r2["promoted"]) == 1, "consumer: 3rd meaningful event flushes the batch")
    check(inbox.unconsumed() == [], "consumer: whole batch consumed on flush (no loss, no dup)")


def test_skip_settles_immediately():
    inbox = _inbox_with([_ev("$noise", 1, etype="room.state_changed"),
                         _ev("$self", 2, actor="@me:hs"),
                         _ev("$m", 3)])
    d = tempfile.mkdtemp()
    h = TaskifyHandler(d, agent_mxid="@me:hs", threshold=5)
    r = EventConsumer(inbox, h).drain()
    # noise (non-meaningful) + self-echo are settled immediately; $m is held.
    check(r["consumed"] == 2, "consumer: non-meaningful + self-echo settle immediately (skip)")
    check([e["event_id"] for e in inbox.unconsumed()] == ["$m"],
          "consumer: only the held meaningful event remains unconsumed")
    check(r["promoted"] == [], "consumer: no promotion below threshold")


def test_idempotent_redrain_no_duplicate_task():
    # Crash before mark_consumed: re-drain the SAME flushed batch → the
    # deterministic id resolves to the same task file (no duplicate).
    d = tempfile.mkdtemp()
    inbox = _inbox_with([_ev("$x", 1), _ev("$y", 2)])
    h1 = TaskifyHandler(d, agent_mxid="@me:hs", threshold=2)
    p1 = h1.offer(_ev("$x", 1)); p1 = h1.offer(_ev("$y", 2))  # flush
    # simulate crash: a fresh handler replays the same events
    h2 = TaskifyHandler(d, agent_mxid="@me:hs", threshold=2)
    h2.offer(_ev("$x", 1)); h2.offer(_ev("$y", 2))
    files = [f for f in os.listdir(d) if f.endswith(".txt")]
    check(len(files) == 1, "consumer: crash-replay of a batch produces the SAME task (idempotent, no dup)")


def test_promotion_is_durable_before_consume():
    # Review blocker: _promote() renamed the task file without fsync; a crash
    # after the SQLite consume-commit but before data/dir-entry flush could
    # lose the promoted task while its source events were already consumed.
    # Assert fsync covers BOTH the file and the containing directory before
    # drain() marks anything consumed.
    import ag2_sparrow.event_consumer as ecmod
    fsynced = []
    orig_fsync = os.fsync
    os.fsync = lambda fd: fsynced.append(fd)
    try:
        d = tempfile.mkdtemp()
        inbox = _inbox_with([_ev(f"$d{i}", i) for i in range(1, 4)])
        h = TaskifyHandler(d, agent_mxid="@me:hs", threshold=3)
        r = EventConsumer(inbox, h).drain()
    finally:
        os.fsync = orig_fsync
    check(len(r["promoted"]) == 1 and len(fsynced) >= 2,
          "consumer: promotion fsyncs file AND directory before the batch is consumed")
    check(inbox.consumed_cursor() == 3,
          "consumer: consume still happens after the durable promotion")
    _ = ecmod  # module referenced for clarity of what is under test


def test_rooms_never_mix_in_one_task():
    # Review P1: one global batch attributed a private room's events to the
    # last room seen. Batches are now per-room: each room promotes its OWN
    # task with correct channel_id + provenance, never mixed.
    d = tempfile.mkdtemp()
    inbox = _inbox_with([
        _ev("$p1", 1, room="!private:hs"), _ev("$s1", 2, room="!shared:hs"),
        _ev("$p2", 3, room="!private:hs"), _ev("$s2", 4, room="!shared:hs")])
    h = TaskifyHandler(d, agent_mxid="@me:hs", threshold=2)
    r = EventConsumer(inbox, h).drain()
    check(len(r["promoted"]) == 2, "two rooms at threshold → two separate tasks")
    bodies = {open(p).read() for p in r["promoted"]}
    priv = next(b for b in bodies if "channel_id: !private:hs" in b)
    shar = next(b for b in bodies if "channel_id: !shared:hs" in b)
    check("$p1" in priv and "$p2" in priv and "$s1" not in priv,
          "private task carries ONLY private-room events")
    check("$s1" in shar and "$s2" in shar and "$p1" not in shar,
          "shared task carries ONLY shared-room events (no boundary crossing)")
    check(inbox.unconsumed() == [], "all events settled across both rooms")


def test_task_carries_reviewable_payload():
    # Review P1: the task said "review and act" but carried only ids — the
    # sandboxed consumer had nothing to review. Bounded UNTRUSTED summaries
    # (type + actor + first 120 chars) now ride in the body.
    d = tempfile.mkdtemp()
    ev = _ev("$m1", 1)
    ev["content"] = {"body": "unique-payload-marker-xyz: please review the deploy"}
    inbox = _inbox_with([ev, _ev("$m2", 2), _ev("$m3", 3)])
    h = TaskifyHandler(d, agent_mxid="@me:hs", threshold=3)
    r = EventConsumer(inbox, h).drain()
    body = open(r["promoted"][0]).read()
    check("UNTRUSTED" in body and "unique-payload-marker-xyz" in body,
          "task body carries bounded untrusted event summaries (reviewable)")
    check("[message.created] @u:hs" in body,
          "summaries carry type + actor attribution")


def test_failed_promotion_is_retried_on_redrain():
    # Review P1: events were recorded in _seen BEFORE promotion; a transient
    # promotion failure was never retried until a NEW event arrived. Now a
    # threshold-ready batch re-attempts the flush on every re-drain.
    d = tempfile.mkdtemp()
    inbox = _inbox_with([_ev("$f1", 1), _ev("$f2", 2)])
    h = TaskifyHandler(d, agent_mxid="@me:hs", threshold=2)
    c = EventConsumer(inbox, h)
    calls = {"n": 0}
    orig_replace = os.replace

    def flaky_replace(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient disk error")
        return orig_replace(a, b)

    os.replace = flaky_replace
    try:
        r1 = c.drain()
        check(r1["promoted"] == [] and r1["consumed"] == 0
              and inbox.unconsumed() != [],
              "transient promotion failure settles NOTHING (events kept)")
        r2 = c.drain()   # same events re-drained, no new event needed
        check(len(r2["promoted"]) == 1 and inbox.unconsumed() == [],
              "RETRY — the same threshold-ready batch promotes on re-drain")
    finally:
        os.replace = orig_replace


def test_full_page_of_held_rooms_never_starves_newer_events():
    # Review P1 repro: MORE held sub-threshold rows than one read page. 120
    # distinct rooms each hold 1 event (threshold 3, never flush), then room
    # "!hot:hs" gets 3 events at the END of the backlog. With a fixed
    # oldest-first window the first 100 held rows pin the page and the hot
    # room's events are never even SEEN — drain makes zero progress forever.
    # Cursor pagination must reach and promote them in ONE drain call.
    events = [_ev(f"$cold{i}", i, room=f"!cold{i}:hs") for i in range(1, 121)]
    events += [_ev(f"$hot{i}", 120 + i, room="!hot:hs") for i in range(1, 4)]
    inbox = _inbox_with(events)
    d = tempfile.mkdtemp()
    h = TaskifyHandler(d, agent_mxid="@me:hs", threshold=3)
    c = EventConsumer(inbox, h, batch=100)  # page smaller than the held set
    r = c.drain()
    check(r["seen"] == 123, "starvation: one drain pages past held rows (sees ALL 123)")
    check(len(r["promoted"]) == 1 and "!hot:hs" in open(r["promoted"][0]).read(),
          "starvation: hot room BEYOND the first page still promotes")
    held = [e["event_id"] for e in inbox.unconsumed(limit=500)]
    check(len(held) == 120 and "$hot1" not in held,
          "starvation: hot batch consumed; cold held rows stay for crash recovery")
    # a re-drain is a no-op (held rows deduped, nothing new) — but still sees all
    r2 = c.drain()
    check(r2["promoted"] == [] and r2["consumed"] == 0,
          "starvation: re-drain of held-only backlog stays a safe no-op")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"# {name}")
            fn()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})")
        return 1
    print("\nPASS — event consumer P1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
