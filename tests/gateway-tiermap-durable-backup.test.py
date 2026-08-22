#!/usr/bin/env python3
"""Durable on-disk backup for the ag2-sparrow tierMap — survives wipe + restart.

The in-memory _TIER_MAP_CACHE preserves the owner's down-tier map across a
transient access.json fault, but ONLY while the process lives. A wipe/corrupt
access.json PLUS a process restart empties the cache; without a backup
_load_tier_map() returns {}, and _tier_for() then resolves every previously
down-tiered sender to LOCAL_TIER — on a LOCAL_TIER=owner node a "team" teammate
silently regains owner (no error, no log). This mirrors the slack allowlist
backup (cd5c5db1 / #2163), scoped to the tierMap.

Load-bearing guards:
  - #3: after a wipe + restart with a backup, `@rick` still resolves to `team`.
  - #2: a DELIBERATELY emptied tierMap IS persisted (validate structure, not
        emptiness) — else the fix would restore a down-tier the owner deleted.
"""
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Import the EXACT module this PR modifies as a proper package import so its
# relative imports resolve (same rationale as gateway-per-sender-tier.test.py).
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
import ag2_sparrow.remote_gateway_bridge as rgb  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="rgb-tiermap-backup-"))
ACCESS = tmp / "access.json"
BACKUP = tmp / "auth" / "ag2space-tiermap-backup.json"

rgb._ag2space_access_path = lambda: str(ACCESS)
rgb._TIER_MAP_BACKUP_FILE = BACKUP
rgb.LOCAL_TIER = "owner"


def _cold_process():
    """Simulate a fresh process: empty cache, no remembered mtime."""
    rgb._TIER_MAP_CACHE = {"path": None, "ident": None, "map": {}}


def _write_access(tier_map):
    ACCESS.write_text(json.dumps({"allowFrom": ["@qingyun:ag2.space"], "tierMap": tier_map}))


def _wipe_access():
    if ACCESS.exists():
        ACCESS.unlink()


def _rm_backup():
    if BACKUP.exists():
        BACKUP.unlink()


def _seed_backup(tier_map):
    """Populate a good backup by doing a successful load."""
    _cold_process()
    _write_access(tier_map)
    rgb._load_tier_map()


# ── 1. a successful load writes the durable backup (0600) ────────────────────
_cold_process()
_write_access({"@rick:ag2.space": "team", "@johnm:ag2.space": "team"})
assert rgb._load_tier_map().get("@rick:ag2.space") == "team"
check("successful load writes backup", BACKUP.exists())
check("backup content matches the loaded map",
      json.loads(BACKUP.read_text()).get("@rick:ag2.space") == "team")
check("backup is chmod 0600 (same authz data as access.json)",
      stat.S_IMODE(os.stat(BACKUP).st_mode) == 0o600,
      oct(stat.S_IMODE(os.stat(BACKUP).st_mode)))

# ── 2. a DELIBERATELY empty tierMap IS persisted (structure, not emptiness) ──
# The owner removing every down-tier is a legitimate state. Backing up the OLD
_cold_process()
_write_access({})                     # owner deliberately clears the map
rgb._load_tier_map()
check("deliberate empty map is persisted (not the stale prior map)",
      rgb._restore_tier_map_from_disk() == {})
_cold_process()
_wipe_access()
check("after deliberate-empty, wipe+restart → @rick is LOCAL_TIER, not stale team",
      rgb._tier_for("@rick:ag2.space", "owner") == "owner")

# ── 3. THE REGRESSION: wipe + restart with a backup restores the down-tier ───
_seed_backup({"@rick:ag2.space": "team", "@johnm:ag2.space": "team"})
_cold_process()          # restart: cache empty
_wipe_access()           # access.json gone
_rick = rgb._tier_for("@rick:ag2.space", "owner")
check("wipe+restart: @rick restored to team (NOT owner)", _rick == "team", f"got {_rick}")
check("wipe+restart: @johnm restored to team (NOT owner)",
      rgb._tier_for("@johnm:ag2.space", "owner") == "team")
check("wipe+restart: unlisted sender still LOCAL_TIER (owner)",
      rgb._tier_for("@stranger:ag2.space", "owner") == "owner")

# ── 4. wipe + restart with NO backup → {} → LOCAL_TIER (honest residual) ─────
_rm_backup()
_cold_process()
_wipe_access()
check("wipe+restart, no backup → LOCAL_TIER (nothing known)",
      rgb._tier_for("@rick:ag2.space", "owner") == "owner")

# ── 5. a WARM cache tolerates a transient wipe without needing the backup ────
_cold_process()
_write_access({"@rick:ag2.space": "team"})
rgb._load_tier_map()               # warm the cache
_rm_backup()                       # prove the cache path is used, not the disk
_wipe_access()
check("warm cache survives transient wipe (no backup needed)",
      rgb._tier_for("@rick:ag2.space", "owner") == "team")

