"""A live poller keeps its workspace lock through a loop pass that outlasts
the stale window.

The main loop refreshed the lock once per pass, and one pass (35 s poll +
up to 60 s backoff + retries) can exceed the 90 s window, so a second launch
reaped a live holder and both polled one relay bearer. Measured across a real
process boundary with the window shrunk to STALE_S.

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


def fake_req(method, path, *a, **k):
    if path.startswith("/v1/tasks?") and not started.exists():
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


def _spawn(base):
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
           "PROBE_STARTED": str(started), "PROBE_GATE": str(gate)}
    env.pop("SUTANDO_BRIDGE_LOCK", None)
    child = subprocess.Popen([sys.executable, "-c", _CHILD % (str(PKG_ROOT), str(SRC_DIR), STALE_S)],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)
    deadline = time.time() + 20
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
        child, gate = _spawn(base)
        m2 = _load(base)
        try:
            # The pass is now blocked; wait well past the stale window.
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


def test_a_lost_lock_still_stops_the_poller():
    """The dual-poll guard is unchanged: once another process owns the lock,
    the holder logs the loss and leaves main() on its next pass."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        child, gate = _spawn(base)
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
    test_a_lost_lock_still_stops_the_poller()
    print("all ok")
