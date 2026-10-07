#!/usr/bin/env python3
"""pool_beat: the beat is an mtime, and absent is not stale.

Each rule here is the design's (`docs/worker-pool-design.md`), not a preference:
the beat is refreshed every 30 s and stale at 90 s, a future-dated beat counts
as stale, and the file carries no payload. `absent` is a third state because a
host that never ran a beat writer and a worker that died are indistinguishable
to a two-valued check, and reading the first as death would abandon every worker
on the release that introduces beats.
"""
import contextlib
import importlib.util as u
import io
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
spec = u.spec_from_file_location(
    "pool_beat", HERE.parent / "skills" / "worker-pool" / "scripts" / "pool_beat.py")
pb = u.module_from_spec(spec)
spec.loader.exec_module(pb)

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


SB = tempfile.mkdtemp()
NOW = 1_000_000.0

# --- the paths the design names ------------------------------------------

# endswith, not a slice: a one-character offset error reports the
# implementation broken when only the assertion is.
check("worker beat path", str(pb.beat_path("/ws", "worker", "abc")).endswith("/state/workers/abc.alive"), True)
check("watcher beat path", str(pb.beat_path("/ws", "watcher", "abc")).endswith("/state/watchers/abc.alive"), True)
try:
    pb.beat_path("/ws", "core", "abc"); got = "accepted"
except ValueError:
    got = "refused"
check("an unknown kind is refused, not guessed", got, "refused")
for bad in ("", "../escape", "a/b"):
    try:
        pb.beat_path("/ws", "worker", bad); got = "accepted"
    except ValueError:
        got = "refused"
    check(f"id {bad!r} refused", got, "refused")

# --- the cadence constants come from the design --------------------------
check("refresh interval is 30s", pb.BEAT_INTERVAL_S, 30.0)
check("stale after 90s", pb.STALE_AFTER_S, 90.0)

# --- mtime only ----------------------------------------------------------
p = pathlib.Path(SB) / "state" / "workers" / "w1.alive"
pb.touch(p)
check("beat file is created", p.exists(), True)
check("beat carries NO payload (mtime only)", p.stat().st_size, 0)
pre = p.stat().st_mtime
os.utime(p, (pre - 500, pre - 500))
pb.touch(p)
check("touch refreshes the mtime", p.stat().st_mtime > pre - 500, True)
check("...and still writes no bytes", p.stat().st_size, 0)

# --- classify: three states ----------------------------------------------
os.utime(p, (NOW - 10, NOW - 10))
check("fresh beat is live", pb.classify(p, NOW), pb.LIVE)
os.utime(p, (NOW - 89, NOW - 89))
check("89s is still live (boundary is 90, inclusive)", pb.classify(p, NOW), pb.LIVE)
os.utime(p, (NOW - 90, NOW - 90))
check("exactly 90s is live", pb.classify(p, NOW), pb.LIVE)
os.utime(p, (NOW - 91, NOW - 91))
check("91s is stale", pb.classify(p, NOW), pb.STALE)
os.utime(p, (NOW + 500, NOW + 500))
check("a FUTURE-dated beat is stale, per the design", pb.classify(p, NOW), pb.STALE)
check("a beat that was never written is ABSENT, not stale",
      pb.classify(pathlib.Path(SB) / "state" / "workers" / "never.alive", NOW), pb.ABSENT)
check("absent and stale are distinct tokens", pb.ABSENT != pb.STALE, True)

# --- the discriminator ---------------------------------------------------

# Rejects the two implementations that would pass trivially: always-stale,
# and folding absent into stale.
print("\n  -- control: the suite must reject the obvious wrong implementations --")
os.utime(p, (NOW - 1, NOW - 1))
check("a fresh beat does NOT read stale (rejects always-stale)", pb.classify(p, NOW), pb.LIVE)
check("absent does NOT read stale (rejects folding the two)",
      pb.classify(pathlib.Path(SB) / "nope.alive", NOW) == pb.STALE, False)

# --- the unreadable branch -----------------------------------------------

# A symlink loop is the cheapest real OSError that is neither missing nor a
# non-directory; unreadable must not read as absent.
_a = pathlib.Path(SB) / "loop_a"
_b = pathlib.Path(SB) / "loop_b"
os.symlink(_b, _a)
os.symlink(_a, _b)
check("an unreadable beat is UNKNOWN, never absent", pb.classify(_a, NOW), pb.UNKNOWN)

# @yixuan-ag2's discriminator on #4421: the docstring told the caller not to act on
# an unreadable beat while handing back a value identical to a genuine stale one.
os.utime(p, (NOW - 500, NOW - 500))
check("unreadable is DISTINGUISHABLE from genuinely stale",
      pb.classify(_a, NOW) != pb.classify(p, NOW), True)
check("...and the genuinely stale one still reads stale", pb.classify(p, NOW), pb.STALE)
check("unknown is not live either", pb.classify(_a, NOW) != pb.LIVE, True)