# ── 6. a malformed backup file must not raise → falls through to LOCAL_TIER ──
_cold_process()
BACKUP.parent.mkdir(parents=True, exist_ok=True)
BACKUP.write_text("{ this is not json")
_wipe_access()
check("malformed backup → no crash, LOCAL_TIER",
      rgb._tier_for("@rick:ag2.space", "owner") == "owner")

# ── 7. corrupt (present-but-unparseable) access.json + cold + backup ─────────
_cold_process()
BACKUP.write_text(json.dumps({"@rick:ag2.space": "team"}))
ACCESS.write_text("{ half-written")
_rick_corrupt = rgb._tier_for("@rick:ag2.space", "owner")
check("corrupt access.json cold-start restores from backup",
      _rick_corrupt == "team", f"got {_rick_corrupt}")

# ── 8. a genuine backup-write failure must never break tier resolution ───────
# Parent is a FILE, so mkdir(parents=True) raises → the backup write fails, but
_cold_process()
blocker = tmp / "blocker-file"
blocker.write_text("x")
rgb._TIER_MAP_BACKUP_FILE = blocker / "nested" / "b.json"
_write_access({"@rick:ag2.space": "team"})
try:
    got = rgb._tier_for("@rick:ag2.space", "owner")
    check("backup-write failure does not break load", got == "team", f"got {got}")
except Exception as e:  # noqa: BLE001
    check("backup-write failure does not break load", False, str(e))
rgb._TIER_MAP_BACKUP_FILE = BACKUP  # restore

# ── 9. the backup is BORN 0600 — no world-readable write window (#2354 review) ─
# A "final mode is 0600" check (guard 1) is blind to a write_text()-then-chmod()
_cold_process()
rgb._TIER_MAP_BACKUP_FILE = BACKUP
_rm_backup()
_old_umask = os.umask(0o000)
_orig_chmod, _orig_fchmod = os.chmod, os.fchmod
try:
    os.chmod = lambda *a, **k: None    # neutralize any post-write narrowing
    os.fchmod = lambda *a, **k: None
    rgb._backup_tier_map_to_disk({"@rick:ag2.space": "team"})
    born_mode = stat.S_IMODE(os.stat(BACKUP).st_mode)
finally:
    os.chmod, os.fchmod = _orig_chmod, _orig_fchmod
    os.umask(_old_umask)
check("backup is BORN 0600 — no world/group-readable write window",
      born_mode & 0o077 == 0, oct(born_mode))

# ── 10. state/auth/ is created owner-only (0700), umask-independent (#2354 rev) ─
# The dir holds only 0600 secrets, so it must not rely on the parent's incidental
_fresh_auth = tmp / "fresh_auth_dir"
rgb._TIER_MAP_BACKUP_FILE = _fresh_auth / "ag2space-tiermap-backup.json"
_cold_process()
_old_umask2 = os.umask(0o000)
try:
    rgb._backup_tier_map_to_disk({"@rick:ag2.space": "team"})
    dir_mode = stat.S_IMODE(os.stat(_fresh_auth).st_mode)
finally:
    os.umask(_old_umask2)
check("state/auth/ dir created 0700 (no group/other-readable), umask-independent",
      dir_mode & 0o077 == 0, oct(dir_mode))
rgb._TIER_MAP_BACKUP_FILE = BACKUP  # restore

# ── 13-15. post-#2771 merge interactions (two-arg _tier_for + path-scoped cache)

# 13. A cold cache has path=None; reading that as a "path switch" refuses the
# backup on request #1 ONLY, so a single-call test cannot see it.
_seed_backup({"@rick:ag2.space": "team"})
_wipe_access()
_cold_process()
_first = rgb._tier_for("@rick:ag2.space", "owner")
check("cold start, FIRST request: cap held from backup (not just from the 2nd)",
      _first == "team", f"got {_first}")

# 14. The backup is ONE file with no path scoping, so restoring it across a
# config-path switch would leak trust between installs.
_seed_backup({"@rick:ag2.space": "team"})
_wipe_access()
_cold_process()
rgb._TIER_MAP_CACHE["path"] = "/some/other/install/access.json"
_switched = rgb._tier_for("@rick:ag2.space", "owner")
check("path switch does NOT restore another install's backup",
      _switched == "owner", f"got {_switched}")

# 15. Same _stale_safe rule main applies to a stale cache: a restored grant
# above LOCAL_TIER is projected down, not resurrected from disk.
_seed_backup({"@rick:ag2.space": "owner"})
_wipe_access()
_cold_process()
_prev_local, rgb.LOCAL_TIER = rgb.LOCAL_TIER, "team"
_above = rgb._tier_for("@rick:ag2.space", "owner")
rgb.LOCAL_TIER = _prev_local
check("restored grant above LOCAL_TIER is projected down, not resurrected",
      _above == "team", f"got {_above}")


if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("\nPASS — tierMap durable backup survives wipe+restart")
