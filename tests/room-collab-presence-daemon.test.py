#!/usr/bin/env python3
"""Direct coverage for skills/room-collab/scripts/presence_daemon.py.

No server and no clock: `reconcile(now)` takes the time as an argument and the
room-collab client is a stub, so every case here is the daemon's own bookkeeping
— what it starts, what it stops, what it remembers about a surface it stopped,
and what it publishes for the next pass to read.

The thing worth pinning is the memory. A surface dropped for idleness must NOT
come back on the next pass; a surface no longer wanted must leave no trace. Get
that wrong and the 30-minute rule either means nothing or never lets go.
"""
import asyncio
import importlib.util
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "room-collab" / "scripts"

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILS.append(label)


class FakeDoc:
    """Enough of RoomDoc for the daemon: presence, an activity hook, and a
    session that stays open until cancelled."""

    def __init__(self, opened):
        self.opened = opened
        self.touch = None

    async def set_presence(self, name, user_id=None):
        self.opened.append(("presence", name, user_id))

    def on_activity(self, callback):
        self.touch = callback
        return lambda: None


def install_stub(opened, fail_for=()):
    """A stand-in for room_collab_client, injected the way the daemon imports it."""
    mod = types.ModuleType("room_collab_client")

    class _Session:
        def __init__(self, room, kind):
            self.room, self.kind = room, kind

        async def __aenter__(self):
            if self.room in fail_for:
                raise RuntimeError(f"refused: {self.room}")
            opened.append(("open", self.room, self.kind))
            return FakeDoc(opened)

        async def __aexit__(self, *exc):
            return False

    def open_room_collab(url, room, token, *, kind="markdown", insecure=False):
        return _Session(room, kind)

    mod.open_room_collab = open_room_collab
    sys.modules["room_collab_client"] = mod


def load():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "presence_daemon", SCRIPTS / "presence_daemon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    opened = []
    install_stub(opened, fail_for={"!bad:x"})
    dm = load()
    store = sys.modules["presence_store"] if "presence_store" in sys.modules else None
    if store is None:
        spec = importlib.util.spec_from_file_location(
            "presence_store", SCRIPTS / "presence_store.py")
        store = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(store)

    TMP = Path(tempfile.mkdtemp(prefix="presence-daemon-"))
    NOW = 1_000_000.0

    def want(entries):
        store.write_entries(dm.desired_path(TMP), entries)

    def entry(room, kind="markdown", summoned_at=NOW):
        return {"room": room, "kind": kind, "identity": "@a:x", "name": "A",
                "summoned_at": summoned_at}

    # The daemon's clock is the test's clock: `reconcile(now)` alone is not
    # enough, because a connection stamps its own activity too.
    clock = {"t": NOW}
    d = dm.Daemon(TMP, "https://example.invalid", "tok", clock=lambda: clock["t"])

    print("── joining and leaving ──")
    want([entry("!a")])
    asyncio.run(d.reconcile(NOW))
    check("a summoned surface is held", set(d.held) == {("!a", "markdown")})
    check("...and the socket was actually opened",
          ("open", "!a", "markdown") in opened)
    check("...announcing a name, not an anonymous connection",
          any(o[0] == "presence" and o[1] == "A" for o in opened))
    check("the live record is published for the next pass",
          [r["state"] for r in store.read_entries(dm.live_path(TMP))] == [dm.policy.CONNECTED])

    want([])
    asyncio.run(d.reconcile(NOW + 1))
    check("a surface no longer summoned is released", d.held == {})
    # `left` leaves no memory: remembering it would keep a row in the record
    # for something nobody is asking for.
    check("...and nothing is remembered about it", d.retired == {})
    check("the published record empties with it",
          store.read_entries(dm.live_path(TMP)) == [])

    print("── the 30-minute rule, and the memory that makes it mean something ──")
    want([entry("!a", summoned_at=NOW)])
    clock["t"] = NOW
    asyncio.run(d.reconcile(NOW))
    idle_at = NOW + dm.policy.IDLE_SECONDS
    clock["t"] = idle_at
    asyncio.run(d.reconcile(idle_at))
    check("a quiet surface is dropped at the timeout", d.held == {})
    check("...remembered as idle, not forgotten",
          [r["state"] for r in d.retired.values()] == [dm.policy.IDLE])

    asyncio.run(d.reconcile(idle_at + 1))
    check("the next pass does NOT rejoin it — the timeout would mean nothing",
          d.held == {})

    want([entry("!a", summoned_at=idle_at + 2)])
    asyncio.run(d.reconcile(idle_at + 3))
    check("a NEW summon brings it back", set(d.held) == {("!a", "markdown")})

    print("── activity resets the timer (any change, not only mentions) ──")
    held = d.held[("!a", "markdown")]
    asyncio.run(asyncio.sleep(0))  # let hold() reach on_activity
    held.last_activity = idle_at + 3
    quiet_until = idle_at + 3 + dm.policy.IDLE_SECONDS - 1
    asyncio.run(d.reconcile(quiet_until))
    check("one second short of the timeout it is still held",
          set(d.held) == {("!a", "markdown")})
    held.last_activity = quiet_until          # what on_activity does on a keystroke
    asyncio.run(d.reconcile(quiet_until + dm.policy.IDLE_SECONDS - 1))
    check("a touch inside the window keeps it", set(d.held) == {("!a", "markdown")})

    print("── a surface that refuses must not take the daemon down ──")
    want([entry("!a", summoned_at=NOW), entry("!bad", summoned_at=NOW)])
    asyncio.run(d.reconcile(quiet_until + 2))
    check("the daemon survives a refused surface", ("!a", "markdown") in d.held)

    print("── the cap ──")
    d2 = dm.Daemon(TMP, "https://example.invalid", "tok", cap=2, clock=lambda: clock["t"])
    want([entry(f"!r{n}", summoned_at=NOW + n) for n in range(4)])
    asyncio.run(d2.reconcile(NOW + 10))
    check("never more than the cap are held at once", len(d2.held) == 2)
    check("the newest summons win the slots",
          sorted(k[0] for k in d2.held) == ["!r2", "!r3"])

    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all presence-daemon checks ok'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
