#!/usr/bin/env python3
"""
Tests for _emit_health_transition_events() in health-check.py.

Guards the four transition cases:
  a) fresh degraded  (no prior state, check is failing) → health.degraded
  b) new degraded    (was ok, now failing)               → health.degraded
  c) recovered       (was failing, now ok)               → health.recovered
  d) stable ok       (was ok, still ok)                  → no event
  e) stable failing  (was failing, still failing)        → no event
  f) state file updated after each run
  g) non-fatal on unwritable state dir

Run: python3 tests/health-check-event-log.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

PASS = 0
FAIL = 0


def ok(name: str):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


def fail(name: str, reason: str):
    global FAIL
    FAIL += 1
    print(f"  ✗ {name}: {reason}")


def _run(checks, prev_state: dict | None, tmp: Path) -> list[dict]:
    """Run _emit_health_transition_events and return captured log_event calls."""
    state_file = tmp / "state" / "health-last-status.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if prev_state is not None:
        state_file.write_text(json.dumps(prev_state))
    elif state_file.exists():
        state_file.unlink()

    logged: list[dict] = []

    def _fake_log(kind, **fields):
        logged.append({"kind": kind, **fields})

    old_ws = hc.WORKSPACE_DIR
    old_log = hc.log_event
    hc.WORKSPACE_DIR = tmp
    hc.log_event = _fake_log
    try:
        hc._emit_health_transition_events(checks)
    finally:
        hc.WORKSPACE_DIR = old_ws
        hc.log_event = old_log
    return logged


def mk(name, status):
    return {"name": name, "status": status, "detail": "test"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fresh_degraded():
    """No prior state, check is failing → health.degraded emitted."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = _run([mk("voice", "down")], prev_state=None, tmp=Path(tmp))
        degraded = [e for e in logs if e["kind"] == "health.degraded"]
        if degraded and degraded[0]["check"] == "voice":
            ok("fresh degraded emits health.degraded")
        else:
            fail("fresh degraded", f"expected health.degraded for voice, got: {logs}")


def test_was_ok_now_failing():
    """was ok, now failing → health.degraded."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = _run([mk("voice", "down")], prev_state={"voice": "ok"}, tmp=Path(tmp))
        degraded = [e for e in logs if e["kind"] == "health.degraded" and e.get("check") == "voice"]
        if degraded:
            ok("was-ok → failing emits health.degraded")
        else:
            fail("was-ok → failing", f"event missing: {logs}")


def test_was_failing_now_ok():
    """was failing, now ok → health.recovered."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = _run([mk("voice", "ok")], prev_state={"voice": "down"}, tmp=Path(tmp))
        recovered = [e for e in logs if e["kind"] == "health.recovered" and e.get("check") == "voice"]
        if recovered and recovered[0].get("prev_status") == "down":
            ok("was-failing → ok emits health.recovered with prev_status")
        else:
            fail("was-failing → ok", f"event missing or wrong fields: {logs}")


def test_stable_ok_no_event():
    """was ok, still ok → no event."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = _run([mk("voice", "ok")], prev_state={"voice": "ok"}, tmp=Path(tmp))
        if not logs:
            ok("stable-ok emits no event")
        else:
            fail("stable-ok", f"unexpected events: {logs}")


def test_stable_failing_no_event():
    """was failing, still failing → no event (avoid log spam on persistent failures)."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = _run([mk("voice", "down")], prev_state={"voice": "down"}, tmp=Path(tmp))
        if not logs:
            ok("stable-failing emits no event")
        else:
            fail("stable-failing", f"unexpected events: {logs}")


def test_warn_treated_as_ok():
    """warn is an ok-status — was warn, still warn → no event."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = _run([mk("loop", "warn")], prev_state={"loop": "warn"}, tmp=Path(tmp))
        if not logs:
            ok("stable-warn emits no event")
        else:
            fail("stable-warn", f"unexpected events: {logs}")


def test_state_file_updated():
    """State file should be written with current statuses after each run."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _run([mk("a", "ok"), mk("b", "down")], prev_state=None, tmp=tmp_path)
        state_file = tmp_path / "state" / "health-last-status.json"
        if not state_file.exists():
            fail("state file updated", "file not written")
            return
        saved = json.loads(state_file.read_text())
        if saved.get("a") == "ok" and saved.get("b") == "down":
            ok("state file written with current statuses")
        else:
            fail("state file updated", f"unexpected content: {saved}")


def test_non_fatal_on_bad_state_dir():
    """Unwritable state dir → no exception raised."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Point WORKSPACE_DIR to a path that can't have state/ created
        bad_path = tmp_path / "nonexistent" / "deeply" / "nested"
        # bad_path/state/ won't exist; mkdir(parents=True) in the function
        # should succeed. Override state_file directly by using a
        # read-only parent dir.
        import os
        ro_dir = tmp_path / "ro"
        ro_dir.mkdir()
        os.chmod(ro_dir, 0o555)
        try:
            logged: list = []
            old_ws = hc.WORKSPACE_DIR
            old_log = hc.log_event
            hc.WORKSPACE_DIR = ro_dir  # state/ inside a read-only dir
            hc.log_event = lambda kind, **f: logged.append(kind)
            try:
                hc._emit_health_transition_events([mk("x", "down")])
                ok("non-fatal on unwritable state dir")
            except Exception as e:
                fail("non-fatal on unwritable state dir", f"raised: {e}")
            finally:
                hc.WORKSPACE_DIR = old_ws
                hc.log_event = old_log
        finally:
            os.chmod(ro_dir, 0o755)


def test_multiple_checks_mixed():
    """Multiple checks with mixed transitions — correct events for each."""
    with tempfile.TemporaryDirectory() as tmp:
        checks = [mk("voice", "down"), mk("loop", "ok"), mk("ws", "ok")]
        prev = {"voice": "ok", "loop": "down", "ws": "ok"}
        logs = _run(checks, prev_state=prev, tmp=Path(tmp))
        kinds = {e["kind"] for e in logs}
        checks_in_logs = {e["check"] for e in logs}
        if "health.degraded" in kinds and "health.recovered" in kinds:
            degraded_names = {e["check"] for e in logs if e["kind"] == "health.degraded"}
            recovered_names = {e["check"] for e in logs if e["kind"] == "health.recovered"}
            if "voice" in degraded_names and "loop" in recovered_names and "ws" not in checks_in_logs:
                ok("multiple mixed transitions emit correct events")
            else:
                fail("multiple mixed", f"wrong assignments: {logs}")
        else:
            fail("multiple mixed", f"missing event kinds: {logs}")


if __name__ == "__main__":
    print("health-check event-log transition tests")
    print()
    test_fresh_degraded()
    test_was_ok_now_failing()
    test_was_failing_now_ok()
    test_stable_ok_no_event()
    test_stable_failing_no_event()
    test_warn_treated_as_ok()
    test_state_file_updated()
    test_non_fatal_on_bad_state_dir()
    test_multiple_checks_mixed()
    print()
    if FAIL == 0:
        print(f"━━━ health-check event-log tests: {PASS} passed / 0 failed ━━━")
    else:
        print(f"━━━ FAILED: {FAIL} failed / {PASS} passed ━━━")
        sys.exit(1)
