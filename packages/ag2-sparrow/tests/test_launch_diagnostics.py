"""Launch diagnostics — the bridge must be diagnosable however it was started.

A supervised launch (SUTANDO_SUPERVISED=1, stdout persisted by the supervisor's
redirect) keeps _log stdout-only, byte-identical to the old behavior. A bare
launch (the 2026-07-25 tester wedge: bridge started outside startup.sh, 21h
stuck, zero logs to read) also tees every _log line to
<state-parent>/logs/gateway-bridge.log, and gateway-status.json carries
launched_via so a supervisor / health-check can flag unsupervised bridges.
"""
import importlib
import json
import os
import pathlib
import sys
import tempfile


def _load(state_dir, supervised):
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(state_dir)
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    if supervised:
        os.environ["SUTANDO_SUPERVISED"] = "1"
    else:
        os.environ.pop("SUTANDO_SUPERVISED", None)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


def test_bare_launch_tees_log_to_file():
    with tempfile.TemporaryDirectory() as d:
        state = pathlib.Path(d) / "state"
        state.mkdir()
        m = _load(state, supervised=False)
        assert m._LAUNCHED_VIA == "bare"
        m._log("hello from a bare launch")
        log = pathlib.Path(d) / "logs" / "gateway-bridge.log"
        assert log.exists(), "bare launch must create the in-bridge log file"
        body = log.read_text()
        assert "hello from a bare launch" in body
        assert "[remote-gateway-bridge]" in body
        print("PASS test_bare_launch_tees_log_to_file")


def test_supervised_launch_stays_stdout_only():
    with tempfile.TemporaryDirectory() as d:
        state = pathlib.Path(d) / "state"
        state.mkdir()
        m = _load(state, supervised=True)
        assert m._LAUNCHED_VIA == "supervised"
        m._log("hello from a supervised launch")
        assert not (pathlib.Path(d) / "logs").exists(), \
            "supervised launch must not duplicate stdout into a file"
        print("PASS test_supervised_launch_stays_stdout_only")


def test_status_carries_launched_via():
    with tempfile.TemporaryDirectory() as d:
        state = pathlib.Path(d) / "state"
        state.mkdir()
        m = _load(state, supervised=False)
        m._emit_gateway_status(True)
        rec = json.loads(m.GATEWAY_STATUS_FILE.read_text())
        assert rec["launched_via"] == "bare"
        m2 = _load(state, supervised=True)
        m2._emit_gateway_status(False, error="x", backoff_s=2)
        rec2 = json.loads(m2.GATEWAY_STATUS_FILE.read_text())
        assert rec2["launched_via"] == "supervised"
        # additive: existing consumers' keys are all still present
        for k in ("connected", "ts", "last_ok_ts", "backoff_s", "error",
                  "gateway", "schema_version"):
            assert k in rec2
        print("PASS test_status_carries_launched_via")


def test_log_rotates_past_cap():
    with tempfile.TemporaryDirectory() as d:
        state = pathlib.Path(d) / "state"
        state.mkdir()
        m = _load(state, supervised=False)
        m._LOG_DIR.mkdir(parents=True, exist_ok=True)
        m._LOG_FILE.write_text("x" * (m._LOG_MAX_BYTES + 1))
        m._log("post-rotation line")
        rotated = m._LOG_FILE.with_suffix(".log.1")
        assert rotated.exists(), "oversized log must rotate to .1"
        assert rotated.stat().st_size > m._LOG_MAX_BYTES
        body = m._LOG_FILE.read_text()
        assert "post-rotation line" in body
        assert m._LOG_FILE.stat().st_size < 1024
        print("PASS test_log_rotates_past_cap")


def test_log_write_failure_never_raises():
    with tempfile.TemporaryDirectory() as d:
        state = pathlib.Path(d) / "state"
        state.mkdir()
        m = _load(state, supervised=False)
        # unwritable log destination → _log must swallow, never raise
        m._LOG_DIR = pathlib.Path("/proc/nonexistent/dir")
        m._LOG_FILE = m._LOG_DIR / "gateway-bridge.log"
        m._log("this must not raise")
        print("PASS test_log_write_failure_never_raises")


if __name__ == "__main__":
    test_bare_launch_tees_log_to_file()
    test_supervised_launch_stays_stdout_only()
    test_status_carries_launched_via()
    test_log_rotates_past_cap()
    test_log_write_failure_never_raises()
