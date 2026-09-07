#!/usr/bin/env python3
"""Telegram 409 conflicts warn only until this host receives a later update, and
run_all_checks() must include telegram-bridge in the log-content gate.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional
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

# Verbatim producer strings — the gate discriminates on them. A producer REWORD is caught
# by the run_all_checks cases below, which write a real heartbeat and assert the real output.
HB_STALE  = "running but heartbeat stale (1906s old)"
LOG_STALE = "running but log stale (600s old)"

print("direct contract:")

_r = hc.bridge_log_content_status("telegram-bridge", "ok", [STARTUP] + [CONFLICT] * 20)
check("409 storm, no receipt after → warn", _r is not None and _r[0] == "warn", str(_r))
check("detail names the remedy (SKIP_TELEGRAM)", _r is not None and "SKIP_TELEGRAM=1" in _r[1], str(_r))

_r2 = hc.bridge_log_content_status("telegram-bridge", "ok", [CONFLICT] * 20 + [RECEIPT])
check("409s then a receipt → no override (host winning again)", _r2 is None, str(_r2))

_r3 = hc.bridge_log_content_status("telegram-bridge", "ok", [STARTUP, RECEIPT])
check("clean log → no override", _r3 is None, str(_r3))

# A stale heartbeat must NOT suppress the 409: the heartbeat only advances on an
# accepted poll, so the conflict is its cause.
_rh = hc.bridge_log_content_status("telegram-bridge", "warn", [STARTUP] + [CONFLICT] * 20, HB_STALE)
check("stale heartbeat does NOT hide the 409",
      _rh is not None and _rh[0] == "warn" and "competing" in _rh[1], str(_rh))
check("and it names the causal direction",
      _rh is not None and "CONSEQUENCE" in _rh[1], str(_rh))

# The tested no-downgrade case stays intact: "stale" means stale CODE, a different
# axis, and the 409 branch must still keep its hands off it.
_r4 = hc.bridge_log_content_status("telegram-bridge", "stale", [CONFLICT] * 20)
check("already stale → not downgraded to warn", _r4 is None, str(_r4))

_r5 = hc.bridge_log_content_status("telegram-bridge", "ok", [CONFLICT, RECEIPT, CONFLICT])
check("receipt BEFORE the last 409 does not clear it", _r5 is not None and _r5[0] == "warn", str(_r5))

# A discord log carrying a 409 must not pick up the telegram remedy.
_r6 = hc.bridge_log_content_status("discord-bridge", "ok", [CONFLICT] * 20)
check("branch is telegram-only (discord unaffected)", _r6 is None, str(_r6))

# Accepting every `warn` would let a merely-OLD log ending in historical 409s advise
# SKIP_TELEGRAM=1, which can disable the only working bridge. These two must survive.
_r7 = hc.bridge_log_content_status("telegram-bridge", "warn", [CONFLICT] * 20, LOG_STALE)
check("a stale-LOG warn is NOT replaced by the conflict diagnosis", _r7 is None, str(_r7))

_r8 = hc.bridge_log_content_status("telegram-bridge", "warn", [CONFLICT] * 20,
                                   "running but log inode replaced — bridge writing to a deleted file")
check("a dead-inode warn is NOT replaced either", _r8 is None, str(_r8))

# The freshness rule still applies under the wider gate.
_r9 = hc.bridge_log_content_status("telegram-bridge", "warn", [CONFLICT] * 20 + [RECEIPT], HB_STALE)
check("receipt after the last 409 still clears it", _r9 is None, str(_r9))

# Widening must not have loosened the code-stale pin; re-assert WITH a detail present.
_r10 = hc.bridge_log_content_status("telegram-bridge", "stale", [CONFLICT] * 20,
                                    "running but code is 900 min newer than process — restart needed")
check("code-stale is still never downgraded", _r10 is None, str(_r10))

# Back-compat: 3-arg callers (tests/health-check-bridge-log-content.test.py) still work.
_r11 = hc.bridge_log_content_status("telegram-bridge", "ok", [CONFLICT] * 20)
check("3-arg call still works (detail defaults)", _r11 is not None and _r11[0] == "warn", str(_r11))

print("wiring through run_all_checks (guards the call-site gate):")

def _telegram_check(log_contents: str, heartbeat_age_s: Optional[int] = None):
    with tempfile.TemporaryDirectory() as tmpws, tempfile.TemporaryDirectory() as tmphome:
        tmpws = Path(tmpws)
        (tmpws / "logs").mkdir(parents=True)
        (tmpws / "logs" / "telegram-bridge.log").write_text(log_contents)
        if heartbeat_age_s is not None:
            # A real stale heartbeat on disk, not a stubbed status — the call-site
            # gate only fires on the genuine one.
            (tmpws / "state").mkdir(parents=True, exist_ok=True)
            hb = tmpws / "state" / "telegram-bridge.heartbeat"
            stamp = int(time.time()) - heartbeat_age_s
            hb.write_text(str(stamp))
            os.utime(hb, (stamp, stamp))
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
             patch.object(hc, "probe_pids", return_value=(["999999"], True)):
            checks = hc.run_all_checks()
        return next((c for c in checks if c["name"] == "telegram-bridge"), None)


_w1 = _telegram_check("\n".join([STARTUP] + [CONFLICT] * 20) + "\n")
check("run_all_checks: 409 storm → telegram-bridge warns", _w1 is not None and _w1["status"] == "warn", str(_w1))

_w2 = _telegram_check("\n".join([CONFLICT] * 20 + [RECEIPT]) + "\n")
check("run_all_checks: 409s then receipt → stays ok", _w2 is not None and _w2["status"] == "ok", str(_w2))

# The call-site gate: dropping `detail` from the bridge_log_content_status(...) call
# leaves every direct case above green, because those pass detail themselves.
_w3 = _telegram_check("\n".join([STARTUP] + [CONFLICT] * 20) + "\n", heartbeat_age_s=1906)
check("run_all_checks: 409 storm + REAL stale heartbeat → conflict diagnosis",
      _w3 is not None and _w3["status"] == "warn" and "SKIP_TELEGRAM=1" in _w3["detail"], str(_w3))
check("...and the vaguer symptom no longer wins",
      _w3 is not None and "heartbeat stale" not in _w3["detail"], str(_w3))

# Control in the other direction: a change that ALWAYS returned the conflict detail
# would pass the case above. Stale heartbeat + clean log must still report the heartbeat.
_w4 = _telegram_check("\n".join([STARTUP, RECEIPT]) + "\n", heartbeat_age_s=1906)
check("stale heartbeat + clean log → reports the heartbeat, not a conflict",
      _w4 is not None and "heartbeat stale" in _w4["detail"], str(_w4))

if failures:
    print(f"\nFAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("\nPASS — telegram poll-conflict detection")
