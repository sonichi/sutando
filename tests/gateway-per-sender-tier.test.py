#!/usr/bin/env python3
"""Per-sender access_tier resolution in the ag2-sparrow gateway.

The gateway writes access_tier as a LOCAL decision (never trusting the task's
self-claim). This test covers the owner-controlled per-sender tierMap layered on
top of LOCAL_TIER: a named teammate is down-tiered by user_id, everyone else
keeps LOCAL_TIER, and a malformed/missing map never re-tiers (fail-soft).
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


rgb = _load("remote_gateway_bridge", REPO / "src" / "remote-gateway-bridge.py")

failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="rgb-tier-test-"))
ACCESS = tmp / "access.json"
# point the resolver at our temp file, and make sure LOCAL_TIER is a known value
rgb._ag2space_access_path = lambda: str(ACCESS)
rgb.LOCAL_TIER = "owner"


def _write_map(m):
    ACCESS.write_text(json.dumps({"allowFrom": ["@qingyun:ag2.space"], "tierMap": m}))
    rgb._TIER_MAP_CACHE["mtime"] = None  # force re-read (mtime granularity is coarse)


# 1. named teammate is down-tiered by user_id
_write_map({"@rick:ag2.space": "team"})
check("mapped sender → team", rgb._tier_for("@rick:ag2.space") == "team")

# 2. unlisted sender keeps LOCAL_TIER (owner) — no escalation, no accidental downgrade
check("unlisted sender → LOCAL_TIER", rgb._tier_for("@qingyun:ag2.space") == "owner")

# 3. empty / missing user_id → LOCAL_TIER
check("empty user_id → LOCAL_TIER", rgb._tier_for("") == "owner")
check("None user_id → LOCAL_TIER", rgb._tier_for(None) == "owner")

# 4. invalid tier value in the map is ignored → LOCAL_TIER (never a bogus tier)
_write_map({"@rick:ag2.space": "boss"})
check("invalid tier value ignored → LOCAL_TIER", rgb._tier_for("@rick:ag2.space") == "owner")

# 5. 'other' tier is honored
_write_map({"@stranger:ag2.space": "other"})
check("'other' tier honored", rgb._tier_for("@stranger:ag2.space") == "other")

# 6. missing file → LOCAL_TIER (fail-soft, no crash)
ACCESS.unlink()
rgb._TIER_MAP_CACHE["mtime"] = None
check("missing access.json → LOCAL_TIER", rgb._tier_for("@rick:ag2.space") == "owner")

# 7. malformed JSON → LOCAL_TIER (fail-soft)
ACCESS.write_text("{ not json ]")
rgb._TIER_MAP_CACHE["mtime"] = None
check("malformed access.json → LOCAL_TIER", rgb._tier_for("@rick:ag2.space") == "owner")

# 8. live update: adding a teammate is picked up without reload (mtime cache)
_write_map({"@rick:ag2.space": "team", "@sam:ag2.space": "team"})
check("live-added teammate → team", rgb._tier_for("@sam:ag2.space") == "team")

# --- owner-activity gate (same tier map must gate presence, not just task tier) ---
# Regression for the blocking finding on a3e24dd: _write_owner_activity() gated on
# the blanket LOCAL_TIER, so a down-tiered teammate still overwrote
# state/last-owner-activity.json and poisoned owner-presence routing. It must gate
# on the SENDER's resolved tier, exactly like the task access_tier write.
OWNER_ACT = tmp / "last-owner-activity.json"
rgb.OWNER_ACTIVITY_FILE = OWNER_ACT
_write_map({"@rick:ag2.space": "team"})

# 9. owner sender → owner-activity IS written
OWNER_ACT.unlink(missing_ok=True)
rgb._write_owner_activity({"task": "hi", "source": "ag2space", "user_id": "@qingyun:ag2.space", "channel_id": "!r:hs"})
check("owner sender writes owner-activity", OWNER_ACT.exists())

# 10. team-tier teammate → owner-activity NOT written (the fix)
OWNER_ACT.unlink(missing_ok=True)
rgb._write_owner_activity({"task": "hi", "source": "ag2space", "user_id": "@rick:ag2.space", "channel_id": "!r:hs"})
check("team teammate does NOT write owner-activity", not OWNER_ACT.exists())

# 11. team teammate cannot OVERWRITE a prior genuine owner-activity record
rgb._write_owner_activity({"task": "owner here", "source": "ag2space", "user_id": "@qingyun:ag2.space", "channel_id": "!r:hs"})
before = OWNER_ACT.read_text()
rgb._write_owner_activity({"task": "rick here", "source": "ag2space", "user_id": "@rick:ag2.space", "channel_id": "!r:hs"})
check("teammate does not clobber owner's activity record", OWNER_ACT.read_text() == before)

_total = 11
print(f"\nResults: {_total - len(failures)}/{_total} passed" if not failures else f"\nResults: FAILED {failures}")
sys.exit(1 if failures else 0)
