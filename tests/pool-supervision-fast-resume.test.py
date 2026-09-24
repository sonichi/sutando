#!/usr/bin/env python3
"""A dead worker that still owes work recovers on the first confirming tick.

After a host restart every worker session is gone, and the first sample is a
resume sample the ladder discards, so a worker holding a delivery waited the
full three-tick sustain on top of it: resume + 3 x 300 s. A gone session is not
something a sleep explains, so for a worker whose own inbox still holds work the
resume sample now counts as first detection and the next tick confirms it; with
no resume in between, the tick after first detection confirms. A worker that
owes nothing keeps the full sustain, and owner-paused still outranks everything.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "pool_supervision", REPO / "skills" / "worker-pool" / "scripts" / "pool_supervision.py")
ps = importlib.util.module_from_spec(spec)
sys.modules["pool_supervision"] = ps
spec.loader.exec_module(ps)

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def dead(work, paused=False):
    try:
        return ps.Observation(beat=ps.STALE, session_alive=False, paused=paused,
                              work_outstanding=work)
    except TypeError:          # the parent commit has no work field
        return ps.Observation(beat=ps.STALE, session_alive=False, paused=paused)


def ticks(obs, n, *, last_sample_at, first_at):
    """n ticks 300 s apart starting at `first_at`; returns decisions and the state."""
    st = ps.SupervisionState(last_sample_at=last_sample_at)
    out, t = [], first_at
    for _ in range(n):
        st, d = ps.evaluate(st, {"w": obs}, t)
        out.append(d["w"])
        t += 300.0
    return out, st


# Still dead a tick after recovery is the owner's (the ladder's rung 2).
N, R, E = ps.NOTHING, ps.RECOVER, ps.ESCALATE
BOOT = 100_000.0          # the timer's first run after a reboot: hours after its last

got, st = ticks(dead(True), 3, last_sample_at=BOOT - 8 * 3600, first_at=BOOT)
check("after a host restart, a worker owing work recovers one tick after the resume sample",
      got, [N, R, E])

got, _ = ticks(dead(False), 5, last_sample_at=BOOT - 8 * 3600, first_at=BOOT)
check("a worker owing nothing keeps the full sustain after the resume sample",
      got, [N, N, N, R, E])

got, _ = ticks(dead(None), 5, last_sample_at=BOOT - 8 * 3600, first_at=BOOT)
check("an unreadable queue is not evidence of work: full sustain", got, [N, N, N, R, E])

got, _ = ticks(dead(True), 3, last_sample_at=BOOT - 300.0, first_at=BOOT)
check("with no resume, the tick after first detection confirms", got, [N, R, E])

got, _ = ticks(dead(True, paused=True), 4, last_sample_at=BOOT - 8 * 3600, first_at=BOOT)
check("owner-paused outranks the fast lane", got, [N, N, N, N])

st = ps.SupervisionState(last_sample_at=BOOT - 8 * 3600)
st, d = ps.evaluate(st, {"w": dead(True)}, BOOT, detect_after_s=90.0)
st, d = ps.evaluate(st, {"w": dead(True)}, BOOT + 30.0, detect_after_s=90.0)
check("the fast lane still waits for the 90 s stale line", d["w"], N)
st, d = ps.evaluate(st, {"w": dead(True)}, BOOT + 120.0, detect_after_s=90.0)
check("...and recovers once it passes", d["w"], R)

live = ps.Observation(beat=ps.LIVE, session_alive=True, watcher_beat=ps.LIVE)
st, d = ps.evaluate(ps.SupervisionState(last_sample_at=BOOT - 8 * 3600), {"w": live}, BOOT)
check("a resume sample of a live worker records nothing", st.workers.get("w"), None)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — pool_supervision fast resume")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
