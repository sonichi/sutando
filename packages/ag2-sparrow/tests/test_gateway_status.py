"""_emit_gateway_status — connection-liveness sidecar (state/gateway-status.json).
Covers the shape a local supervisor reads and the last_ok_ts preservation that
lets it show "last connected N s ago" while reconnecting.
"""
import json, os, tempfile, importlib, sys, pathlib

def _load(tmp):
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(tmp)
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)

def test_connected_then_reconnecting_preserves_last_ok():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        m = _load(tmp)
        f = m.GATEWAY_STATUS_FILE
        # connected
        m._emit_gateway_status(True)
        rec = json.loads(f.read_text())
        assert rec["connected"] is True
        assert isinstance(rec["last_ok_ts"], int) and rec["last_ok_ts"] > 0
        assert rec["schema_version"] == 1 and rec["error"] is None
        ok_ts = rec["last_ok_ts"]
        # reconnecting — last_ok_ts must be PRESERVED, connected flips false
        m._emit_gateway_status(False, error="network: timed out", backoff_s=4)
        rec2 = json.loads(f.read_text())
        assert rec2["connected"] is False
        assert rec2["last_ok_ts"] == ok_ts, "last_ok_ts must survive a reconnecting write"
        assert rec2["backoff_s"] == 4
        assert "network" in rec2["error"]
        # no temp file left behind (atomic replace)
        assert not (f.with_suffix(".json.tmp")).exists()
        print("PASS test_connected_then_reconnecting_preserves_last_ok")

def test_status_write_never_raises(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        # point at an unwritable path → helper must swallow, not raise
        m.GATEWAY_STATUS_FILE = pathlib.Path("/proc/nonexistent/dir/gw.json")
        m._emit_gateway_status(True)  # should not raise
        print("PASS test_status_write_never_raises")

if __name__ == "__main__":
    test_connected_then_reconnecting_preserves_last_ok()
    test_status_write_never_raises()
    print("ALL PASS")
