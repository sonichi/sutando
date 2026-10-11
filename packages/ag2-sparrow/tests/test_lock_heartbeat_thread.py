"""A live poller keeps its workspace lock through a loop pass that outlasts
the stale window; a hung poller still loses it.

The main loop refreshed the lock once per pass, and one pass (35 s poll +
up to 60 s backoff + retries) can exceed the 90 s window, so a second launch
reaped a live holder and both polled one relay bearer. The off-loop refresh
stops once a pass outlasts the pass bound, so a loop that hangs goes stale as
before. Measured across a real process boundary with the window shrunk to
STALE_S and the bound set per case.

Run: python3 packages/ag2-sparrow/tests/test_lock_heartbeat_thread.py
"""
import importlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = PKG_ROOT.parents[1] / "src"
STALE_S = 3

_CHILD = '''
import os, pathlib, sys, time
sys.path.insert(0, %r)
# As the src/ loader runs it: the host label must not change mid-run.
sys.path.insert(0, %r)
from ag2_sparrow import remote_gateway_bridge as m
started = pathlib.Path(os.environ["PROBE_STARTED"])
gate = pathlib.Path(os.environ["PROBE_GATE"])
m._LOCK_STALE_S = %d
m._LOCK_PASS_MAX_S = float(os.environ["PROBE_PASS_MAX"])

warmup = float(os.environ.get("PROBE_WARMUP", "0"))
first_poll = None


def fake_req(method, path, *a, **k):
    global first_poll
    if path.startswith("/v1/tasks?") and not started.exists():
        # Quick passes first, so the long pass starts late in the run.
        first_poll = first_poll or time.monotonic()
        if time.monotonic() - first_poll < warmup:
            time.sleep(0.2)
            return {}
        started.write_text(str(os.getpid()))
        while not gate.exists():
            time.sleep(0.02)
    return {}


m._req = fake_req
m.main()
'''


def _load(base):
    os.environ["AGENT_CONNECT_TASK_DIR"] = str(base / "tasks")
    os.environ["AGENT_CONNECT_RESULT_DIR"] = str(base / "results")
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(PKG_ROOT))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


def _spawn(base, pass_max, warmup=0):
    for sub in ("tasks", "results", "state"):
        (base / sub).mkdir()
    started, gate = base / "started", base / "gate"
    env = {**os.environ,
           "AGENT_CONNECT_TASK_DIR": str(base / "tasks"),
           "AGENT_CONNECT_RESULT_DIR": str(base / "results"),
           "AGENT_CONNECT_STATE_DIR": str(base / "state"),
           "REMOTE_TASK_URL": "https://gw.example/relay",
           "REMOTE_TASK_TOKEN": "dummy-secret",
           "SUTANDO_SUPERVISED": "1",
           "PROBE_STARTED": str(started), "PROBE_GATE": str(gate),
           "PROBE_PASS_MAX": str(pass_max), "PROBE_WARMUP": str(warmup)}
    env.pop("SUTANDO_BRIDGE_LOCK", None)
    child = subprocess.Popen([sys.executable, "-c", _CHILD % (str(PKG_ROOT), str(SRC_DIR), STALE_S)],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)
    deadline = time.time() + 20 + warmup
    while not started.exists() and child.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert started.exists(), f"child never reached the task poll (rc={child.poll()})"
    return child, gate


def _stop(child, gate):
    gate.write_text("open")
    if child.poll() is None:
        child.terminate()
    try:
        return child.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        child.kill()
        return child.communicate(timeout=5)[0]


def test_a_long_loop_pass_does_not_let_a_successor_reap_the_holder():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        # The pass outlasts the stale window but stays under the pass bound.
        child, gate = _spawn(base, pass_max=STALE_S * 4)
        m2 = _load(base)
        try:
            time.sleep(STALE_S * 2)
            assert child.poll() is None, "child exited while its pass was blocked"
            r = m2._ws_acquire(m2._LOCK_ROLE, m2._LOCK_WS, stale_seconds=STALE_S)
            assert r.status == "deferred", (
                f"successor got {r.status!r}: the live holder pid={child.pid} was reaped "
                f"during a loop pass longer than the {STALE_S}s stale window")
            assert (r.holder or {}).get("pid") == child.pid, r.holder
        finally:
            _stop(child, gate)
            m2._ws_release(m2._LOCK_ROLE, m2._LOCK_WS)
        print("PASS test_a_long_loop_pass_does_not_let_a_successor_reap_the_holder")


