"""_emit_gateway_status — connection-liveness sidecar (state/gateway-status.json).
Covers the shape a local supervisor reads and the last_ok_ts preservation that
lets it show "last connected N s ago" while reconnecting.
"""
import json
import os
import tempfile
import importlib
import sys
import pathlib
import time
from unittest.mock import patch

sys.path.append(str(pathlib.Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

def _load(tmp):
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(tmp)
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.reload(importlib.import_module("ag2_sparrow.remote_gateway_bridge"))
    mod.gateway_instance.binding_key_path = lambda: pathlib.Path(tmp, "binding.key")  # never the real one
    return mod

def test_connected_then_reconnecting_preserves_last_ok():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        m = _load(tmp)
        f = m.GATEWAY_STATUS_FILE
        # connected
        m._emit_gateway_status(True, poll_ok_ts=1000)
        rec = json.loads(f.read_text())
        assert rec["connected"] is True
        assert rec["last_ok_ts"] == 1000
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
        assert not list(tmp.glob("gateway-status.json.*.tmp"))
        print("PASS test_connected_then_reconnecting_preserves_last_ok")

def test_status_names_its_writer_and_its_sub_second_time():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        before = time.time()
        m._emit_gateway_status(True)
        rec = json.loads(m.GATEWAY_STATUS_FILE.read_text())
        assert rec["pid"] == os.getpid()
        assert rec["start"] == m.agent_identity.own_start() and rec["start"]
        assert rec["proc_start"] == m._ws_proc_start(os.getpid()) and rec["proc_start"]
        assert isinstance(rec["written_at"], float) and before <= rec["written_at"] <= time.time()
        assert rec["ts"] == int(rec["written_at"])
        print("PASS test_status_names_its_writer_and_its_sub_second_time")

def test_connected_without_a_poll_preserves_absent_evidence():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        for connected in (False, True, False):
            m._emit_gateway_status(connected)
            rec = json.loads(m.GATEWAY_STATUS_FILE.read_text())
            assert rec["last_ok_ts"] is None
            assert rec["connected"] is connected
        print("PASS test_connected_without_a_poll_preserves_absent_evidence")

def test_only_explicit_poll_evidence_advances_last_ok():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        with patch.object(m.time, "time", return_value=1000):
            m._emit_gateway_status(True, poll_ok_ts=900.75)
        for connected in (True, False, True):
            with patch.object(m.time, "time", return_value=1100):
                m._emit_gateway_status(connected)
            rec = json.loads(m.GATEWAY_STATUS_FILE.read_text())
            assert rec["ts"] == 1100
            assert rec["last_ok_ts"] == 900
        with patch.object(m.time, "time", return_value=1200):
            m._emit_gateway_status(False, error="task processing failed", poll_ok_ts=1150.5)
        rec = json.loads(m.GATEWAY_STATUS_FILE.read_text())
        assert rec["ts"] == 1200 and rec["last_ok_ts"] == 1150
        assert rec["connected"] is False
        print("PASS test_only_explicit_poll_evidence_advances_last_ok")

def test_status_is_atomically_replaced():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        m._emit_gateway_status(False)
        replace = m.os.replace
        observed = []

        def inspect_replace(src, dst):
            assert json.loads(dst.read_text())["last_ok_ts"] is None
            assert json.loads(src.read_text())["last_ok_ts"] == 1000
            observed.append(dst)
            replace(src, dst)

        with patch.object(m.os, "replace", side_effect=inspect_replace):
            m._emit_gateway_status(True, poll_ok_ts=1000)
        assert observed == [m.GATEWAY_STATUS_FILE]
        assert json.loads(m.GATEWAY_STATUS_FILE.read_text())["last_ok_ts"] == 1000
        print("PASS test_status_is_atomically_replaced")

def test_status_write_never_raises(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        # point at an unwritable path → helper must swallow, not raise
        blocked = pathlib.Path(d) / "not-a-directory"
        blocked.write_text("blocked")
        m.GATEWAY_STATUS_FILE = blocked / "gw.json"
        m._emit_gateway_status(True, poll_ok_ts=1000)  # should not raise
        print("PASS test_status_write_never_raises")

def test_redact_url_strips_userinfo_and_query():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        # userinfo + secret query + fragment all dropped; scheme/host/path kept
        assert m._redact_url("https://user:pass@gw.example:8443/relay?token=SEKRIT#frag") \
            == "https://gw.example:8443/relay"
        # clean URL is unchanged
        assert m._redact_url("https://gw.example/relay") == "https://gw.example/relay"
        # a string that is not a URL fails closed to a placeholder (never raises)
        assert m._redact_url("not-a-url?token=SEKRIT") == m.gateway_instance.REDACTED_URL
        print("PASS test_redact_url_strips_userinfo_and_query")

def test_emitted_gateway_field_carries_no_secret():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        # a gateway configured with userinfo + ?token= must NOT persist to state/
        os.environ["REMOTE_TASK_URL"] = "https://u:p@gw.example/relay?token=SEKRIT"
        m = _load(tmp)
        m._emit_gateway_status(True)
        rec = json.loads(m.GATEWAY_STATUS_FILE.read_text())
        assert rec["gateway"] == "https://gw.example/relay"
        assert "SEKRIT" not in rec["gateway"] and "u:p@" not in rec["gateway"]
        os.environ.pop("REMOTE_TASK_URL", None)
        print("PASS test_emitted_gateway_field_carries_no_secret")

class _Stop(Exception):
    pass


def _main_stubs(m, req):
    """Stubs for every main() step but the poll and the status write; _drive
    runs main() to its first backoff sleep and returns the last status."""
    noop = lambda *a, **k: None  # noqa: E731
    stubs = dict(
        _acquire_singleton=lambda: True, _heartbeat_singleton=lambda: True,
        _abort_if_poll_stalled=lambda *a: False, _advance_held_tasks=lambda *a: False,
        _keep_relay_results_in_flight=lambda *a: False, _load_inflight=set,
        _reconcile_abandoned=lambda *a: set(), _receive_round=lambda tasks: (tasks, {}),
        _req=req, _ATTEMPTS=None)
    for name in ("_resolve_host_label", "_start_lock_heartbeat", "_stand_down_if_asked",
                 "_settle_proactive_room", "_lane_identity", "_recover_orphan_proactive",
                 "_close_stale_turns", "_retire_delivered_marks", "_sweep_held_dir",
                 "_reclaim_parked_relay_results", "refresh_routing", "_maybe_start_event_channel",
                 "_start_results_watcher", "_start_outbound_worker", "_stamp_loop_progress",
                 "_post_heartbeat", "_retry_withheld_reviews", "_push_pool_advertisement",
                 "_settle_round", "_reconcile_orphan_results", "_reconcile_ledger",
                 "_retry_turn_closes", "_log"):
        stubs.setdefault(name, noop)
    return stubs


def _drive(m, stubs):
    def stop(*_a):
        raise _Stop()
    with patch.multiple(m, **stubs), patch.object(m.atexit, "register"), \
            patch.object(m.time, "sleep", side_effect=stop):
        try:
            m.main()
        except _Stop:
            pass
    return json.loads(m.GATEWAY_STATUS_FILE.read_text())


def test_a_post_poll_failure_keeps_connected_true():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        stubs = _main_stubs(m, lambda method, path, *a, **k: {"tasks": []})

        def ledger_fails(_inflight):
            raise RuntimeError("ledger write failed")
        stubs["_reconcile_ledger"] = ledger_fails
        rec = _drive(m, stubs)
        assert rec["connected"] is True, rec
        assert rec["error"].startswith("unexpected: RuntimeError"), rec
        assert rec["last_ok_ts"] is not None, rec
        print("PASS test_a_post_poll_failure_keeps_connected_true")


def test_a_failed_poll_still_reports_reconnecting():
    import urllib.error
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))

        def poll_503(method, path, *a, **k):
            raise urllib.error.HTTPError(path, 503, "unavailable", {}, None)
        rec = _drive(m, _main_stubs(m, poll_503))
        assert rec["connected"] is False, rec
        assert rec["error"] == "HTTP 503" and rec["backoff_s"] > 0, rec
        print("PASS test_a_failed_poll_still_reports_reconnecting")


if __name__ == "__main__":
    test_connected_then_reconnecting_preserves_last_ok()
    test_connected_without_a_poll_preserves_absent_evidence()
    test_status_names_its_writer_and_its_sub_second_time()
    test_only_explicit_poll_evidence_advances_last_ok()
    test_status_is_atomically_replaced()
    test_status_write_never_raises()
    test_redact_url_strips_userinfo_and_query()
    test_emitted_gateway_field_carries_no_secret()
    test_a_post_poll_failure_keeps_connected_true()
    test_a_failed_poll_still_reports_reconnecting()
    print("ALL PASS")
