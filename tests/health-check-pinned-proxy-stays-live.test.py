#!/usr/bin/env python3
"""A pinned credential proxy is still routing, so quota checks must not go blind.

`mark_stale_if_outdated` turns the proxy's status to `warn` when a pin is armed.
Both quota consumers gate on `proxy_status not in ("ok", "stale")`, so the pinned
proxy read as down and BOTH checks silently self-suppressed — budgeting blind for
as long as the witness was preserved.

Controls run both ways: a genuinely down proxy must still suppress them, or this
test would pass on a build that had simply stopped gating.

Run: python3 tests/health-check-pinned-proxy-stays-live.test.py
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-proxy-live-")

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hc_proxy_live", REPO / "src/health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


# The PRODUCTION mapping, not a copy of it: a re-implementation here would stay
# green after run_all_checks stopped using it.
_passed = hc.proxy_liveness_status


# A pin turns status to warn while the port is still listening.
pinned = {"name": "credential-proxy", "status": "warn", "live": True,
          "detail": "DO NOT RESTART credential-proxy pid 1 — witness in flight"}
assert _passed(pinned) == "stale", _passed(pinned)
assert hc.check_quota_telemetry(_passed(pinned))["detail"] != \
    "credential proxy not running — quota telemetry not expected"
assert hc.check_quota_account_identity(_passed(pinned)).get("detail") != \
    "credential proxy not up — nothing to compare"

# CONTROL: genuinely down (never listened) must still suppress both.
down = {"name": "credential-proxy", "status": "warn", "live": False,
        "detail": "not running (optional)"}
assert _passed(down) == "warn", _passed(down)
assert hc.check_quota_telemetry(_passed(down))["detail"] == \
    "credential proxy not running — quota telemetry not expected"
assert hc.check_quota_account_identity(_passed(down))["detail"] == \
    "credential proxy not up — nothing to compare"

# A healthy proxy is unchanged by the mapping.
assert _passed({"status": "ok", "live": True}) == "ok"
assert _passed({"status": "stale", "live": True}) == "stale"

print("PASS — a pinned proxy stays live to both quota checks; "
      "a down proxy still suppresses them")
