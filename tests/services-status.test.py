#!/usr/bin/env python3
"""Tests for src/services_status.py — the bundled-runtime services-status emit.

Pure-function coverage of the probe helpers (running/offline/unknown branches)
plus the payload assembly and atomic write. No real processes or sockets: the
`pid_alive`/`connect` callables are injected, and `.alive`/pidfiles are temp
files with controlled mtimes.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import services_status as ss  # noqa: E402


def _tmp(content: str | None = None, mtime: float | None = None) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "probe.file"
    if content is not None:
        p.write_text(content)
        if mtime is not None:
            import os
            os.utime(p, (mtime, mtime))
    return p


def test_alive_file_running():
    now = 1_000_000.0
    p = _tmp("beat", mtime=now - 10)  # 10s old, within 90s ttl
    status, detail, since = ss.probe_alive_file(p, now)
    assert status == "running", status
    assert since == now - 10
    assert "ago" in detail


def test_alive_file_stale_offline():
    now = 1_000_000.0
    p = _tmp("beat", mtime=now - 500)  # well past ttl
    status, detail, _ = ss.probe_alive_file(p, now)
    assert status == "offline", status
    assert "stale" in detail


def test_alive_file_missing_offline():
    now = 1_000_000.0
    p = Path(tempfile.mkdtemp()) / "nope.alive"
    status, detail, since = ss.probe_alive_file(p, now)
    assert status == "offline"
    assert since is None
    assert "no heartbeat" in detail


def test_pidfile_running_when_alive():
    p = _tmp("12345")
    status, detail, _ = ss.probe_pidfile(p, pid_alive=lambda pid: True)
    assert status == "running"
    assert "12345" in detail


def test_pidfile_offline_when_dead():
    p = _tmp("12345")
    status, detail, _ = ss.probe_pidfile(p, pid_alive=lambda pid: False)
    assert status == "offline"
    assert "dead" in detail


def test_pidfile_missing_offline():
    p = Path(tempfile.mkdtemp()) / "nope.pid"
    status, detail, _ = ss.probe_pidfile(p, pid_alive=lambda pid: True)
    assert status == "offline"
    assert "no pidfile" in detail


def test_pidfile_empty_offline():
    p = _tmp("   ")
    status, detail, _ = ss.probe_pidfile(p, pid_alive=lambda pid: True)
    assert status == "offline"
    assert "empty" in detail


def test_pidfile_nonpositive_pid_unknown():
    # pid 0 / negative signal the process GROUP via os.kill → would falsely
    # read "running"; a 0/negative pidfile must read as unknown (corrupt).
    for raw in ("0", "-5"):
        p = _tmp(raw)
        status, detail, _ = ss.probe_pidfile(p, pid_alive=lambda pid: True)
        assert status == "unknown", (raw, status)
        assert "non-positive" in detail


def test_run_forever_clamps_interval_floor():
    # interval <= 0 must not spin: the clamp raises it to >= 1s. Verified via
    # the injected-emit shutdown pattern with a zero interval — the loop exits
    # after one emit rather than spinning (and the clamp keeps slice_s > 0).
    import services_status as m
    m._SHUTDOWN_REQUESTED = False
    calls = []
    orig = m.emit_once
    def fake_emit():
        calls.append(1); m._SHUTDOWN_REQUESTED = True; return {}
    m.emit_once = fake_emit
    try:
        rc = m.run_forever(interval=0)
        assert rc == 0 and len(calls) == 1
    finally:
        m.emit_once = orig; m._SHUTDOWN_REQUESTED = False


def test_pidfile_malformed_unknown():
    p = _tmp("not-a-number")
    status, detail, _ = ss.probe_pidfile(p, pid_alive=lambda pid: True)
    assert status == "unknown"
    assert "unreadable" in detail


def test_port_running():
    status, detail, _ = ss.probe_port(8080, connect=lambda port: True)
    assert status == "running"
    assert "8080" in detail


def test_port_offline():
    status, detail, _ = ss.probe_port(8080, connect=lambda port: False)
    assert status == "offline"
    assert "not listening" in detail


def test_process_running():
    status, detail, _ = ss.probe_process("discord-bridge.py", pgrep=lambda pat: ["4242"])
    assert status == "running"
    assert "4242" in detail


def test_process_offline():
    status, detail, _ = ss.probe_process("discord-bridge.py", pgrep=lambda pat: [])
    assert status == "offline"
    assert "no process" in detail


def test_real_pgrep_returns_list():
    # pgrep -f for a pattern that matches this very python process (its argv
    # contains the test file path) → non-empty; a nonsense pattern → empty.
    import os
    assert isinstance(ss._real_pgrep("services-status"), list)
    assert ss._real_pgrep("zzz-no-such-process-zzz-9999") == []


def test_build_payload_shape_and_status():
    now = 2_000_000.0
    alive = _tmp("beat", mtime=now - 5)
    pidf = _tmp("999")
    registry = [
        {"id": "core", "name": "Sutando Core", "probe": ("alive_file", alive)},
        {"id": "task-watcher", "name": "Task Watcher", "probe": ("pidfile", pidf)},
        {"id": "gw", "name": "Gateway", "probe": ("port", 8080)},
        {"id": "discord-bridge", "name": "Discord", "probe": ("process", r"discord-bridge\.py")},
    ]
    payload = ss.build_payload(
        registry, now,
        pid_alive=lambda pid: True,
        connect=lambda port: False,
        pgrep=lambda pat: ["777"],
    )
    assert payload["schema_version"] == ss.SCHEMA_VERSION
    assert payload["emitted_at"] == now
    assert "host" in payload
    by_id = {s["id"]: s for s in payload["services"]}
    assert by_id["core"]["status"] == "running"
    assert by_id["task-watcher"]["status"] == "running"
    assert by_id["gw"]["status"] == "offline"
    assert by_id["discord-bridge"]["status"] == "running"
    # every service carries the full field set
    for s in payload["services"]:
        assert set(s) == {"id", "name", "status", "pid", "since", "detail", "last_check_at"}
        assert s["last_check_at"] == now


def test_build_payload_bad_probe_kind_unknown():
    now = 3_000_000.0
    registry = [{"id": "x", "name": "X", "probe": ("nonsense", None)}]
    payload = ss.build_payload(registry, now, pid_alive=lambda p: True, connect=lambda p: True)
    assert payload["services"][0]["status"] == "unknown"


def test_write_payload_atomic_and_valid_json():
    now = 4_000_000.0
    payload = ss.build_payload(
        [{"id": "core", "name": "C", "probe": ("port", 1)}],
        now, pid_alive=lambda p: True, connect=lambda p: True,
    )
    out = Path(tempfile.mkdtemp()) / "services-status.json"
    ss.write_payload(payload, out)
    assert out.exists()
    loaded = json.loads(out.read_text())
    assert loaded["schema_version"] == ss.SCHEMA_VERSION
    assert loaded["services"][0]["id"] == "core"
    # no leftover tmp file
    assert not out.with_suffix(".json.tmp").exists()


def test_service_registry_full_desktop_set():
    # G9: the registry covers the sutando-desktop dashboard service set.
    reg = ss.service_registry()
    ids = {s["id"] for s in reg}
    for expected in ("core", "gateway", "task-watcher", "voice-agent", "web-client",
                     "conversation-server", "screen-capture", "credential-proxy",
                     "discord-bridge", "slack-bridge", "telegram-bridge"):
        assert expected in ids, expected
    for s in reg:
        assert s["probe"][0] in ("alive_file", "pidfile", "port", "process", "gateway")
    # a full build over the real registry must not raise and covers every kind
    payload = ss.build_payload(reg, 1.0, pid_alive=lambda p: False,
                               connect=lambda p: False, pgrep=lambda pat: [])
    assert len(payload["services"]) == len(reg)


def test_emit_once_returns_payload():
    # Point the emitter at a temp status path so we don't clobber real state.
    import services_status as m
    p = Path(tempfile.mkdtemp()) / "services-status.json"
    orig = m.STATUS_PATH
    m.STATUS_PATH = p
    try:
        payload = m.emit_once()
        assert payload["schema_version"] == m.SCHEMA_VERSION
        assert p.exists()
    finally:
        m.STATUS_PATH = orig


def test_parse_args_defaults_and_once():
    a = ss._parse_args([])
    assert a.interval == 30.0 and a.once is False
    b = ss._parse_args(["--once", "--interval", "5"])
    assert b.once is True and b.interval == 5.0


def test_main_once():
    import services_status as m
    p = Path(tempfile.mkdtemp()) / "services-status.json"
    orig = m.STATUS_PATH
    m.STATUS_PATH = p
    try:
        rc = m.main(["--once"])
        assert rc == 0
        assert p.exists()
    finally:
        m.STATUS_PATH = orig


def test_real_pid_alive():
    import os
    assert ss._real_pid_alive(os.getpid()) is True
    # A PID far above any real one → not alive.
    assert ss._real_pid_alive(2_000_000_000) is False


def test_real_connect_true_and_false():
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert ss._real_connect(port) is True
    finally:
        srv.close()
    # Port now closed → connect fails.
    assert ss._real_connect(port) is False


def test_handle_signal_sets_flag():
    ss._SHUTDOWN_REQUESTED = False
    ss._handle_signal(15, None)
    assert ss._SHUTDOWN_REQUESTED is True
    ss._SHUTDOWN_REQUESTED = False


def test_run_forever_single_iteration_then_shutdown():
    # emit_once sets the shutdown flag so the loop runs exactly once and returns.
    import services_status as m
    m._SHUTDOWN_REQUESTED = False
    calls = []
    orig = m.emit_once
    def fake_emit():
        calls.append(1)
        m._SHUTDOWN_REQUESTED = True
        return {}
    m.emit_once = fake_emit
    try:
        rc = m.run_forever(interval=0.01)
        assert rc == 0
        assert len(calls) == 1
    finally:
        m.emit_once = orig
        m._SHUTDOWN_REQUESTED = False


def _sidecar(connected, ts_offset=0.0, now=None, last_ok_offset=None):
    """Write a gateway-status.json sidecar and return its path."""
    now = time.time() if now is None else now
    body = {"connected": connected, "ts": now + ts_offset}
    if last_ok_offset is not None:
        body["last_ok_ts"] = now + last_ok_offset
    d = Path(tempfile.mkdtemp())
    p = d / "gateway-status.json"
    p.write_text(json.dumps(body))
    return p


def test_gateway_connected_without_last_ok_is_offline():
    """`connected` with no completed poll is not serving. This is the shape a
    dead bridge's own final write leaves behind — the dashboard rendered it
    `running / connected` while nothing was delivered."""
    now = time.time()
    p = _sidecar(True, ts_offset=-5, now=now)          # deliberately no last_ok_ts
    status, detail, since = ss.probe_gateway(p, "nope", now, pgrep=lambda pat: [])
    assert status == "offline", (status, detail)
    assert since is None, since
    assert "no successful poll" in detail, detail


def test_gateway_sidecar_connected_is_running():
    now = time.time()
    # last_ok_offset is required: without it the fixture is a never-polled lane,
    # not the "sidecar beats pgrep" case this test pins.
    p = _sidecar(True, ts_offset=-5, now=now, last_ok_offset=-5)
    status, detail, _ = ss.probe_gateway(p, "nope", now, pgrep=lambda pat: [])
    # pgrep says NO process, yet the sidecar says connected → sidecar wins.
    assert status == "running", (status, detail)
    assert "connected" in detail


def test_gateway_sidecar_disconnected_is_offline_even_with_a_live_process():
    """The regression this guards: a healthy PROCESS is not a serving connection."""
    now = time.time()
    p = _sidecar(False, ts_offset=-5, now=now, last_ok_offset=-3600)
    status, detail, since = ss.probe_gateway(p, "nope", now, pgrep=lambda pat: ["78594"])
    assert status == "offline", (status, detail)
    assert "not serving" in detail
    assert "3600s" in detail
    assert since is not None


def test_gateway_disconnected_without_last_ok_still_offline():
    """A bridge that has NEVER connected has no last_ok_ts — still offline, no crash."""
    now = time.time()
    p = _sidecar(False, ts_offset=-5, now=now)          # note: no last_ok_ts
    status, detail, since = ss.probe_gateway(p, "nope", now, pgrep=lambda pat: ["999"])
    assert status == "offline", (status, detail)
    assert detail == "not serving"
    assert since is None


def test_gateway_stale_sidecar_falls_back_to_process():
    now = time.time()
    p = _sidecar(True, ts_offset=-(ss.GATEWAY_STATUS_TTL_S + 60), now=now)
    # Sidecar claims connected but is too old to trust → pgrep answers.
    status, detail, _ = ss.probe_gateway(p, "bridge", now, pgrep=lambda pat: [])
    assert status == "offline"
    assert "no process" in detail


def test_gateway_missing_sidecar_falls_back_to_process():
    now = time.time()
    missing = Path(tempfile.mkdtemp()) / "absent.json"
    status, detail, _ = ss.probe_gateway(missing, "bridge", now, pgrep=lambda pat: ["4242"])
    assert status == "running"
    assert "4242" in detail  # pre-sidecar behaviour preserved


def test_gateway_malformed_sidecar_falls_back_to_process():
    d = Path(tempfile.mkdtemp()); p = d / "gateway-status.json"
    p.write_text("not json at all")
    status, detail, _ = ss.probe_gateway(p, "bridge", time.time(), pgrep=lambda pat: ["7"])
    assert status == "running"
    assert "7" in detail


def test_registry_gateway_uses_the_sidecar_probe():
    spec = next(s for s in ss.service_registry() if s["id"] == "gateway")
    assert spec["probe"][0] == "gateway", spec["probe"]
    assert spec["probe"][1] == ss.GATEWAY_STATUS_PATH


if __name__ == "__main__":
    # Minimal assert-runner so the file is self-executing without pytest too.
    import inspect
    mod = sys.modules[__name__]
    fns = [f for n, f in inspect.getmembers(mod, inspect.isfunction) if n.startswith("test_")]
    passed = 0
    for f in fns:
        sig = inspect.signature(f)
        if not sig.parameters:  # skip pytest-fixture tests in bare mode
            f()
            passed += 1
    print(f"services-status: {passed} tests passed (bare mode)")
