#!/usr/bin/env python3
"""An armed pin must survive every LATER bridge diagnostic, not just the stale one.

The pin is applied in the stale-code branch, but the dead-log-inode check and the
log-content override both run afterwards and used to REPLACE status+detail with an
explicit restart command — telling an operator to perform the exact hand-restart
that destroys the pinned witness.

Controls run both ways: with the pin removed the same inputs must still prescribe
the restart, or this test passes by construction and certifies nothing.

Run: python3 tests/health-check-pin-veto-survives-later-checks.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

# Bound at module level BEFORE the import: health-check resolves the channel
# allowlist under CLAUDE_CONFIG_DIR, falling back to the real home when unset.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-pin-veto-")
_ccd = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_ccd.mkdir(parents=True, exist_ok=True)
(_ccd / "access.json").write_text("{}")

REPO = Path(__file__).resolve().parents[1]
PID = "424242"
LSTART = "Mon Aug 25 00:00:00 2026"
DEAD_LOG = "/tmp/pin-veto-control/slack-bridge.log"


def _load():
    spec = importlib.util.spec_from_file_location("hc_pin_veto", REPO / "src/health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_run(real):
    def run(cmd, *a, **kw):
        argv = cmd if isinstance(cmd, list) else [cmd]
        joined = " ".join(str(x) for x in argv)
        if "pgrep" in joined and "slack-bridge" in joined:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{PID}\n", stderr="")
        if "/bin/ps" in joined and "lstart=" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=LSTART + "\n", stderr="")
        if "lsof" in joined:
            return subprocess.CompletedProcess(
                argv, 0, stderr="",
                stdout=f"slack-bri {PID} u 1w REG 1,2 0 9 {DEAD_LOG}\n")
        return real(cmd, *a, **kw)
    return run


def _bridge_detail(pinned: bool) -> tuple:
    with tempfile.TemporaryDirectory() as td:
        ws, repo = Path(td) / "ws", Path(td) / "repo"
        (ws / "state").mkdir(parents=True)
        (ws / "logs").mkdir(parents=True)
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "slack-bridge.py").write_text("# bridge\n")
        pins = {"pins": [{"service": "slack-bridge", "pid": PID, "lstart": LSTART,
                          "reason": "branch-only witness in flight",
                          "expires_at": "2099-01-01T00:00:00Z"}]} if pinned else {"pins": []}
        (ws / "state" / "process-pins.json").write_text(json.dumps(pins))

        mod = _load()
        mod.WORKSPACE_DIR = ws
        mod.REPO_DIR = repo
        real = subprocess.run
        with mock.patch.object(mod.subprocess, "run", _fake_run(real)):
            checks = mod.run_all_checks()
        row = next((c for c in checks if c.get("name") == "slack-bridge"), None)
        assert row is not None, "slack-bridge produced no check row"
        return row["status"], row["detail"]


# CONTROL FIRST: unpinned, the dead inode must still prescribe the restart.
status, detail = _bridge_detail(pinned=False)
assert "log inode dead" in detail, f"control lost the finding: {detail}"
assert "kickstart" in detail, f"control lost the restart prescription: {detail}"
assert "DO NOT RESTART" not in detail, f"control should carry no veto: {detail}"

# PINNED: the finding survives, the remedy does not.
status, detail = _bridge_detail(pinned=True)
assert status == "warn", f"pinned bridge should still warn, got {status}"
assert "DO NOT RESTART" in detail, f"pin veto was retracted: {detail}"
assert "log inode dead" in detail, f"dead-inode finding was dropped: {detail}"
assert "kickstart" not in detail, (
    f"pinned bridge still prescribes the restart the pin forbids: {detail}")

print("PASS — an armed pin survives the dead-log-inode check; "
      "the finding stays visible and the restart prescription is dropped")
