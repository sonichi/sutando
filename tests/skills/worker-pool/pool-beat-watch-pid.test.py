#!/usr/bin/env python3
"""pool_beat --watch-pid: a beat that answers about an ARBITRARY pid, not our
own OS parent.

A worker's own beat cannot be spawned as a direct OS child of the claude
process it speaks for: every process this skill can start from inside a
running session is born through a tool call whose own shell exits when the
call returns, orphaning any background child to PID 1 (measured live on
sonichi/sutando#4421's follow-up). `--parent-pid`'s `getppid()`-based check
would then read "parent gone" within about a second of the very first poll,
regardless of whether the watched process is alive. `target_gone` asks by
signal instead, so the beat writer's own reparenting is irrelevant to what
it reports on — and it guards against pid reuse the same way `parent_gone`
guards against reparenting: a signal to a RECYCLED pid still succeeds, so a
process-start-time mismatch is what tells the two apart.
"""
import contextlib
import importlib.util as u
import io
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
spec = u.spec_from_file_location(
    "pool_beat", HERE.parents[2] / "skills" / "worker-pool" / "scripts" / "pool_beat.py")
pb = u.module_from_spec(spec)
spec.loader.exec_module(pb)

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


SB = tempfile.mkdtemp()


def _raises(exc):
    def _k(_pid, _sig):
        raise exc
    return _k


# --- process_start_time ----------------------------------------------------

def _lstart_ok(argv, **kw):
    class R:
        returncode = 0
        stdout = "Fri Sep 19 09:00:00 2026\n"
    return R()


check("process_start_time returns ps -o lstart= verbatim, stripped",
      pb.process_start_time(4242, run=_lstart_ok), "Fri Sep 19 09:00:00 2026")


def _lstart_missing(argv, **kw):
    class R:
        returncode = 1
        stdout = ""
    return R()


check("process_start_time is empty when ps refuses (pid already gone)",
      pb.process_start_time(4242, run=_lstart_missing), "")


def _lstart_raises(argv, **kw):
    raise OSError("ps not found")


check("process_start_time is empty when the run() itself fails, not a raise",
      pb.process_start_time(4242, run=_lstart_raises), "")

# --- target_gone -------------------------------------------------------------

check("a live watched pid, matching start time, is not gone",
      pb.target_gone(7, "T0", kill=lambda p, s: None, lstart=lambda p: "T0"), False)
check("a watched pid that no longer exists is gone",
      pb.target_gone(7, "T0", kill=_raises(ProcessLookupError()), lstart=lambda p: "T0"), True)
check("a watched pid we may not signal is still there (existence, not permission)",
      pb.target_gone(7, "T0", kill=_raises(PermissionError()), lstart=lambda p: "T0"), False)
check("empty start_time (never recorded) skips the reuse check entirely",
      pb.target_gone(7, "", kill=lambda p, s: None, lstart=lambda p: "DIFFERENT"), False)
print("\n  -- john-the-dev on PR #4452: an unreadable ps must not read as reuse --")
check("kill(pid,0) SUCCEEDS (pid exists) but ps can't be read: not gone, "
      "not a false reuse signal off a transient failure",
      pb.target_gone(7, "T0", kill=lambda p, s: None, lstart=lambda p: ""), False)

print("\n  -- the property this whole feature exists for --")
check("kill(pid,0) succeeding is NOT enough: a RECYCLED pid still reads gone",
      pb.target_gone(7, "T0", kill=lambda p, s: None, lstart=lambda p: "T1-not-T0"), True)

print("\n  -- control: the suite must reject the obvious wrong implementation --")
check("...but an UNCHANGED start time does not (rejects always-gone-on-reuse-check)",
      pb.target_gone(7, "T0", kill=lambda p, s: None, lstart=lambda p: "T0"), False)

# --- run_forever(watch_pid=...) ---------------------------------------------

_dead = pathlib.Path(SB) / "state" / "workers" / "born-dead.alive"
_real_tg = pb.target_gone
pb.target_gone = lambda pid, start: True
try:
    _rc = pb.run_forever(_dead, 30.0, watch_pid=9999)
finally:
    pb.target_gone = _real_tg
check("a beat whose watched pid is already gone returns at once", _rc, 0)
check("...and never writes a fresh mtime for it", _dead.exists(), False)

# One refresh, then the loop returns from inside the poll rather than
# finishing the interval — same shape as the existing parent_pid case.
_polls = {"n": 0}
_touches = {"n": 0}
_real_touch = pb.touch
_real_sleep = pb.time.sleep


