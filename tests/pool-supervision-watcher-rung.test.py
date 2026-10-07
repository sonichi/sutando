#!/usr/bin/env python3
"""pool_supervision: the watcher rung — a lost watcher under a live session.

The session ladder needs the session GONE. A session that is alive but whose
inbox nobody watches is the other half of the design's supervision: the beat
of the watcher expires or never existed, and no session-role watcher holds
the inbox. Same three-tick sustain, same 90 s line, one `rearm_watcher`, then
the owner at 3 minutes. Two things must never count: a stale beat whose inbox
IS held (a watcher launched before beat injection beats nothing yet serves the
inbox), and a holder check that could not be told.
"""
import importlib.util as u
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
spec = u.spec_from_file_location(
    "pool_supervision",
    HERE.parent / "skills" / "worker-pool" / "scripts" / "pool_supervision.py")
ps = u.module_from_spec(spec)
sys.modules["pool_supervision"] = ps
spec.loader.exec_module(ps)

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def lost(beat=ps.STALE, held=False):
    return ps.Observation(beat=ps.LIVE, session_alive=True,
                          watcher_beat=beat, watcher_held=held)


def fine():
    return ps.Observation(beat=ps.LIVE, session_alive=True, watcher_beat=ps.LIVE)


def tick(state, obs, now, **kw):
    kw.setdefault("expected_period_s", 30.0)
    return ps.evaluate(state, obs, now, **kw)


S0 = ps.SupervisionState()

# --- what counts as a lost watcher -----------------------------------------

_, d = tick(S0, {"w": ps.Observation(beat=ps.LIVE, session_alive=True)}, 100.0)
check("an observation without watcher fields decides nothing", d["w"], ps.NOTHING)

_, d = tick(S0, {"w": lost(held=True)}, 100.0)
check("a stale beat with a session-role holder is NOT loss", d["w"], ps.NOTHING)
st, _ = tick(S0, {"w": lost(held=True)}, 100.0)
check("...and counts no evidence", st.workers["w"].watcher_consecutive, 0)

st, _ = tick(S0, {"w": lost(held=None)}, 100.0)
check("an unproven holder check is not evidence", st.workers["w"].watcher_consecutive, 0)

st, _ = tick(S0, {"w": ps.Observation(beat=ps.STALE, session_alive=None,
                                      watcher_beat=ps.ABSENT, watcher_held=False)}, 100.0)
check("no watcher ladder without a session that answered alive",
      st.workers.get("w", ps.WorkerEvidence()).watcher_consecutive, 0)

st, _ = tick(S0, {"w": lost(beat=ps.ABSENT)}, 100.0)
check("an ABSENT beat with no holder is evidence", st.workers["w"].watcher_consecutive, 1)

st, _ = tick(S0, {"w": lost(beat=ps.UNKNOWN)}, 100.0)
check("an UNREADABLE beat is not evidence", st.workers["w"].watcher_consecutive, 0)

# an unproven tick holds what was counted, it does not reset it
s1, _ = tick(S0, {"w": lost()}, 100.0)
s2, _ = tick(s1, {"w": lost(held=None)}, 130.0)
check("an unproven holder check does not reset counted evidence",
      s2.workers["w"].watcher_consecutive, 1)

# --- the ladder --------------------------------------------------------------

st = ps.SupervisionState()
for t in (100.0, 130.0, 160.0):
    st, d = tick(st, {"w": lost()}, t)
check("sustained but under 90 s does not re-arm", d["w"], ps.NOTHING)
check("the watcher clock started at FIRST detection",
      st.workers["w"].watcher_first_detected_at, 100.0)

st, d = tick(st, {"w": lost()}, 200.0)
check("past 90 s with sustained loss asks for a re-arm", d["w"], ps.REARM_WATCHER)
check("the re-arm is stamped", st.workers["w"].rearm_issued_at, 200.0)

st, d = tick(st, {"w": lost()}, 230.0)
check("the re-arm is issued once, not every tick", d["w"], ps.NOTHING)

st, d = tick(st, {"w": lost()}, 281.0)
check("still lost past 3 min from first detection escalates", d["w"], ps.ESCALATE)

st, d = tick(st, {"w": lost()}, 311.0)
check("escalation is issued once", d["w"], ps.NOTHING)

# --- what clears it ----------------------------------------------------------

st, d = tick(st, {"w": fine()}, 341.0)
check("a live watcher beat clears the ladder", d["w"], ps.NOTHING)
check("...all of it", st.workers["w"].rearm_issued_at, None)

st, _ = tick(S0, {"w": lost()}, 100.0)
st, _ = tick(st, {"w": lost(held=True)}, 130.0)
check("a session-role holder appearing clears counted evidence",
      st.workers["w"].watcher_consecutive, 0)

# --- the two ladders are separate --------------------------------------------

dead = ps.Observation(beat=ps.STALE, session_alive=False, watcher_beat=ps.ABSENT,
                      watcher_held=False)
st = ps.SupervisionState()
for t in (100.0, 130.0, 160.0, 200.0):
    st, d = tick(st, {"w": dead}, t)
check("a dead session takes the session ladder, never the watcher's", d["w"], ps.RECOVER)
check("...and counts no watcher evidence", st.workers["w"].watcher_consecutive, 0)

# the session came back (recovered) but its watcher is lost: the session ladder
# clears while the watcher ladder starts fresh from this tick
st, d = tick(st, {"w": lost()}, 230.0)
check("a live session clears the session ladder", st.workers["w"].consecutive, 0)
check("...and the watcher ladder starts counting", st.workers["w"].watcher_consecutive, 1)

# the wake rule covers the watcher ladder too: a gap the sweep cannot explain
# discards the sample
st = ps.SupervisionState(last_sample_at=1000.0, workers={"w": ps.WorkerEvidence(
    watcher_first_detected_at=900.0, watcher_consecutive=3)})
st2, d = tick(st, {"w": lost()}, 1000.0 + 300.0 + 61.0, expected_period_s=300.0)
check("a resume tick decides nothing for the watcher ladder", d["w"], ps.NOTHING)
check("...and leaves its evidence as it was", st2.workers["w"].watcher_consecutive, 3)

# --- owner-paused outranks the watcher ladder too ------------------------------

_, d = tick(S0, {"w": ps.Observation(beat=ps.LIVE, session_alive=True, paused=True,
                                     watcher_beat=ps.ABSENT, watcher_held=False)}, 100.0)
check("a paused worker's lost watcher decides nothing", d["w"], ps.NOTHING)

n = 26
print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — pool_supervision watcher rung ({n} checks)")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
