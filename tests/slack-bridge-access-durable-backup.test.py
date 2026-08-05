#!/usr/bin/env python3
"""Durable on-disk backup for the Slack access allowlist (#899 defense-in-depth).

The in-memory _access_cache restores access.json after an intermittent wipe
only while the process lives. A wipe + process restart (observed 2026-07-17)
loses the cache and the bridge boots into TOFU — the next DM'er becomes owner.
A durable backup under state/auth/ closes that: on a fresh process with the
in-memory cache empty, tofu_onboard restores from disk instead of enrolling.

Guards:
  1. a valid enrolled state gets backed up to disk on cache update
  2. an empty / TOFU-pending state does NOT overwrite the good backup
  3. wipe + fresh process (cache cleared) → tofu_onboard restores from disk,
     does NOT create a new TOFU owner
  4. no backup + no cache → genuine TOFU still works (first run)

Run: python3 tests/slack-bridge-access-durable-backup.test.py  (exit 0/1)
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    class _FakeApp:
        def __init__(self, token=None):
            self.client = types.SimpleNamespace(
                chat_postMessage=lambda **k: {"ok": True},
                conversations_replies=lambda **k: {"ok": True, "messages": []},
            )

        def _d(self, *a, **k):
            return lambda fn: fn

        event = message = command = action = shortcut = view = _d

    b = types.ModuleType("slack_bolt"); b.App = _FakeApp
    sys.modules["slack_bolt"] = b
    ad = types.ModuleType("slack_bolt.adapter")
    sm = types.ModuleType("slack_bolt.adapter.socket_mode")
    sm.SocketModeHandler = type("SocketModeHandler", (), {"__init__": lambda self, *a, **k: None})
    sys.modules["slack_bolt.adapter"] = ad
    sys.modules["slack_bolt.adapter.socket_mode"] = sm
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test"
    # Hermeticity (#2163 CR): isolate CLAUDE_CONFIG_DIR to a temp root BEFORE
    # exec_module. slack-bridge.py resolves its channel .env + access.json at
    # IMPORT time and chmod 0600's the .env when it exists — under the real
    # ~/.claude that mutates a developer's live Slack credentials. Pointing
    # CLAUDE_CONFIG_DIR at an empty temp dir makes claude_home_path() /
    # channel_access_path() resolve inside temp and keeps the suite off live
    # config. Create the canonical access file before import: otherwise the
    # resolver can legitimately fall back to a legacy HOME path when that file
    # exists on the host, defeating this isolation despite CLAUDE_CONFIG_DIR.
    ccd = Path(tempfile.mkdtemp(prefix="sl-dbak-ccd-"))
    os.environ["CLAUDE_CONFIG_DIR"] = str(ccd)
    canonical_access = ccd / "channels" / "slack" / "access.json"
    canonical_access.parent.mkdir(parents=True)
    canonical_access.write_text('{"allowFrom":[]}\n')
    canonical_access.chmod(0o600)
    spec = importlib.util.spec_from_file_location("slackbridge_dbak", REPO / "src" / "slack-bridge.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, ccd


slack, _ccd = _load()
# Capture the IMPORT-time path resolution before the test reassigns ACCESS_FILE,
# so we can assert nothing resolved to the real ~/.claude channel config.
_import_access = slack.ACCESS_FILE
_import_env = slack.channels_env
_tmp = Path(tempfile.mkdtemp(prefix="sl-dbak-"))
slack.ACCESS_FILE = _tmp / "access.json"
slack.ACCESS_BACKUP_FILE = _tmp / "auth" / "slack-access-backup.json"

failures: list[str] = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# 0. Hermeticity gate: the import above must have resolved every channel path
#    inside the isolated temp CLAUDE_CONFIG_DIR — never the developer's live
#    ~/.claude/channels/slack config (no read, no chmod, no write).
_ccd_root = str(_ccd)
check("import resolved ACCESS_FILE inside the temp CLAUDE_CONFIG_DIR (hermetic)",
      str(_import_access).startswith(_ccd_root), f"{_import_access} not under {_ccd_root}")
check("import resolved channels .env inside the temp CLAUDE_CONFIG_DIR (no live-config chmod)",
      str(_import_env).startswith(_ccd_root), f"{_import_env} not under {_ccd_root}")


def _reset_cache():
    if hasattr(slack, "_access_cache"):
        slack._access_cache = None


# 1. valid enrolled state → backed up to disk
good = {"allowFrom": ["U_OWNER"], "tierMap": {"U_OWNER": "owner"}, "tofuOwner": "U_OWNER"}
slack._update_access_cache(good)
check("valid state is backed up to disk", slack.ACCESS_BACKUP_FILE.exists())
check("backup content matches", json.loads(slack.ACCESS_BACKUP_FILE.read_text()).get("tofuOwner") == "U_OWNER")

# 2. a transient/partial wipe (schema-INVALID: no allowFrom list) does NOT
#    overwrite the good backup. (An intentional empty allowlist IS valid and IS
#    backed up — that's covered in the migration-state tests below. CR #2163.)
slack._update_access_cache({"pending": {}})  # no allowFrom key → partial wipe
check("partial-wipe (no allowFrom) does not clobber the backup",
      json.loads(slack.ACCESS_BACKUP_FILE.read_text()).get("tofuOwner") == "U_OWNER")
slack._backup_access_to_disk("not-a-dict")  # type: ignore[arg-type]  — malformed
check("non-dict does not clobber the backup",
      json.loads(slack.ACCESS_BACKUP_FILE.read_text()).get("tofuOwner") == "U_OWNER")

# 3. wipe + fresh process: access.json gone, in-memory cache cleared → restore from disk
slack.ACCESS_FILE.unlink(missing_ok=True)
_reset_cache()
result = slack.tofu_onboard("U_STRANGER", "stranger")
restored = json.loads(slack.ACCESS_FILE.read_text()) if slack.ACCESS_FILE.exists() else {}
check("wipe+restart restores from disk backup (not TOFU)", restored.get("tofuOwner") == "U_OWNER",
      f"got {restored.get('tofuOwner')}")
check("stranger is NOT enrolled as owner", "U_STRANGER" not in (restored.get("allowFrom") or []),
      str(restored.get("allowFrom")))
check("restored allowlist returned", "U_OWNER" in (result or set()))

# 4. no backup + no cache → genuine TOFU still works (first run)
slack.ACCESS_FILE.unlink(missing_ok=True)
slack.ACCESS_BACKUP_FILE.unlink(missing_ok=True)
_reset_cache()
first = slack.tofu_onboard("U_FIRST", "first-user")
fresh = json.loads(slack.ACCESS_FILE.read_text())
check("genuine first-run TOFU still enrolls when no backup exists",
      fresh.get("tofuOwner") == "U_FIRST" and "U_FIRST" in first)

# 3b. EXPLICIT before/after wipe+restart contrast (CR #2163 missing_before_after)
# Same scenario — access.json wiped, fresh process (cache cleared), a stranger
# sends the first DM — run in both worlds, asserting the resulting OWNER differs.
#   BEFORE (no durable backup, the pre-fix world): stranger is TOFU-enrolled as owner.
#   AFTER  (durable backup present, this fix): the original owner is restored;
#          the stranger is not owner.
_owner_before = _tmp / "auth" / "slack-access-backup.json"
# BEFORE: no backup on disk, no cache → wipe+restart enrolls the stranger.
slack.ACCESS_FILE.unlink(missing_ok=True)
slack.ACCESS_BACKUP_FILE.unlink(missing_ok=True)
_reset_cache()
slack.tofu_onboard("U_ATTACKER", "attacker")
before_owner = json.loads(slack.ACCESS_FILE.read_text()).get("tofuOwner")
# AFTER: a good backup exists on disk (written before the wipe) → restore, no enroll.
slack.ACCESS_FILE.unlink(missing_ok=True)
_reset_cache()
slack.ACCESS_BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
slack.ACCESS_BACKUP_FILE.write_text(json.dumps(
    {"allowFrom": ["U_REAL_OWNER"], "tierMap": {"U_REAL_OWNER": "owner"}, "tofuOwner": "U_REAL_OWNER"}) + "\n")
slack.tofu_onboard("U_ATTACKER", "attacker")
after_owner = json.loads(slack.ACCESS_FILE.read_text()).get("tofuOwner")
check("BEFORE-fix: wipe+restart TOFU-enrolls the stranger as owner",
      before_owner == "U_ATTACKER", f"got {before_owner}")
check("AFTER-fix: wipe+restart restores the real owner, not the stranger",
      after_owner == "U_REAL_OWNER", f"got {after_owner}")
check("before/after owner identity differs (exposure closed)",
      before_owner != after_owner and after_owner == "U_REAL_OWNER",
      f"before={before_owner} after={after_owner}")

# ── error-branch coverage on the helpers directly ─────────────────────────────
# backup: OSError during write is swallowed (best-effort), never raises
import unittest.mock as _mock
with _mock.patch.object(slack.ACCESS_BACKUP_FILE.__class__, "write_text", side_effect=OSError("disk full")):
    slack._backup_access_to_disk({"tofuOwner": "U", "allowFrom": ["U"]})  # must not raise
check("backup swallows OSError (best-effort)", True)

# backup: schema-invalid doc (no allowFrom list) → early return, no file written
slack.ACCESS_BACKUP_FILE.unlink(missing_ok=True)
slack._backup_access_to_disk({"pending": {}})
check("backup skips a schema-invalid (no-allowFrom) doc", not slack.ACCESS_BACKUP_FILE.exists())

# restore: missing backup file → False
slack.ACCESS_BACKUP_FILE.unlink(missing_ok=True)
check("restore returns False when backup absent", slack._restore_access_from_disk() is False)

# restore: backup present but schema-invalid (no allowFrom list) → False
slack.ACCESS_BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
slack.ACCESS_BACKUP_FILE.write_text('{"pending": {}}')
check("restore returns False when backup is schema-invalid", slack._restore_access_from_disk() is False)

# restore: valid backup but ACCESS_FILE write fails → False (exception branch)
slack.ACCESS_BACKUP_FILE.write_text('{"tofuOwner": "U", "allowFrom": ["U"]}')
# NOTE: the write now goes through util_paths.write_private_text (born-0600), so the
# failure must be injected at that seam rather than Path.write_text. Same intent:
# simulate an unwritable access.json and assert the exception branch returns False.
with _mock.patch.object(slack, "write_private_text", side_effect=OSError("readonly")):
    check("restore returns False when access.json write fails", slack._restore_access_from_disk() is False)

# ── migration states the old tofuOwner predicate left unprotected (CR #2163) ──
# (A) legacy populated allowlist enrolled BEFORE tofuOwner existed: must be
#     backed up AND restored, or a wipe+restart re-opens TOFU.
slack.ACCESS_FILE.unlink(missing_ok=True)
slack.ACCESS_BACKUP_FILE.unlink(missing_ok=True)
_reset_cache()
legacy = {"allowFrom": ["U_LEGACY"], "tierMap": {"U_LEGACY": "owner"}}  # no tofuOwner
slack._update_access_cache(legacy)
check("legacy populated allowlist (no tofuOwner) is backed up", slack.ACCESS_BACKUP_FILE.exists())
slack.ACCESS_FILE.unlink(missing_ok=True)
_reset_cache()
slack.tofu_onboard("U_STRANGER", "stranger")
restored_legacy = json.loads(slack.ACCESS_FILE.read_text())
check("legacy allowlist restored on wipe+restart, stranger not enrolled",
      restored_legacy.get("allowFrom") == ["U_LEGACY"] and "U_STRANGER" not in restored_legacy.get("allowFrom", []),
      str(restored_legacy))

# (B) intentional locked-down state allowFrom: [] : must be backed up AND
#     restored, so a wipe+restart preserves the lockdown (no TOFU re-opening).
slack.ACCESS_FILE.unlink(missing_ok=True)
slack.ACCESS_BACKUP_FILE.unlink(missing_ok=True)
_reset_cache()
slack._update_access_cache({"allowFrom": []})  # deliberate lockdown
check("intentional empty lockdown is backed up",
      slack.ACCESS_BACKUP_FILE.exists()
      and json.loads(slack.ACCESS_BACKUP_FILE.read_text()).get("allowFrom") == [])
slack.ACCESS_FILE.unlink(missing_ok=True)
_reset_cache()
slack.tofu_onboard("U_STRANGER2", "stranger2")
restored_lockdown = json.loads(slack.ACCESS_FILE.read_text())
check("empty lockdown restored on wipe+restart, stranger not enrolled",
      restored_lockdown.get("allowFrom") == [] and "U_STRANGER2" not in restored_lockdown.get("allowFrom", []),
      str(restored_lockdown))


# --- _restore_access_from_cache: the in-memory path (#899 external-deletion) ----
# Distinct from _restore_access_from_disk above: this one rewrites ACCESS_FILE from
# the live cache when the file is deleted out from under a running bridge. It writes
# authorization data, so assert the file comes back owner-only as well as correct.
slack._update_access_cache({"tofuOwner": "UCACHE", "allowFrom": ["UCACHE"]})
slack.ACCESS_FILE.unlink(missing_ok=True)
_restored = slack._restore_access_from_cache()
check("cache restore returns True when a cached owner exists", _restored is True)
check("cache restore recreates access.json", slack.ACCESS_FILE.exists())
check("cache-restored access.json keeps the cached owner",
      json.loads(slack.ACCESS_FILE.read_text()).get("tofuOwner") == "UCACHE")
check("cache-restored access.json is owner-only (0600)",
      stat.S_IMODE(os.stat(slack.ACCESS_FILE).st_mode) == 0o600)

# and the negative: an empty cache must not fabricate a file
slack._update_access_cache({})
slack.ACCESS_FILE.unlink(missing_ok=True)
check("cache restore returns False with no cached owner",
      slack._restore_access_from_cache() is False)
check("cache restore did not create a file when it returned False",
      not slack.ACCESS_FILE.exists())


print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — slack access durable backup")
