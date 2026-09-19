#!/usr/bin/env python3
"""pool_supervision: the recovery ladder, and the wake rule that keeps it honest.

The rungs are the design's (`docs/worker-pool-design.md`) and the owner's
settlement: process death needs an expired beat AND a gone session AND a
sustained reading, owner-paused outranks every signal, recovery is silent, and
only a recovery that has not worked by 3 minutes reaches the owner.

The wake rule earns its own section. A host sleep expires every beat at once, so
the first sample after one shows the whole pool as dead. CI never produces a real
sleep, so the gap check is asserted by simulation AND by a control that removes
it — without that control, a deleted gap check still passes every other test here.
"""
import importlib.util as u
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
spec = u.spec_from_file_location(
    "pool_supervision",
    HERE.parent / "skills" / "worker-pool" / "scripts" / "pool_supervision.py")
ps = u.module_from_spec(spec)
# Registered BEFORE exec: @dataclass resolves field types through
# sys.modules[cls.__module__], which is None for an unregistered spec load.
sys.modules["pool_supervision"] = ps
spec.loader.exec_module(ps)

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def dead(paused=False):
    return ps.Observation(beat=ps.STALE, session_alive=False, paused=paused)


def alive():
    return ps.Observation(beat=ps.LIVE, session_alive=True)


def tick(state, obs, now, **kw):
    return ps.evaluate(state, obs, now, **kw)


def run_ticks(state, obs_for, times, **kw):
    """Feed a sequence of sample times; return (state, last decisions)."""
    dec = {}
    for t in times:
        state, dec = tick(state, obs_for(t), t, **kw)
    return state, dec


# --- the design's constants, pinned ---------------------------------------

check("stale line is the design's 90 s", ps.DETECT_AFTER_S, 90.0)
check("sustained is the design's three ticks", ps.SUSTAINED_TICKS, 3)
check("the sweep is the design's five minutes", ps.SAMPLE_PERIOD_S, 300.0)
check("escalation is the owner's 3 minutes", ps.ESCALATE_AFTER_S, 180.0)

# --- what counts as death -------------------------------------------------

S0 = ps.SupervisionState()

_, d = tick(S0, {"w": ps.Observation(beat=ps.STALE, session_alive=True)}, 100.0)
check("expired beat with a LIVE session is not death", d["w"], ps.NOTHING)

_, d = tick(S0, {"w": ps.Observation(beat=ps.STALE, session_alive=None)}, 100.0)
check("expired beat with an UNANSWERED probe is not death", d["w"], ps.NOTHING)

_, d = tick(S0, {"w": ps.Observation(beat=ps.UNKNOWN, session_alive=False)}, 100.0)
check("an UNREADABLE beat is not death", d["w"], ps.NOTHING)

_, d = tick(S0, {"w": ps.Observation(beat=ps.ABSENT, session_alive=False)}, 100.0)
check("an ABSENT beat with a gone session IS evidence", d["w"], ps.NOTHING)  # not yet sustained

# an unreadable beat must not erase a death already counted
s1, _ = tick(S0, {"w": dead()}, 100.0)
s2, _ = tick(s1, {"w": ps.Observation(beat=ps.UNKNOWN, session_alive=None)}, 130.0)
check("an unreadable tick does not reset counted evidence",
      s2.workers["w"].consecutive, 1)

# --- the ladder -----------------------------------------------------------

# three sustained ticks, 30 s apart: sustained is met at t=160 but only 60 s has
# elapsed since first detection, so the 90 s rung is not reached yet.
st, d = run_ticks(ps.SupervisionState(), lambda t: {"w": dead()},
                  [100.0, 130.0, 160.0], expected_period_s=30.0)
check("sustained but under 90 s does not recover", d["w"], ps.NOTHING)
check("the clock started at FIRST detection", st.workers["w"].first_detected_at, 100.0)

st, d = tick(st, {"w": dead()}, 200.0, expected_period_s=30.0)
check("past 90 s with sustained evidence recovers", d["w"], ps.RECOVER)

st, d = tick(st, {"w": dead()}, 230.0, expected_period_s=30.0)
check("recovery is issued once, not every tick", d["w"], ps.NOTHING)

