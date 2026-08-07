#!/usr/bin/env python3
"""Telegram 409 conflicts warn only until this host receives a later update, and
run_all_checks() must include telegram-bridge in the log-content gate.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)

failures = []


def check(label, cond, extra=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}  {extra}")


CONFLICT = ('API error 409: {"ok":false,"error_code":409,"description":"Conflict: '
            'terminated by other getUpdates request; make sure that only one bot '
            'instance is running"}')
RECEIPT = "  @chi: hello there"
STARTUP = "Telegram bridge started. Polling for messages..."

print("direct contract:")

_r = hc.bridge_log_content_status("telegram-bridge", "ok", [STARTUP] + [CONFLICT] * 20)
check("409 storm, no receipt after → warn", _r is not None and _r[0] == "warn", str(_r))
check("detail names the remedy (SKIP_TELEGRAM)", _r is not None and "SKIP_TELEGRAM=1" in _r[1], str(_r))

_r2 = hc.bridge_log_content_status("telegram-bridge", "ok", [CONFLICT] * 20 + [RECEIPT])
check("409s then a receipt → no override (host winning again)", _r2 is None, str(_r2))

_r3 = hc.bridge_log_content_status("telegram-bridge", "ok", [STARTUP, RECEIPT])
check("clean log → no override", _r3 is None, str(_r3))

_r4 = hc.bridge_log_content_status("telegram-bridge", "stale", [CONFLICT] * 20)
check("already stale → not downgraded to warn", _r4 is None, str(_r4))

_r5 = hc.bridge_log_content_status("telegram-bridge", "ok", [CONFLICT, RECEIPT, CONFLICT])
check("receipt BEFORE the last 409 does not clear it", _r5 is not None and _r5[0] == "warn", str(_r5))

# A discord log carrying a 409 must not pick up the telegram remedy.
_r6 = hc.bridge_log_content_status("discord-bridge", "ok", [CONFLICT] * 20)
check("branch is telegram-only (discord unaffected)", _r6 is None, str(_r6))

print("wiring through run_all_checks (guards the call-site gate):")

_orig_run = subprocess.run


def _fake_pgrep(cmd, *args, **kwargs):
    if isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == "/usr/bin/pgrep" and "telegram-bridge" in cmd[2]:
        class _R:
            returncode = 0
            stdout = "999999\n"
        return _R()
    return _orig_run(cmd, *args, **kwargs)


def _telegram_check(log_contents: str):
    with tempfile.TemporaryDirectory() as tmpws, tempfile.TemporaryDirectory() as tmphome:
        tmpws = Path(tmpws)
        (tmpws / "logs").mkdir(parents=True)
        (tmpws / "logs" / "telegram-bridge.log").write_text(log_contents)
        chan = Path(tmphome) / "channels" / "telegram"
        chan.mkdir(parents=True)
        (chan / ".env").write_text("TELEGRAM_BOT_TOKEN=test\n")

        _orig_chp = hc.claude_home_path

        def _fake_chp(*sub):
            if sub and sub[0] == "channels":
                return Path(tmphome).joinpath(*sub)
            return _orig_chp(*sub)

        for _k in ("SKIP_TELEGRAM", "SKIP_DISCORD", "SKIP_SLACK"):
            os.environ.pop(_k, None)

        with patch.object(hc, "WORKSPACE_DIR", tmpws), \
             patch.object(hc, "claude_home_path", side_effect=_fake_chp), \
             patch.object(subprocess, "run", side_effect=_fake_pgrep):
            checks = hc.run_all_checks()
        return next((c for c in checks if c["name"] == "telegram-bridge"), None)


_w1 = _telegram_check("\n".join([STARTUP] + [CONFLICT] * 20) + "\n")
check("run_all_checks: 409 storm → telegram-bridge warns", _w1 is not None and _w1["status"] == "warn", str(_w1))

_w2 = _telegram_check("\n".join([CONFLICT] * 20 + [RECEIPT]) + "\n")
check("run_all_checks: 409s then receipt → stays ok", _w2 is not None and _w2["status"] == "ok", str(_w2))

if failures:
    print(f"\nFAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("\nPASS — telegram poll-conflict detection")