def test_a_hung_loop_pass_lets_a_successor_reap_the_holder():
    """Past the pass bound the thread stops refreshing: the lock goes stale,
    a successor reaps it, and the holder exits on the loss once it wakes."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        child, gate = _spawn(base, pass_max=STALE_S)
        m2 = _load(base)
        try:
            # Bound, then stale window, then margin for the 1 s refresh cadence.
            time.sleep(STALE_S * 4)
            assert child.poll() is None, "child exited while its pass was blocked"
            r = m2._ws_acquire(m2._LOCK_ROLE, m2._LOCK_WS, stale_seconds=STALE_S)
            assert r.status == "reaped", (
                f"successor got {r.status!r}: the hung holder pid={child.pid} kept "
                f"refreshing past the {STALE_S}s pass bound")
            gate.write_text("open")
            child.wait(15)
            out = child.stdout.read()
            assert child.returncode == 0, (child.returncode, out)
            assert "singleton: main loop has made no progress" in out, out
            assert "singleton: lost workspace poller lock" in out, out
            lock = m2._LOCK_WS / "state" / "locks" / f"{m2._LOCK_ROLE}.lock"
            assert json.loads(lock.read_text())["pid"] == os.getpid(), \
                "the woken holder overwrote the successor's lock"
        finally:
            _stop(child, gate)
            m2._ws_release(m2._LOCK_ROLE, m2._LOCK_WS)
        print("PASS test_a_hung_loop_pass_lets_a_successor_reap_the_holder")


def test_a_late_long_pass_is_covered_by_the_loop_stamp():
    """The loop stamps progress on every pass. Without that stamp the only
    tick is the one written at thread start, so a long pass that begins after
    uptime has passed the pass bound reads as a stall and the lock goes stale."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        # Quick passes for 3x the bound, then one pass under the bound but
        # over the stale window.
        child, gate = _spawn(base, pass_max=STALE_S * 2, warmup=STALE_S * 3)
        m2 = _load(base)
        out = None
        try:
            time.sleep(STALE_S + 1.5)
            assert child.poll() is None, "child exited while its pass was blocked"
            r = m2._ws_acquire(m2._LOCK_ROLE, m2._LOCK_WS, stale_seconds=STALE_S)
            assert r.status == "deferred", (
                f"successor got {r.status!r}: the holder pid={child.pid} was reaped "
                f"during a bounded pass that began after uptime passed the "
                f"{STALE_S * 2}s pass bound")
            assert (r.holder or {}).get("pid") == child.pid, r.holder
            out = _stop(child, gate)
            assert "singleton: main loop has made no progress" not in out, out
        finally:
            if out is None:
                _stop(child, gate)
            m2._ws_release(m2._LOCK_ROLE, m2._LOCK_WS)
        print("PASS test_a_late_long_pass_is_covered_by_the_loop_stamp")


def test_a_lost_lock_still_stops_the_poller():
    """The dual-poll guard is unchanged: once another process owns the lock,
    the holder logs the loss and leaves main() on its next pass."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        child, gate = _spawn(base, pass_max=STALE_S * 4)
        m2 = _load(base)
        lock = m2._LOCK_WS / "state" / "locks" / f"{m2._LOCK_ROLE}.lock"
        try:
            holder = json.loads(lock.read_text())
            holder["pid"] = os.getpid()
            lock.write_text(json.dumps(holder))
            time.sleep(STALE_S)
            gate.write_text("open")
            child.wait(15)
            out = child.stdout.read()
            assert child.returncode == 0, (child.returncode, out)
            assert "singleton: lost workspace poller lock" in out, out
            assert json.loads(lock.read_text())["pid"] == os.getpid(), \
                "the old holder overwrote the new owner's lock"
        finally:
            _stop(child, gate)
            m2._ws_release(m2._LOCK_ROLE, m2._LOCK_WS)
        print("PASS test_a_lost_lock_still_stops_the_poller")


if __name__ == "__main__":
    test_a_long_loop_pass_does_not_let_a_successor_reap_the_holder()
    test_a_hung_loop_pass_lets_a_successor_reap_the_holder()
    test_a_late_long_pass_is_covered_by_the_loop_stamp()
    test_a_lost_lock_still_stops_the_poller()
    print("all ok")
