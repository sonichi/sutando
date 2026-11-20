#!/usr/bin/env python3
"""The owner-DM resolver follows the desktop's active lane (#5993): after a move from ag2.space to
s.space the s.space lane's state/owner-routing.s_dspace.json wins over the stopped lane's unsuffixed
file, which stays the fallback. Exit code: 0 on pass, 1 on fail."""

from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import owner_room_access as access  # noqa: E402

APP = Path(tempfile.mkdtemp())
WS = Path(tempfile.mkdtemp())
STATE = WS / "state"
STATE.mkdir()
OLD_DM, NEW_DM = "!old:ag2.space", "!new:s.space"
failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(f"{name} {detail}".rstrip())


def routing(name, dm):
    (STATE / name).write_text(json.dumps({"owner_dm": dm, "identity": "@a:x", "private": True}))


def select(lane):
    sel = APP / "channels" / "active-gateway-lane"
    sel.parent.mkdir(parents=True, exist_ok=True)
    sel.write_text(lane + "\n")


routing("owner-routing.json", OLD_DM)
routing("owner-routing.s_dspace.json", NEW_DM)

# No desktop selector (a shell-run job): the unsuffixed file, as before.
os.environ.pop("SUTANDO_APP_SUPPORT", None)
check("no selector keeps the unsuffixed file", access.owner_dm(access.owner_routing(WS)) == OLD_DM)

# The desktop signed in on s.space: the active lane's file, not the stopped lane's.
os.environ["SUTANDO_APP_SUPPORT"] = str(APP)
select("s_dspace")
check("active lane's routing wins", access.owner_routing_path(WS) == STATE / "owner-routing.s_dspace.json",
      str(access.owner_routing_path(WS)))
check("owner DM is the s.space DM", access.owner_dm(access.owner_routing(WS)) == NEW_DM,
      access.owner_dm(access.owner_routing(WS)))

# A selector meaning every lane, or a lane with no file yet, falls back to the unsuffixed file.
for lane in ("all", "primary", "../x"):
    select(lane)
    check(f"selector {lane!r} is no lane", access.owner_routing_path(WS) == STATE / "owner-routing.json")
select("s_dspace")
(STATE / "owner-routing.s_dspace.json").unlink()
check("missing lane file falls back", access.owner_dm(access.owner_routing(WS)) == OLD_DM)

if failures:
    print("FAIL:\n  " + "\n  ".join(failures))
    sys.exit(1)
print("PASS: owner routing follows the active lane")