# --- run_forever refreshes, it does not just create ----------------------

class _Stop(Exception):
    pass

_slept = {"n": 0}
_real_sleep = pb.time.sleep


def _fake_sleep(_s):
    _slept["n"] += 1
    if _slept["n"] >= 2:
        raise _Stop


_rf = pathlib.Path(SB) / "state" / "workers" / "rf.alive"
pb.time.sleep = _fake_sleep
try:
    pb.run_forever(_rf, 0.01)
except _Stop:
    pass
finally:
    pb.time.sleep = _real_sleep
check("run_forever created the beat", _rf.exists(), True)
check("run_forever kept refreshing (slept more than once)", _slept["n"] >= 2, True)

# --- the CLI -------------------------------------------------------------

check("--once writes one beat and returns 0",
      pb.main(["--workspace", SB, "--kind", "worker", "--id", "m1", "--once"]), 0)
check("...and the file is there",
      (pathlib.Path(SB) / "state" / "workers" / "m1.alive").exists(), True)
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    _rc = pb.main(["--workspace", SB, "--kind", "worker", "--id", "m1", "--read"])
check("--read returns 0", _rc, 0)
check("--read PRINTS the state of that same beat", _buf.getvalue().strip(), pb.LIVE)
_delegated = {}
_real_rf = pb.run_forever


def _spy(path, interval, *, parent_pid=None):
    _delegated["path"], _delegated["interval"] = path, interval
    _delegated["parent_pid"] = parent_pid
    return 0


pb.run_forever = _spy
try:
    _rc = pb.main(["--workspace", SB, "--kind", "worker", "--id", "m2", "--interval", "7"])
finally:
    pb.run_forever = _real_rf
check("with neither --once nor --read the CLI runs the daemon", _rc, 0)
check("...on the beat path it was asked for",
      str(_delegated.get("path", "")).endswith("/state/workers/m2.alive"), True)
check("...at the interval it was given", _delegated.get("interval"), 7.0)
check("...with no parent to watch unless one is named", _delegated.get("parent_pid"), None)
pb.run_forever = _spy
try:
    pb.main(["--workspace", SB, "--kind", "watcher", "--id", "m3", "--parent-pid", "4242"])
finally:
    pb.run_forever = _real_rf
check("--parent-pid reaches the daemon", _delegated.get("parent_pid"), 4242)

# --- the beat stops when the process it speaks for dies -------------------

# SIGKILL and a crash run no trap, so nothing tells the beat to stop: it looks.


def _raises(exc):
    def _k(_pid, _sig):
        raise exc
    return _k


check("a live parent is not gone",
      pb.parent_gone(7, getppid=lambda: 7, kill=lambda p, s: None), False)
check("a parent whose pid no longer exists is gone",
      pb.parent_gone(7, getppid=lambda: 7, kill=_raises(ProcessLookupError())), True)
check("reparented means gone, even if the pid was recycled and answers",
      pb.parent_gone(7, getppid=lambda: 1, kill=lambda p, s: None), True)
check("a parent we may not signal is still a parent",
      pb.parent_gone(7, getppid=lambda: 7, kill=_raises(PermissionError())), False)

_real_pg = pb.parent_gone
_dead = pathlib.Path(SB) / "state" / "watchers" / "born-orphaned.alive"
pb.parent_gone = lambda _pid: True
try:
    _rc = pb.run_forever(_dead, 30.0, parent_pid=7)
finally:
    pb.parent_gone = _real_pg
check("a beat whose parent is already gone returns at once", _rc, 0)
check("...and never writes a fresh mtime for it", _dead.exists(), False)

# Parent alive across one full interval, then dead: exactly one refresh happens,
# and the loop returns from INSIDE the next sleep rather than finishing it.
_polls = {"n": 0}
_touches = {"n": 0}
_real_touch = pb.touch


def _gone_after_three(_pid):
    _polls["n"] += 1
    return _polls["n"] > 3


def _count_touch(path):
    _touches["n"] += 1
    _real_touch(path)


_lived = pathlib.Path(SB) / "state" / "watchers" / "lived.alive"
pb.parent_gone, pb.touch, pb.time.sleep = _gone_after_three, _count_touch, (lambda _s: None)
try:
    _rc = pb.run_forever(_lived, 0.02, parent_pid=7, poll_s=0.01)
finally:
    pb.parent_gone, pb.touch, pb.time.sleep = _real_pg, _real_touch, _real_sleep
check("the daemon returns 0 when its parent dies", _rc, 0)
check("it refreshed once at start and once after a full interval", _touches["n"], 2)
check("it polled inside the interval, not once per 30 s", _polls["n"], 4)

try:
    pb.main(["--workspace", SB, "--kind", "nope", "--id", "m1", "--once"])
    _bad = "accepted"
except SystemExit:
    _bad = "refused"
check("the CLI refuses an unknown --kind", _bad, "refused")

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — pool_beat (46 checks)")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
