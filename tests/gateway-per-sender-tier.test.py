#!/usr/bin/env python3
"""Broker-attested access_tier resolution in the ag2-sparrow gateway.

The broker tier and owner-controlled local tierMap are independent upper bounds.
The effective tier is the lower one: a local map may downgrade an authenticated
sender, but can never promote a broker-attested team or guest task.
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
    rgb._TIER_MAP_CACHE["ident"] = None  # force re-read (cache keys on mtime_ns+size+inode)


# 1. named teammate is down-tiered by user_id
_write_map({"@rick:ag2.space": "team"})
check("mapped sender → team", rgb._tier_for("@rick:ag2.space", "owner") == "team")

# 2. unlisted sender keeps LOCAL_TIER (owner) — no escalation, no accidental downgrade
check("unlisted sender → LOCAL_TIER", rgb._tier_for("@qingyun:ag2.space", "owner") == "owner")

# 3. empty / missing user_id → LOCAL_TIER
check("empty user_id → LOCAL_TIER", rgb._tier_for("", "owner") == "owner")
check("None user_id → LOCAL_TIER", rgb._tier_for(None, "owner") == "owner")

# 4. invalid tier value in the map is ignored → LOCAL_TIER (never a bogus tier)
_write_map({"@rick:ag2.space": "boss"})
check("invalid tier value ignored → LOCAL_TIER", rgb._tier_for("@rick:ag2.space", "owner") == "owner")

# 5. legacy 'other' tier is normalized to guest
_write_map({"@stranger:ag2.space": "other"})
check("'other' tier normalized to guest", rgb._tier_for("@stranger:ag2.space", "owner") == "guest")

# 6. missing file → LOCAL_TIER (fail-soft, no crash)
ACCESS.unlink()
rgb._TIER_MAP_CACHE["ident"] = None
check("missing access.json → LOCAL_TIER", rgb._tier_for("@rick:ag2.space", "owner") == "owner")

# 7. malformed JSON → LOCAL_TIER (fail-soft)
ACCESS.write_text("{ not json ]")
rgb._TIER_MAP_CACHE["ident"] = None
check("malformed access.json → LOCAL_TIER", rgb._tier_for("@rick:ag2.space", "owner") == "owner")

# 8. live update: adding a teammate is picked up without reload (mtime cache)
_write_map({"@rick:ag2.space": "team", "@sam:ag2.space": "team"})
check("live-added teammate → team", rgb._tier_for("@sam:ag2.space", "owner") == "team")

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
rgb._write_owner_activity({"task": "hi", "source": "ag2space", "user_id": "@qingyun:ag2.space", "access_tier": "owner", "channel_id": "!r:hs"})
check("owner sender writes owner-activity", OWNER_ACT.exists())

# 10. team-tier teammate → owner-activity NOT written (the fix)
OWNER_ACT.unlink(missing_ok=True)
rgb._write_owner_activity({"task": "hi", "source": "ag2space", "user_id": "@rick:ag2.space", "access_tier": "owner", "channel_id": "!r:hs"})
check("team teammate does NOT write owner-activity", not OWNER_ACT.exists())

# 11. team teammate cannot OVERWRITE a prior genuine owner-activity record
rgb._write_owner_activity({"task": "owner here", "source": "ag2space", "user_id": "@qingyun:ag2.space", "access_tier": "owner", "channel_id": "!r:hs"})
before = OWNER_ACT.read_text()
rgb._write_owner_activity({"task": "rick here", "source": "ag2space", "user_id": "@rick:ag2.space", "access_tier": "owner", "channel_id": "!r:hs"})
check("teammate does not clobber owner's activity record", OWNER_ACT.read_text() == before)

# --- per-sender owner tier on a least-privilege node (the shared-gateway shape) ---
# 12. An explicitly listed sender may exceed the node default only when the
# authenticated broker independently attests that same higher tier.
_prev_local = rgb.LOCAL_TIER
rgb.LOCAL_TIER = "team"
_write_map({"@dana:ag2.space": "owner", "@stranger:ag2.space": "other"})
check("listed sender IS up-tiered above LOCAL_TIER (team node, explicit owner)",
      rgb._tier_for("@dana:ag2.space", "owner") == "owner")
check("local owner map cannot promote a broker guest",
      rgb._tier_for("@dana:ag2.space", "guest") == "guest")
check("map still down-tiers below LOCAL_TIER (team node, 'other' is guest)",
      rgb._tier_for("@stranger:ag2.space", "owner") == "guest")
# 12b. the escalation is EXPLICIT-ONLY: an unlisted sender on the same node keeps
# the least-privilege default. This is the property that makes the shared-gateway
# config default-CLOSED, and it is what a blanket owner default cannot give you.
check("unlisted sender on a team node stays team (no blanket escalation)",
      rgb._tier_for("@nobody:ag2.space", "owner") == "team")
# 12c. an invalid mapped value still cannot escalate — it is ignored, not honored.
_write_map({"@rick:ag2.space": "admin"})
check("invalid mapped value on a team node does NOT escalate",
      rgb._tier_for("@rick:ag2.space", "owner") == "team")
rgb.LOCAL_TIER = _prev_local

# 12d. no silent demotion: on an owner-default node an unlisted sender is STILL
# owner, so existing single-owner installs are untouched by this change.
_write_map({"@rick:ag2.space": "team"})
check("owner-default node: unlisted sender still owner (no regression)",
      rgb._tier_for("@unknown:ag2.space", "owner") == "owner")
check("owner-default node: missing broker tier fails closed",
      rgb._tier_for("@unknown:ag2.space") == "guest")

# --- fail-safe: a transient read error preserves the last-known-good map ---
# 13. once a teammate is down-tiered, a malformed/mid-write access.json must NOT
# silently fail-open them back to LOCAL_TIER (owner). Preserve last-known-good.
_write_map({"@rick:ag2.space": "team"})
assert rgb._tier_for("@rick:ag2.space", "owner") == "team"  # prime the cache with a good read
ACCESS.write_text("{ corrupt not json ]")           # file now unparseable
rgb._TIER_MAP_CACHE["ident"] = None                    # force a re-read attempt (hits except)
check("malformed re-read keeps the down-tier (fail-safe, not fail-open to owner)",
      rgb._tier_for("@rick:ag2.space", "owner") == "team")

# --- stale cache must fail CLOSED in BOTH directions (revocation safety) ---
# Once the map can grant a tier ABOVE LOCAL_TIER, replaying a STALE cache verbatim
# would keep an escalation alive on a file the owner just deleted to revoke it.
# Pre-change this could not happen (the clamp made an above-LOCAL_TIER entry
# unreachable), so the hazard is introduced by the up-tier and fixed here.
_prev_local = rgb.LOCAL_TIER
rgb.LOCAL_TIER = "team"

# 18. revocation-by-deletion actually revokes an ESCALATED sender
_write_map({"@dana:ag2.space": "owner"})
assert rgb._tier_for("@dana:ag2.space", "owner") == "owner"   # prime cache with the grant
ACCESS.unlink()
rgb._TIER_MAP_CACHE["ident"] = None                     # force re-read → OSError path
check("deleting access.json REVOKES an escalated sender (no stale owner)",
      rgb._tier_for("@dana:ag2.space", "owner") == "team")

# 19. ...while a DOWN-tier still survives the same stale read (original fail-safe intact)
_write_map({"@rick:ag2.space": "other"})
assert rgb._tier_for("@rick:ag2.space", "owner") == "guest"
ACCESS.unlink()
rgb._TIER_MAP_CACHE["ident"] = None
check("stale cache still preserves a DOWN-tier (fail-safe not broken)",
      rgb._tier_for("@rick:ag2.space", "owner") == "guest")

# 20. malformed (not deleted) also drops the escalation but keeps the down-tier
_write_map({"@dana:ag2.space": "owner", "@rick:ag2.space": "other"})
assert rgb._tier_for("@dana:ag2.space", "owner") == "owner"
ACCESS.write_text("{ corrupt ]")
rgb._TIER_MAP_CACHE["ident"] = None
check("malformed read drops the escalation", rgb._tier_for("@dana:ag2.space", "owner") == "team")
check("malformed read keeps the down-tier", rgb._tier_for("@rick:ag2.space", "owner") == "guest")

# 21. HOT PATH REGRESSION GUARD: a present, unchanged file is a VALID cache, not a
# stale one — a legitimate up-tier must survive repeated reads untouched.
_write_map({"@dana:ag2.space": "owner"})
check("valid unchanged cache keeps the up-tier (hot path not projected)",
      rgb._tier_for("@dana:ag2.space", "owner") == "owner" and rgb._tier_for("@dana:ag2.space", "owner") == "owner")
rgb.LOCAL_TIER = _prev_local

# --- the OTHER _tier_for consumer: owner-presence gate, under UP-tiering ---
# _write_owner_activity gates on the SENDER's resolved tier. Its docstring reasons
# only about DOWN-tiering ("a down-tiered teammate must not overwrite..."), because
# until now an above-LOCAL_TIER entry was unreachable. The up-tier creates a case it
# never contemplated, and presence drives the proactive loop's "owner active" signal
# plus the core-supervisor escalation target — a silent break here is expensive and
# hard to trace, so pin both directions.
_prev_local = rgb.LOCAL_TIER
rgb.LOCAL_TIER = "team"
_write_map({"@dana:ag2.space": "owner"})

# 23. an UP-tiered sender on a team node DOES register owner presence
OWNER_ACT.unlink(missing_ok=True)
rgb._write_owner_activity({"task": "hi", "source": "ag2space",
                           "user_id": "@dana:ag2.space", "access_tier": "owner", "channel_id": "!r:hs"})
check("up-tiered owner registers owner-presence on a team node", OWNER_ACT.exists())

# 24. ...and an unlisted sender on that same node still does NOT
OWNER_ACT.unlink(missing_ok=True)
rgb._write_owner_activity({"task": "hi", "source": "ag2space",
                           "user_id": "@nobody:ag2.space", "access_tier": "owner", "channel_id": "!r:hs"})
check("unlisted sender does NOT register owner-presence on a team node",
      not OWNER_ACT.exists())
rgb.LOCAL_TIER = _prev_local

# --- same-mtime revocation (john-the-dev [P1] on #2584, reproduced then fixed) ---
# The cache used to key on float os.path.getmtime(). A rewrite landing in the same
# second — or one whose mtime is restored via os.utime — compared EQUAL, so the
# cached map was served verbatim. Harmless while the map could only down-tier; once
# it can grant ABOVE LOCAL_TIER a REVOKED owner grant survives.
#
# NOTE: these rewrites reuse the SAME envelope as _write_map (allowFrom + tierMap).
# An earlier draft dropped allowFrom, which changed st_size — so the size component
# caught the staleness and the tests passed even with the above-local guard removed,
# i.e. they did not exercise the path they claimed. Keep the envelope identical.
import os as _os

def _rewrite_same_identity(tier_map):
    """Rewrite access.json preserving the envelope, then restore mtime exactly."""
    st = _os.stat(ACCESS)
    ACCESS.write_text(json.dumps({"allowFrom": ["@qingyun:ag2.space"], "tierMap": tier_map}))
    _os.utime(ACCESS, ns=(st.st_atime_ns, st.st_mtime_ns))

_prev_local = rgb.LOCAL_TIER
rgb.LOCAL_TIER = "team"

# 25. SAME size, SAME mtime_ns, SAME inode -> identity collides exactly; only the
# above-local re-read can catch the revocation. owner->other keeps byte length.
_write_map({"@dana:ag2.space": "owner"})
assert rgb._tier_for("@dana:ag2.space", "owner") == "owner"           # prime the grant
_rewrite_same_identity({"@dana:ag2.space": "other"})
check("identity-colliding rewrite does NOT serve a revoked owner grant",
      rgb._tier_for("@dana:ag2.space", "owner") == "guest")

# 26. full revocation to an empty map under a restored mtime
_write_map({"@dana:ag2.space": "owner"})
assert rgb._tier_for("@dana:ag2.space", "owner") == "owner"
_rewrite_same_identity({})
check("same-mtime revocation to an empty map drops the grant",
      rgb._tier_for("@dana:ag2.space", "owner") == "team")

# 27. a NON-escalating cache still uses the identity shortcut (no perf regression)
_write_map({"@rick:ag2.space": "team"})
assert rgb._tier_for("@rick:ag2.space", "owner") == "team"
check("cache identity shortcut still serves a non-escalating map",
      rgb._tier_for("@rick:ag2.space", "owner") == "team")
rgb.LOCAL_TIER = _prev_local

_total = 30
print(f"\nResults: {_total - len(failures)}/{_total} passed" if not failures else f"\nResults: FAILED {failures}")
sys.exit(1 if failures else 0)
