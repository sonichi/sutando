#!/usr/bin/env python3
"""Every restart/rebuild prescription must consult the pin, not just src-vs-process.

`mark_stale_if_outdated` had three arms that can prescribe an action, and only
one of them evaluated the pin. The compiled-artifact arm and the
binary-older-than-source arm both returned before pin evaluation, so an operator
following their prescription destroys the branch-only compiled witness the pin
exists to preserve — a rebuild destroys it exactly as a restart does.

Each arm carries a no-pin control: without it, an assertion that the arm is
suppressed would also pass on a build where the arm simply stopped firing.

Run: python3 tests/health-check-pin-covers-every-prescription.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-pin-cover-")

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hc_pin_cover", REPO / "src/health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

PID, LSTART, SERVICE = "424242", "Mon Aug 25 10:00:00 2026", "credential-proxy"
from datetime import datetime as _DT
PROC_START = _DT.strptime(LSTART, "%a %b %d %H:%M:%S %Y").timestamp()
NOW = PROC_START + 7200          # "now" is 2h after the pinned process started


def _workspace(with_pin: bool) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="ws-pin-cover-"))
    (ws / "state").mkdir()
    pins = [{"service": SERVICE, "pid": PID, "lstart": LSTART,
             "reason": "branch-only compiled witness",
             "expires_at": "2099-01-01T00:00:00Z"}] if with_pin else []
    (ws / "state" / "process-pins.json").write_text(json.dumps({"pins": pins}))
    return ws


def _run(*, with_pin: bool, src_mtime: float, bin_mtime: float) -> dict:
    """Drive the PRODUCTION function; only the process probe is fabricated."""
    d = Path(tempfile.mkdtemp(prefix="tree-pin-cover-"))
    src, binary = d / "svc.ts", d / "svc"
    src.write_text("x"), binary.write_text("y")
    os.utime(src, (src_mtime, src_mtime))
    os.utime(binary, (bin_mtime, bin_mtime))

    # Stub at subprocess.run: the seam present in every version, so this test
    # also drives the pre-fix source and fails there on the pin assertion.
    import subprocess as _sp
    from datetime import datetime as _dt

    class _R:
        def __init__(self, out): self.stdout = out

    def _fake_run(cmd, **_kw):
        if "/usr/bin/pgrep" in cmd[0]:
            return _R(f"{PID}\n")
        if "/bin/ps" in cmd[0]:
            return _R(f"{PID} {LSTART}\n")
        return _R("")

    orig_ws, orig_run, orig_cur, orig_filt = (
        hc.WORKSPACE_DIR, hc.subprocess.run, hc._binary_is_current,
        hc._filter_pids_this_checkout)
    hc.WORKSPACE_DIR = _workspace(with_pin)
    hc.subprocess.run = _fake_run
    hc._binary_is_current = lambda _b, _s: False   # force the mtime arm, not the git cross-check
    hc._filter_pids_this_checkout = lambda pids: pids
    try:
        check = {"name": SERVICE, "status": "ok", "detail": ""}
        hc.mark_stale_if_outdated(check, src, "svc-pattern",
                                  binary_path=binary, service=SERVICE)
        return check
    finally:
        (hc.WORKSPACE_DIR, hc.subprocess.run, hc._binary_is_current,
         hc._filter_pids_this_checkout) = orig_ws, orig_run, orig_cur, orig_filt


# --- ARM 1: compiled artifact rebuilt AFTER the process started -------------
# src older than binary so the binary-vs-source arm cannot fire first.
art = dict(src_mtime=NOW - 10800, bin_mtime=NOW - 1800)

fired = _run(with_pin=False, **art)
assert fired["status"] == "stale", fired          # CONTROL: the arm really fires
assert "artifact it executes was rebuilt" in fired["detail"], fired

pinned = _run(with_pin=True, **art)
assert pinned["status"] == "warn", pinned         # armed pin overrides the prescription
assert "DO NOT RESTART" in pinned["detail"], pinned
assert "restart needed" not in pinned["detail"], pinned

# --- ARM 2: binary OLDER than source ("rebuild needed") ---------------------
# This arm returns before the src-vs-process comparison entirely.
old = dict(src_mtime=NOW, bin_mtime=NOW - 7200)

fired = _run(with_pin=False, **old)
assert fired["status"] == "stale", fired          # CONTROL
assert "older than source" in fired["detail"], fired

pinned = _run(with_pin=True, **old)
assert pinned["status"] == "warn", pinned
assert "DO NOT RESTART" in pinned["detail"], pinned
assert "rebuild needed" not in pinned["detail"], pinned

# --- ARM 3: src newer than process (regression — was already correct) -------
srcarm = dict(src_mtime=NOW, bin_mtime=NOW)
pinned = _run(with_pin=True, **srcarm)
assert pinned["status"] == "warn", pinned
assert "DO NOT RESTART" in pinned["detail"], pinned

print("PASS: all three prescriptions consult the pin; each arm's no-pin control fired")


# --- _proc_lstarts fail-open paths ------------------------------------------

# Both must yield ([], {}) so a probe failure cannot hide a stale deploy and
# the binary-vs-source arm still runs regardless of process start.
import subprocess as _sp


def _probe_with(run_impl):
    orig_run, orig_filt = hc.subprocess.run, hc._filter_pids_this_checkout
    hc.subprocess.run = run_impl
    hc._filter_pids_this_checkout = lambda pids: pids
    try:
        return hc._proc_lstarts("svc-pattern")
    finally:
        hc.subprocess.run, hc._filter_pids_this_checkout = orig_run, orig_filt


class _Out:
    def __init__(self, s): self.stdout = s


# pgrep matches nothing -> no pids, no lstarts.
assert _probe_with(lambda cmd, **kw: _Out("\n")) == ([], {})

# The probe itself fails -> ([], None): unknown is NOT the empty set, so a
# failed enumeration can never fabricate ORPHAN or authorize a restart.
def _boom(cmd, **_kw):
    raise _sp.TimeoutExpired(cmd, 5)


assert _probe_with(_boom) == ([], None)

# CONTROL: a working probe must still return data, or the two asserts above
# would pass on a helper that always returns empty.
good = _probe_with(lambda cmd, **kw: _Out(f"{PID}\n" if "pgrep" in cmd[0] else f"{PID} {LSTART}\n"))
assert good[1] == {PID: LSTART}, good

print("PASS: _proc_lstarts: no-match is ([], {}), probe error is ([], None); positive control returns data")