def _gone_after_three(_pid, _start):
    _polls["n"] += 1
    return _polls["n"] > 3


def _count_touch(path):
    _touches["n"] += 1
    _real_touch(path)


_lived = pathlib.Path(SB) / "state" / "workers" / "lived.alive"
pb.target_gone, pb.touch, pb.time.sleep = _gone_after_three, _count_touch, (lambda _s: None)
try:
    _rc = pb.run_forever(_lived, 0.02, watch_pid=9999, poll_s=0.01)
finally:
    pb.target_gone, pb.touch, pb.time.sleep = _real_tg, _real_touch, _real_sleep

check("the daemon returns 0 once its watched pid is gone", _rc, 0)
check("it refreshed once at start and once after a full interval", _touches["n"], 2)
check("it polled inside the interval, not once per 30 s", _polls["n"], 4)

# --- Poll liveness cheaply; check identity before each refresh. -------------


def _watch_timeline(*, death_at, starts):
    clock = {"now": 0}
    signals, reads, touches = [], [], []
    real_start = pb.process_start_time

    def kill(pid, sig):
        signals.append(clock["now"])
        if clock["now"] >= death_at:
            raise ProcessLookupError()

    def lstart(pid):
        reads.append(clock["now"])
        return starts.get(clock["now"], "T0")

    def sleep(seconds):
        clock["now"] += seconds
        if clock["now"] > 100:
            raise AssertionError("daemon did not stop")

    pb.target_gone = lambda pid, start: _real_tg(pid, start, kill=kill, lstart=lstart)
    pb.process_start_time = lstart
    pb.touch = lambda path: touches.append(clock["now"])
    pb.time.sleep = sleep
    try:
        rc = pb.run_forever("unused", 30, watch_pid=7, poll_s=1)
    finally:
        pb.target_gone, pb.process_start_time = _real_tg, real_start
        pb.touch, pb.time.sleep = _real_touch, _real_sleep
    return rc, signals, reads, touches


_rc, _signals, _reads, _writes = _watch_timeline(death_at=32, starts={})
check("signal liveness is checked every second through death", _signals, list(range(33)))
check("ps identity is read only initially and at refresh, not each poll", _reads, [0, 30])
check("death stops the writer before the next refresh", (_rc, _writes), (0, [0, 30]))

_rc, _signals, _reads, _writes = _watch_timeline(death_at=90, starts={30: "T1"})
check("PID reuse stops at the first refresh boundary", (_rc, _signals[-1]), (0, 30))
check("a recycled PID never receives that refresh", _writes, [0])

_rc, _signals, _reads, _writes = _watch_timeline(death_at=62, starts={30: ""})
check("an unreadable identity does not stop a living PID", _writes, [0, 30, 60])
check("identity checks resume after a transient ps failure", _reads, [0, 30, 60])
check("death is still detected promptly after ps recovers", (_rc, _signals[-1]), (0, 62))

_rc, _signals, _reads, _writes = _watch_timeline(death_at=90, starts={30: "", 60: "T1"})
check("transient ps failure preserves the baseline for later reuse detection",
      (_rc, _signals[-1], _writes), (0, 60, [0, 30]))

# --- CLI: --watch-pid reaches the daemon, and --parent-pid still works alone ---

_delegated = {}
_real_rf = pb.run_forever


def _spy(path, interval, **kw):
    _delegated.clear()
    _delegated["path"], _delegated["interval"] = path, interval
    _delegated.update(kw)
    return 0


pb.run_forever = _spy
try:
    pb.main(["--workspace", SB, "--kind", "worker", "--id", "w1", "--watch-pid", "5150"])
finally:
    pb.run_forever = _real_rf
check("--watch-pid reaches the daemon", _delegated.get("watch_pid"), 5150)
check("...alongside the existing parent_pid kwarg (default None)",
      _delegated.get("parent_pid"), None)

pb.run_forever = _spy
try:
    pb.main(["--workspace", SB, "--kind", "watcher", "--id", "w2", "--parent-pid", "4242"])
finally:
    pb.run_forever = _real_rf
check("with no --watch-pid the CLI does not pass that kwarg at all "
      "(the untouched --parent-pid path is byte-for-byte unchanged)",
      "watch_pid" in _delegated, False)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — pool_beat --watch-pid ({27} checks)")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
