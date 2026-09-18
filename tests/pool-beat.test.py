#!/usr/bin/env python3
"""pool_beat: the beat is an mtime, and absent is not stale.

Each rule here is the design's (`docs/worker-pool-design.md`), not a preference:
the beat is refreshed every 30 s and stale at 90 s, a future-dated beat counts
as stale, and the file carries no payload. `absent` is a third state because a
host that never ran a beat writer and a worker that died are indistinguishable
to a two-valued check, and reading the first as death would abandon every worker
on the release that introduces beats.
"""
import importlib.util as u
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

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — pool_beat (20 checks)")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
