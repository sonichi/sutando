#!/usr/bin/env python3
"""The sidecar readers resolve the desktop's active lane to state/gateway-status.<lane>.json,
falling back to the primary connection's sidecar (state/gateway-status.json without a primary row).
A selector outside the bridge's instance bound is no lane and must never raise. Exit code: 0 on pass, 1 on fail."""

from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

APP = Path(tempfile.mkdtemp())
WS = Path(tempfile.mkdtemp())
(WS / "state").mkdir()
os.environ["SUTANDO_APP_SUPPORT"] = str(APP)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import gateway_serving as gs  # noqa: E402


def load(name, fname):
    spec = importlib.util.spec_from_file_location(name, REPO / "src" / fname)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


NOW = time.time()
SERVING = {"ts": NOW, "connected": True, "last_ok_ts": NOW - 3}
STATE = WS / "state"
failures = []


def select(lane):
    sel = APP / "channels" / "active-gateway-lane"
    sel.parent.mkdir(parents=True, exist_ok=True)
    if lane is None:
        sel.unlink(missing_ok=True)
    else:
        sel.write_text(lane + "\n")


def check(name, cond, detail=""):
    if not cond:
        failures.append(f"{name} {detail}".rstrip())


# --- only the s.space lane's file exists, and s_dspace is the active lane
(STATE / "gateway-status.s_dspace.json").write_text(json.dumps(SERVING))
select("s_dspace")
check("resolver picks the active lane's file", gs.status_path(STATE) == STATE / "gateway-status.s_dspace.json",
      str(gs.status_path(STATE)))

hc = load("hc_active_lane", "health-check.py")
hc.WORKSPACE_DIR = WS
check("health-check reads the active lane", hc._gateway_serving() is True, repr(hc._gateway_serving()))
check("health-check last_ok age comes from the lane file", hc._gateway_last_ok_age_h() is not None)

ciw = load("ciw_active_lane", "core-input-watch.py")
check("core-input-watch reads the active lane", ciw._gateway_status(str(STATE)) is True,
      repr(ciw._gateway_status(str(STATE))))

ss = load("ss_active_lane", "services_status.py")
ss.STATE_DIR = STATE
row = next(s for s in ss.service_registry() if s["id"] == "gateway")
check("services_status probes the active lane", row["probe"][1] == STATE / "gateway-status.s_dspace.json",
      str(row["probe"][1]))
state, _detail, _since = ss.probe_gateway(gs.status_path(STATE), "no-such-pattern", NOW, lambda *a, **k: None)
check("services_status reports the lane connected", state == "running", state)

# --- no desktop app-support dir (a shell-run job): no lane
os.environ["SUTANDO_APP_SUPPORT"] = ""
check("without SUTANDO_APP_SUPPORT there is no lane", gs.active_lane() is None, repr(gs.active_lane()))
os.environ["SUTANDO_APP_SUPPORT"] = str(APP)

# --- selector that names no usable lane: no lane, and the unsuffixed file is read
for sel in (None, "", "all", "primary", "../etc", "s.dspace", "s dspace", "a" * 33, "a" * 256):
    select(sel)
    check(f"selector {sel[:12] if sel else sel!r} is not a lane", gs.active_lane() is None, repr(gs.active_lane()))
    check(f"selector {sel[:12] if sel else sel!r} falls back to gateway-status.json",
          gs.status_path(STATE) == STATE / "gateway-status.json", str(gs.status_path(STATE)))

# --- legacy single file still works
(STATE / "gateway-status.s_dspace.json").unlink()
(STATE / "gateway-status.json").write_text(json.dumps(SERVING))
select(None)
check("legacy: health-check reads gateway-status.json", hc._gateway_serving() is True, repr(hc._gateway_serving()))
check("legacy: core-input-watch reads gateway-status.json", ciw._gateway_status(str(STATE)) is True)

# --- active lane named but its file missing: the unsuffixed file answers
select("s_dspace")
check("missing lane file falls back", gs.status_path(STATE) == STATE / "gateway-status.json", str(gs.status_path(STATE)))
check("missing lane file: health-check still reads the legacy file", hc._gateway_serving() is True)

# --- selector past the instance bound (33 and 256 chars): nothing raises, every reader reads the primary file
for n in (33, 256):
    select("a" * n)
    try:
        check(f"{n}-char selector: resolver falls back", gs.status_path(STATE) == STATE / "gateway-status.json")
        check(f"{n}-char selector: health-check reads the primary file", hc._gateway_serving() is True)
        check(f"{n}-char selector: core-input-watch reads the primary file", ciw._gateway_status(str(STATE)) is True)
        row = next(s for s in ss.service_registry() if s["id"] == "gateway")
        check(f"{n}-char selector: services_status probes the primary file",
              row["probe"][1] == STATE / "gateway-status.json", str(row["probe"][1]))
    except OSError as e:
        check(f"{n}-char selector raised", False, repr(e))

# --- the lane file's existence probe raises: the primary file answers, nothing propagates
(STATE / "gateway-status.s_dspace.json").write_text(json.dumps(SERVING))
select("s_dspace")
_real_exists = Path.exists


def _exists_raising_for_the_lane(self, *a, **k):
    if self.name == "gateway-status.s_dspace.json":
        raise OSError(63, "File name too long")
    return _real_exists(self, *a, **k)


Path.exists = _exists_raising_for_the_lane
try:
    check("exists() raises: resolver falls back to the primary file",
          gs.status_path(STATE) == STATE / "gateway-status.json", str(gs.status_path(STATE)))
    check("exists() raises: health-check reads the primary file", hc._gateway_serving() is True)
    check("exists() raises: core-input-watch reads the primary file", ciw._gateway_status(str(STATE)) is True)
except OSError as e:
    check("exists() raising propagated", False, repr(e))
finally:
    Path.exists = _real_exists
check("exists() restored: the lane file is read again", gs.status_path(STATE) == STATE / "gateway-status.s_dspace.json")

# --- stale primary beside a serving active lane: the lane answers, not the stale file
(STATE / "gateway-status.json").write_text(json.dumps({"ts": NOW - 17000, "connected": True, "last_ok_ts": NOW - 17000}))
(STATE / "gateway-status.s_dspace.json").write_text(json.dumps(SERVING))
select("s_dspace")
check("stale primary: health-check reads the serving lane", hc._gateway_serving() is True, repr(hc._gateway_serving()))
check("stale primary: core-input-watch reads the serving lane", ciw._gateway_status(str(STATE)) is True)

if failures:
    print("FAIL:\n  " + "\n  ".join(failures))
    sys.exit(1)
print("PASS: gateway-status readers resolve the active lane, legacy file still read")