st, d = tick(st, {"w": dead()}, 281.0, expected_period_s=30.0)
check("still dead past 3 min from first detection escalates", d["w"], ps.ESCALATE)

st, d = tick(st, {"w": dead()}, 311.0, expected_period_s=30.0)
check("escalation is issued once", d["w"], ps.NOTHING)

# recovery that worked clears everything
st, d = tick(st, {"w": alive()}, 341.0, expected_period_s=30.0)
check("a live beat clears the ladder", d["w"], ps.NOTHING)
check("a live beat clears the evidence", st.workers["w"], ps.WorkerEvidence())

# --- owner-paused ---------------------------------------------------------

stp, d = run_ticks(ps.SupervisionState(), lambda t: {"w": dead(paused=True)},
                   [100.0, 130.0, 160.0, 200.0, 400.0], expected_period_s=30.0)
check("a paused worker is never recovered", d["w"], ps.NOTHING)
check("observing a paused worker records no evidence",
      stp.workers.get("w", ps.WorkerEvidence()), ps.WorkerEvidence())

# a pause arriving mid-ladder stops it, and does not clear what was seen
stm, _ = run_ticks(ps.SupervisionState(), lambda t: {"w": dead()},
                   [100.0, 130.0, 160.0], expected_period_s=30.0)
stm2, d = tick(stm, {"w": dead(paused=True)}, 200.0, expected_period_s=30.0)
check("a pause mid-ladder blocks the recovery that was due", d["w"], ps.NOTHING)
check("a pause does not erase the evidence already counted",
      stm2.workers["w"].consecutive, 3)

# --- the wake rule --------------------------------------------------------

check("a gap inside the period is not a resume",
      ps.is_resume(1000.0, 700.0, expected_period_s=300.0, slack_s=60.0), False)
check("a gap just past period+slack IS a resume",
      ps.is_resume(1361.0, 1000.0, expected_period_s=300.0, slack_s=60.0), True)
check("the first sample ever is not a resume", ps.is_resume(1000.0, None), False)

# The scenario that matters: a laptop sleeps for two hours. Every beat in the
# pool is expired on the first sample after the lid opens.
POOL = {f"w{i}": dead() for i in range(5)}
pre, _ = run_ticks(ps.SupervisionState(), lambda t: POOL, [0.0], expected_period_s=300.0)
after, d = tick(pre, POOL, 7200.0, expected_period_s=300.0)
check("a whole pool expired after a sleep recovers NOBODY",
      sorted(set(d.values())), [ps.NOTHING])
check("the sleep sample is discarded as evidence, not counted",
      [after.workers.get(w, ps.WorkerEvidence()).consecutive for w in sorted(POOL)],
      [1, 1, 1, 1, 1])

# --- the control: the same sleep, with the gap check removed -------------

# If removing it does not change the outcome, the assertions above are
# decorative rather than caused by the gap check.

_real_is_resume = ps.is_resume
ps.is_resume = lambda *a, **k: False          # the gap check, deleted
try:
    ctrl_after, ctrl_d = tick(pre, POOL, 7200.0, expected_period_s=300.0)
finally:
    ps.is_resume = _real_is_resume

check("CONTROL: without the gap check the sleep counts as evidence",
      [ctrl_after.workers[w].consecutive for w in sorted(POOL)], [2, 2, 2, 2, 2])
check("CONTROL: and the ladder's clock is then already 2 h old",
      ctrl_after.workers["w0"].first_detected_at, 0.0)
# one more tick without the gap check reaches `sustained` and relaunches the pool
ctrl_after2, ctrl_d2 = tick(ctrl_after, POOL, 7201.0, expected_period_s=300.0)
check("CONTROL: the very next tick relaunches the WHOLE pool",
      sorted(set(ctrl_d2.values())), [ps.RECOVER])

# and with the gap check in place the same two ticks do not
real_after2, real_d2 = tick(after, POOL, 7201.0, expected_period_s=300.0)
check("with the gap check, the same two ticks recover nobody",
      sorted(set(real_d2.values())), [ps.NOTHING])

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — pool_supervision (30 checks)")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
