#!/usr/bin/env python3
"""Per-sender access_tier resolution in the ag2-sparrow gateway.

The gateway writes access_tier as a LOCAL decision (never trusting the task's
self-claim). This test covers the owner-controlled per-sender tierMap layered on
top of LOCAL_TIER: a named teammate is down-tiered by user_id, everyone else
keeps LOCAL_TIER, and a malformed/missing map never re-tiers (fail-soft).
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Load the EXACT module this PR modifies — packages/ag2-sparrow/ag2_sparrow/
# remote_gateway_bridge.py — as a proper package import so its relative imports
# resolve. (An earlier version loaded src/remote-gateway-bridge.py, a shim that
# EXECS this file; that shim contains none of these functions textually, so the
# indirection made it non-obvious the test exercised the new code — review catch.
# Importing the packages module directly removes all doubt.)
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
import ag2_sparrow.remote_gateway_bridge as rgb  # noqa: E402

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

# --- clamp: the map can only DOWN-tier, never escalate above LOCAL_TIER ---
# 12. on a LOCAL_TIER=team node, a map entry of "owner" is clamped to team, while
# a "other" entry still down-tiers. Guards against a misconfigured/compromised
# access.json escalating a sender above the node's own default.
_prev_local = rgb.LOCAL_TIER
rgb.LOCAL_TIER = "team"
_write_map({"@rick:ag2.space": "owner", "@stranger:ag2.space": "other"})
check("map cannot escalate above LOCAL_TIER (owner clamped to team)", rgb._tier_for("@rick:ag2.space") == "team")
check("map still down-tiers below LOCAL_TIER (team node, 'other' honored)", rgb._tier_for("@stranger:ag2.space") == "other")
rgb.LOCAL_TIER = _prev_local

# --- fail-safe: a transient read error preserves the last-known-good map ---
# 13. once a teammate is down-tiered, a malformed/mid-write access.json must NOT
# silently fail-open them back to LOCAL_TIER (owner). Preserve last-known-good.
_write_map({"@rick:ag2.space": "team"})
assert rgb._tier_for("@rick:ag2.space") == "team"  # prime the cache with a good read
ACCESS.write_text("{ corrupt not json ]")           # file now unparseable
rgb._TIER_MAP_CACHE["mtime"] = -1                    # force a re-read attempt (hits except)
check("malformed re-read keeps the down-tier (fail-safe, not fail-open to owner)",
      rgb._tier_for("@rick:ag2.space") == "team")

_total = 13
print(f"\nResults: {_total - len(failures)}/{_total} passed" if not failures else f"\nResults: FAILED {failures}")
sys.exit(1 if failures else 0)
