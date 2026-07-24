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
    check("access_tier: ambient" in body, "consumer: promoted task is ambient tier (never owner)")
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
