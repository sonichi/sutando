#!/usr/bin/env python3
"""An ARMED voice pin must veto the stuck-transport kickstart, not just the mtime branches.

`mark_stale_if_outdated` consults the pin on all three of its restart/rebuild
arms. The stuck-CONNECTING branch in `check_voice_transport` is a FOURTH
prescription reaching the same process by another route: it sets
`_stuck_connecting`, and main() turns that into `fix_launchd(voice-agent)`
unconditionally. A kickstart destroys the pinned witness exactly as a
stale-code restart would.

The bridge path already states this invariant (resolve the pin even when code
is not stale); this pins it for voice.

Run: python3 tests/health-check-pin-vetoes-voice-kickstart.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-voice-pin-")

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hc_voice_pin", REPO / "src/health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

PID, LSTART, SERVICE = "515151", "Mon Aug 25 09:00:00 2026", "voice-agent"


def _log() -> Path:
    d = Path(tempfile.mkdtemp(prefix="voicelog-"))
    f = d / "voice-agent.log"
    body = ["Sutando — Voice Interface", "[VoiceSession] Transport closed code=1006 abnormal"]
    body += ["[Health] state=CONNECTING"] * 25          # > 20 -> stuck
    f.write_text("\n".join(body) + "\n")
    return f


def _workspace(with_pin: bool) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="ws-voice-pin-"))
    (ws / "state").mkdir()
    pins = [{"service": SERVICE, "pid": PID, "lstart": LSTART,
             "reason": "branch-only voice witness",
             "expires_at": "2099-01-01T00:00:00Z"}] if with_pin else []
    (ws / "state" / "process-pins.json").write_text(json.dumps({"pins": pins}))
    return ws


def _run(with_pin: bool) -> dict:
    """Drive the PRODUCTION check; only the log path and process probe are fabricated."""
    lg = _log()
    o_ws, o_log, o_probe = hc.WORKSPACE_DIR, hc._voice_log_path, hc._proc_lstarts
    hc.WORKSPACE_DIR = _workspace(with_pin)
    hc._voice_log_path = lambda: lg
    hc._proc_lstarts = lambda _pat: ([time.time() - 3600], {PID: LSTART})
    try:
        return hc.check_voice_transport({"name": SERVICE, "status": "ok", "detail": ""})
    finally:
        hc.WORKSPACE_DIR, hc._voice_log_path, hc._proc_lstarts = o_ws, o_log, o_probe


# CONTROL: no pin -> the branch really fires and main() would kickstart.
unpinned = _run(with_pin=False)
assert unpinned.get("_stuck_connecting") is True, unpinned
assert "needs kickstart" in unpinned["detail"], unpinned

# ARMED: same inputs, pin present -> main()'s gate is withheld.
pinned = _run(with_pin=True)
assert pinned.get("_stuck_connecting") is not True, pinned
assert "DO NOT RESTART" in pinned["detail"], pinned
assert "needs kickstart" not in pinned["detail"], pinned

# The transport is still genuinely stuck; the pin changes the REMEDY, not the diagnosis.
assert pinned["status"] == "fail", pinned
assert "stuck CONNECTING" in pinned["detail"], pinned

print("PASS: armed pin withholds _stuck_connecting; unpinned control fires it; status stays fail")
