"""A SIGTERM/SIGINT exit names its signal in the bridge log.

The handler keeps the clean exit 0 a supervisor already expects; before it
logged nothing, so a signal exit read the same as any other status-0 exit.
Measured on a real child process holding the poller lock.

Run: python3 packages/ag2-sparrow/tests/test_signal_exit_log.py
"""
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]

_CHILD = '''
import sys, time
sys.path.insert(0, %r)
from ag2_sparrow import remote_gateway_bridge as m
assert m._acquire_singleton() is True, "child could not acquire the singleton"
print("READY", flush=True)
while True:
    time.sleep(0.05)
'''


def _run_and_signal(sig):
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        env = {**os.environ,
               "AGENT_CONNECT_TASK_DIR": str(base / "tasks"),
               "AGENT_CONNECT_RESULT_DIR": str(base / "results"),
               "AGENT_CONNECT_STATE_DIR": str(base / "state"),
               "REMOTE_TASK_URL": "https://gw.example/relay",
               "REMOTE_TASK_TOKEN": "dummy-secret"}
        env.pop("SUTANDO_SUPERVISED", None)
        child = subprocess.Popen([sys.executable, "-c", _CHILD % str(PKG_ROOT)], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 15
            line = ""
            while time.time() < deadline:
                line = child.stdout.readline()
                if not line or line.strip() == "READY":
                    break
            assert line.strip() == "READY", f"child never became ready (rc={child.poll()})"
            child.send_signal(sig)
            out, err = child.communicate(timeout=15)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(5)
        log_file = base / "logs" / "gateway-bridge.log"
        log_text = log_file.read_text() if log_file.exists() else ""
        return child.returncode, out, err, log_text


def _check(sig):
    rc, out, err, log_text = _run_and_signal(sig)
    want = f"received {sig.name} — exiting"
    assert rc == 0, f"{sig.name}: exit code changed to {rc}; stderr={err!r}"
    assert want in out, f"{sig.name}: stdout lacks {want!r}: {out!r}"
    assert want in log_text, f"{sig.name}: log file lacks {want!r}: {log_text!r}"
    print(f"PASS test_{sig.name.lower()}_exit_is_logged_and_still_exits_0")


def test_sigterm_exit_is_logged():
    _check(signal.SIGTERM)


def test_sigint_exit_is_logged():
    _check(signal.SIGINT)


if __name__ == "__main__":
    test_sigterm_exit_is_logged()
    test_sigint_exit_is_logged()
    print("all ok")
