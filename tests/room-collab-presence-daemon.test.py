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
import os
import shutil
import subprocess
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
    """Enough of the collab document for the daemon: presence, an activity hook,
    and a session that ends when the test says so."""

    def __init__(self, opened, ends):
        self.opened = opened
        self.touch = None
        self._ends = ends            # an asyncio.Future the test resolves

    async def set_presence(self, name, user_id=None):
        self.opened.append(("presence", name, user_id))

    def on_activity(self, callback):
        self.touch = callback
        return lambda: None

    async def closed(self):
        await self._ends
        return RuntimeError("the surface session has ended: test")


def install_stub(opened, fail_for=(), ends=None):
    """A stand-in for room_collab_client, injected the way the daemon imports it."""
    mod = types.ModuleType("room_collab_client")

    class _Session:
        def __init__(self, room, kind):
            self.room, self.kind = room, kind

        async def __aenter__(self):
            if self.room in fail_for:
                raise RuntimeError(f"refused: {self.room}")
            opened.append(("open", self.room, self.kind))
            fut = asyncio.get_event_loop().create_future()
            if ends is not None:
                ends.append(fut)
            return FakeDoc(opened, fut)

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


def policy_key(e):
    return (e.get("room"), e.get("kind"))


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

    print("── invariant 1: transport dies, membership survives, connection returns ──")
    # ⚠ Owner's review of #4646: hold() only slept, so a dead socket went
    # unnoticed while the record still claimed connected.
    ends = []
    opened2 = []
    install_stub(opened2, ends=ends)
    dm2 = load()
    ws2 = Path(tempfile.mkdtemp(prefix="presence-reconnect-"))
    clock2 = {"t": NOW}
    d3 = dm2.Daemon(ws2, "u", "t", clock=lambda: clock2["t"], max_backoff=0.01)
    store.write_entries(dm2.desired_path(ws2), [entry("!r")])

    async def reconnects():
        await d3.reconcile(NOW)
        for _ in range(50):                      # let hold() reach the socket
            await asyncio.sleep(0)
            if opened2.count(("open", "!r", "markdown")) >= 1 and ends:
                break
        first_opens = opened2.count(("open", "!r", "markdown"))
        # A holder that merely loops reopens here too: "it opened again" alone
        # cannot tell that from noticing the socket died.
        for _ in range(60):
            await asyncio.sleep(0.005)
        quiet_opens = opened2.count(("open", "!r", "markdown"))
        held = d3.held[("!r", "markdown")]
        # Read INSIDE the run: after the kill the transport is legitimately
        # down or re-established, so the claim has to be made while it holds.
        up_before = d3.live_rows()[0].get("transport")
        ends[0].set_result(None)                 # the socket dies
        for _ in range(400):
            await asyncio.sleep(0.005)
            if opened2.count(("open", "!r", "markdown")) > first_opens:
                break
        return (first_opens, up_before, opened2.count(("open", "!r", "markdown")),
                held, quiet_opens)

    first, up_before, after, held3, quiet = asyncio.run(reconnects())
    check("the surface connects once to begin with", first == 1)
    check("...and does NOT reopen while that session is alive", quiet == first,
          f"{first} -> {quiet} with no close")
    check("...and while it holds, the record says the transport is up",
          up_before == "up", f"got {up_before!r}")
    check("when the socket dies the daemon reconnects WITHOUT a new summon", after > first,
          f"opens {first} -> {after}")
    check("membership survived the transport", ("!r", "markdown") in d3.held)
    # `state` says this daemon holds the surface; only `transport` claims a
    # live connection, so a lost socket must not still read as connected.
    held3.connected = False
    check("a lost socket is reported as transport down, not as connected",
          d3.live_rows()[0].get("transport") == "down")
    check("the desired record was never touched",
          [policy_key(e) for e in store.read_entries(dm2.desired_path(ws2))] == [("!r", "markdown")])

    print("── invariant 2: the daemon restarts and does not resurrect what it retired ──")
    # ⚠ Same review: live.json was written and never read, so a restart forgot
    # the 30-minute rule and rejoined what it had just let go.
    ws3 = Path(tempfile.mkdtemp(prefix="presence-restart-"))
    store.write_entries(dm.desired_path(ws3), [entry("!q", summoned_at=NOW)])
    clock3 = {"t": NOW}
    d4 = dm.Daemon(ws3, "u", "t", clock=lambda: clock3["t"], max_backoff=0.01)
    asyncio.run(d4.reconcile(NOW))
    clock3["t"] = NOW + dm.policy.IDLE_SECONDS
    asyncio.run(d4.reconcile(clock3["t"]))
    check("the surface idles out before the restart", d4.held == {})
    check("...and that is written down, not only remembered in the process",
          [r["state"] for r in store.read_entries(dm.live_path(ws3))] == [dm.policy.IDLE])

    # Not calling resume() by hand: a reconciler that only remembers when its
    # caller remembers to ask is the same defect in a different place.
    fresh = dm.Daemon(ws3, "u", "t", clock=lambda: clock3["t"], max_backoff=0.01)
    asyncio.run(fresh.reconcile(clock3["t"] + 1))
    check("a RESTARTED daemon does not rejoin the idled surface", fresh.held == {})

    # and the other half: a restart must still honour a new summon
    store.write_entries(dm.desired_path(ws3), [entry("!q", summoned_at=clock3["t"] + 2)])
    asyncio.run(fresh.reconcile(clock3["t"] + 3))
    check("...but a summon after the restart still brings it back",
          set(fresh.held) == {("!q", "markdown")})

    # A CONNECTED row describes a socket that died with the process; taking it
    # back would make `plan` treat the surface as held and never reconnect it.
    ws5 = Path(tempfile.mkdtemp(prefix="presence-restart2-"))
    store.write_entries(dm.desired_path(ws5), [entry("!c", summoned_at=NOW)])
    store.write_entries(dm.live_path(ws5), [{"room": "!c", "kind": "markdown",
                                             "state": dm.policy.CONNECTED,
                                             "since": NOW, "last_activity": NOW,
                                             "transport": "up"}])
    after_restart = dm.Daemon(ws5, "u", "t", clock=lambda: NOW + 1, max_backoff=0.01)
    asyncio.run(after_restart.reconcile(NOW + 1))
    check("a CONNECTED row from the dead process is NOT taken back as state",
          ("!c", "markdown") not in after_restart.retired)
    check("...the surface is reopened instead of assumed still held",
          set(after_restart.held) == {("!c", "markdown")})

    print("── invariant 3: a summon establishes durable membership, session-independently ──")
    # The real CLI in a real subprocess: the registration must outlive the
    # process making it, and needs nothing installed — a bare python runs it.

    # Measured under the coverage gate too, or `stay` reads as uncovered.
    pybase = [sys.executable]
    if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
        pybase += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]
    ws4 = Path(tempfile.mkdtemp(prefix="presence-stay-"))
    cli = REPO / "skills" / "room-collab" / "scripts" / "room_collab.py"
    env = {**os.environ, "AG2SPACE_USER_ID": "@sudoo:x"}
    env.pop("AG2_MATRIX_USER_ID", None)
    r = subprocess.run([*pybase, str(cli), "--workspace", str(ws4), "stay", "!a:x"],
                       capture_output=True, text=True, env=env, timeout=120)
    check("`stay` succeeds with no flags and no token", r.returncode == 0,
          (r.stdout + r.stderr)[-200:])
    regd = store.read_entries(dm.desired_path(ws4))
    check("...writing a record that outlives the process", len(regd) == 1)
    # ⚠ Owner's review: raw args registered identity=None and name=None, and
    # no name means the daemon holds a socket nobody can see.
    check("...carrying a resolved identity, not None",
          regd and regd[0].get("identity") == "@sudoo:x", str(regd))
    check("...and a presence name, without which the daemon joins invisibly",
          bool(regd and regd[0].get("name")), str(regd))

    r2 = subprocess.run([*pybase, str(cli), "--workspace", str(ws4),
                         "stay", "!a:x", "--leave"],
                        capture_output=True, text=True, env=env, timeout=120)
    check("`--leave` deregisters through the same record",
          r2.returncode == 0 and store.read_entries(dm.desired_path(ws4)) == [])

    skill = (REPO / "skills" / "room-collab" / "SKILL.md").read_text(encoding="utf-8")
    # An agent does what the skill says; leaving "hold it open yourself" in it
    # keeps the normal path on the old mechanism.
    check("the skill tells an agent to register, not to hold the surface open",
          "stay '!room:server'" in skill and "you must HOLD it open" not in skill)

    print("── the cap ──")
    d2 = dm.Daemon(TMP, "https://example.invalid", "tok", cap=2, clock=lambda: clock["t"])
    want([entry(f"!r{n}", summoned_at=NOW + n) for n in range(4)])
    asyncio.run(d2.reconcile(NOW + 10))
    check("never more than the cap are held at once", len(d2.held) == 2)
    check("the newest summons win the slots",
          sorted(k[0] for k in d2.held) == ["!r2", "!r3"])

    print("── an unreadable desired record must not evict anyone ──")
    ws8 = Path(tempfile.mkdtemp(prefix="presence-unreadable-"))
    store.write_entries(dm.desired_path(ws8), [entry("!hold")])
    d7 = dm.Daemon(ws8, "u", "t", clock=lambda: NOW, max_backoff=0.01)
    asyncio.run(d7.reconcile(NOW))
    check("the surface is held to begin with", ("!hold", "markdown") in d7.held)
    import stat as _stat
    dm.desired_path(ws8).chmod(0)
    try:
        asyncio.run(d7.reconcile(NOW + 1))
        # The live run evicted here, with reason `left`, over a permission blip.
        check("an unreadable record leaves the surface held",
              ("!hold", "markdown") in d7.held)
    finally:
        dm.desired_path(ws8).chmod(_stat.S_IRUSR | _stat.S_IWUSR)
    asyncio.run(d7.reconcile(NOW + 2))
    check("...and it is still held once the record is readable again",
          ("!hold", "markdown") in d7.held)

    print("── the loop itself: it keeps going, and a bad pass does not end it ──")
    ws7 = Path(tempfile.mkdtemp(prefix="presence-run-"))
    store.write_entries(dm.desired_path(ws7), [entry("!loop")])
    d6 = dm.Daemon(ws7, "u", "t", clock=lambda: NOW, max_backoff=0.01)
    passes = {"n": 0}
    real = d6.reconcile

    async def counted(now):
        passes["n"] += 1
        # The second pass raises: a reconciler that dies on one bad pass leaves
        # the agent absent everywhere until someone notices.
        if passes["n"] == 2:
            raise RuntimeError("one bad pass")
        await real(now)

    d6.reconcile = counted
    dm.RECONCILE_SECONDS, keep_interval = 0.01, dm.RECONCILE_SECONDS

    async def run_briefly():
        task = asyncio.ensure_future(d6.run())
        for _ in range(200):
            await asyncio.sleep(0.005)
            if passes["n"] >= 3:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(run_briefly())
    finally:
        dm.RECONCILE_SECONDS = keep_interval
    check("run() reconciles repeatedly rather than once", passes["n"] >= 3, str(passes))
    check("...and survives a pass that raises", ("!loop", "markdown") in d6.held)

    print("── the entry point's own arguments ──")
    entry = REPO / "skills" / "room-collab" / "scripts" / "presence_daemon.py"
    rc_help = subprocess.run([*pybase, str(entry), "--help"],
                             capture_output=True, text=True, env=env, timeout=120)
    check("--help lists the knobs the owner set", rc_help.returncode == 0
          and "--idle-seconds" in rc_help.stdout and "--cap" in rc_help.stdout,
          rc_help.stdout[:120])

    print("── the CLI's json form, and the daemon's own arguments ──")
    ws6 = Path(tempfile.mkdtemp(prefix="presence-json-"))
    rj = subprocess.run([*pybase, str(cli), "--workspace", str(ws6), "--json", "stay", "!j:x"],
                 capture_output=True, text=True, env=env, timeout=120)
    ok_json = False
    try:
        import json as _json
        parsed = _json.loads(rj.stdout.strip().splitlines()[-1])
        ok_json = parsed.get("ok") is True and len(parsed.get("entries", [])) == 1
    except Exception:
        ok_json = False
    check("`--json` prints a machine-readable receipt", rj.returncode == 0 and ok_json,
          (rj.stdout + rj.stderr)[-160:])

    # The knobs the owner set are arguments, not constants: a daemon whose
    # --cap and --idle-seconds are ignored would look configured and not be.
    d5 = dm.Daemon(TMP, "u", "t", idle_seconds=7.0, cap=3, clock=lambda: NOW)
    check("--idle-seconds reaches the policy", d5.idle_seconds == 7.0)
    check("--cap reaches the policy", d5.cap == 3)

    print("── the client's end-of-session signal ──")
    # `closed()` is the whole reason a holder can notice a dead socket; without
    # a test it is one await away from silently never resolving again.
    try:
        # By file, not by name: sys.modules holds this suite's stub under that
        # name, and importing it would test the stub.
        _spec = importlib.util.spec_from_file_location(
            "room_collab_client_real", SCRIPTS / "room_collab_client.py")
        rcc = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(rcc)
    except (Exception, SystemExit) as exc:
        # The client EXITS at import when its deps are absent, so SystemExit is
        # the normal outcome on a bare interpreter — not a failure of this file.
        check("client end-of-session signal (skipped: deps absent)", True, str(exc)[:60])
    else:
        async def ends():
            doc = object.__new__(rcc.RoomDoc)
            fut = asyncio.get_event_loop().create_future()
            doc._ended = fut
            waiting = asyncio.ensure_future(doc.closed())
            await asyncio.sleep(0)
            pending = not waiting.done()
            fut.set_result(rcc.SessionEnd() if hasattr(rcc, "SessionEnd") else None)
            return pending, await waiting

        pending, err = asyncio.run(ends())
        check("it does not resolve while the session is open", pending)
        check("...and resolves with the reason once it ends", isinstance(err, Exception),
              repr(err)[:80])

    print("── the entry point refuses rather than looping without credentials ──")
    # Only what an interpreter needs: naming the credential variables here
    # would duplicate the skill's own list, and a copy of it drifts.
    bare = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    daemon_cli = REPO / "skills" / "room-collab" / "scripts" / "presence_daemon.py"
    rc = subprocess.run([*pybase, str(daemon_cli), "--workspace", str(TMP)],
                 capture_output=True, text=True, env=bare, timeout=120)
    check("no credentials exits non-zero instead of supervising nothing", rc.returncode == 2,
          f"rc={rc.returncode}")
    check("...naming what is missing", "no service URL" in (rc.stdout + rc.stderr),
          (rc.stdout + rc.stderr)[-160:])

    print("── one daemon per workspace ──")
    # Two copies would open duplicate sockets for the same agent and both write
    # `live`, so each would read the other's surfaces as unheld.
    lock_ws = Path(tempfile.mkdtemp())
    first = dm.acquire_singleton(lock_ws)
    check("the first copy acquires the lock", first is not None)
    check("a second copy is refused", dm.acquire_singleton(lock_ws) is None)
    if first is not None:
        first.close()
    # Keep the handle: the lock lives on the open file description, so dropping
    # the reference lets GC close it and release the lock under the next caller.
    again = dm.acquire_singleton(lock_ws)
    check("...and the lock is free once it exits", again is not None)
    # main() must refuse BEFORE opening a socket, so credentials it never uses
    # are supplied; if the refusal moved after the connect this call would hang.
    rc = dm.main(["--workspace", str(lock_ws), "--url", "wss://example.invalid",
                  "--token", "unused"])
    check("a second copy exits non-zero from main() rather than connecting", rc == 3, f"rc={rc}")
    if again is not None:
        again.close()
    shutil.rmtree(lock_ws, ignore_errors=True)

    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all presence-daemon checks ok'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
