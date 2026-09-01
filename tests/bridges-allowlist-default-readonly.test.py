#!/usr/bin/env python3
"""Allowlist default tier = read-only (owner request 2026-07-17).

Before: a user added to `allowFrom` with no `tierMap` entry was resolved as
OWNER (Slack: only when tierMap absent; Discord: always) — so "add to the
allowlist" silently granted full core capabilities. Fix: a one-time
grandfather-seed writes the CURRENT allowFrom as owner into tierMap, after
which any NEW allowFrom addition is missing from tierMap and resolves to a
read-only tier ("other" on Slack, "team" on Discord).

Guards (behavioral, against the real access.json writers — ACCESS_FILE is
redirected to a temp path so the real owner allowlist is never touched):
  Slack:
    1. seed grandfathers existing allowFrom -> owner in tierMap
    2. seed is idempotent (no-op when tierMap already present)
    3. a newly-added allowFrom uid (post-seed) is NOT owner
    4. TOFU onboarding writes tierMap with the enrollee as owner
  Discord:
    5. seed grandfathers existing allowFrom -> owner
    6. a newly-added allowFrom uid (post-seed) resolves to team, not owner

Run: python3 tests/bridges-allowlist-default-readonly.test.py  (exit 0/1)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── Load slack-bridge with a stubbed slack_bolt + isolated ACCESS_FILE ────────
def _load_slack():
    class _FakeApp:
        def __init__(self, token=None):
            self.client = types.SimpleNamespace(
                chat_postMessage=lambda **k: {"ok": True},
                conversations_replies=lambda **k: {"ok": True, "messages": []},
            )

        def _d(self, *a, **k):
            return lambda fn: fn

        event = message = command = action = shortcut = view = _d

    _bolt = types.ModuleType("slack_bolt"); _bolt.App = _FakeApp
    sys.modules["slack_bolt"] = _bolt
    _ad = types.ModuleType("slack_bolt.adapter")
    _sm = types.ModuleType("slack_bolt.adapter.socket_mode")
    _sm.SocketModeHandler = type("SocketModeHandler", (), {"__init__": lambda self, *a, **k: None})
    sys.modules["slack_bolt.adapter"] = _ad
    sys.modules["slack_bolt.adapter.socket_mode"] = _sm
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test"
    spec = importlib.util.spec_from_file_location("slackbridge_acl", REPO / "src" / "slack-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


slack = _load_slack()
_sf = Path(tempfile.mkdtemp(prefix="sl-acl-")) / "access.json"
slack.ACCESS_FILE = _sf
slack.ACCESS_BACKUP_FILE = _sf.parent / "slack-access-backup.json"


def _write_slack(d):
    _sf.write_text(json.dumps(d))
    slack._update_access_cache(d) if hasattr(slack, "_update_access_cache") else None


# 1. grandfather existing allowFrom -> owner
_write_slack({"allowFrom": ["U_OWNER", "U_OLD"]})
slack._ensure_tier_map_seeded()
seeded = json.loads(_sf.read_text()).get("tierMap", {})
check("slack: seed grandfathers existing allowFrom as owner",
      seeded.get("U_OWNER") == "owner" and seeded.get("U_OLD") == "owner", str(seeded))

# 2. idempotent
before = _sf.read_text()
slack._ensure_tier_map_seeded()
check("slack: seed is idempotent", _sf.read_text() == before)

# 3. newly-added allowFrom uid is NOT owner (missing from tierMap -> "other")
data = json.loads(_sf.read_text())
data["allowFrom"].append("U_NEW")
_write_slack(data)
slack._ensure_tier_map_seeded()  # no-op now (tierMap present)
tm = slack.load_tier_map()
new_tier = tm.get("U_NEW", "other" if tm else "owner")
check("slack: newly-added allowlist user is not owner", new_tier != "owner", f"got {new_tier}")

# 4. TOFU writes tierMap with enrollee as owner
_sf.unlink(missing_ok=True)
slack.ACCESS_BACKUP_FILE.unlink(missing_ok=True)
if hasattr(slack, "_access_cache"):
    slack._access_cache = None
slack.tofu_onboard("U_TOFU", "tofu-user")
tofu = json.loads(_sf.read_text())
check("slack: TOFU writes tierMap with enrollee as owner",
      tofu.get("tierMap", {}).get("U_TOFU") == "owner", str(tofu.get("tierMap")))

# 4b. ORDERING (CR #2161 incomplete_fix): the grandfather snapshot must be
# pinned at STARTUP, before a new allowFrom addition can trigger the on-demand
# seed and grandfather *itself*. Contrast the two orderings on a fresh
# (no-tierMap) install — this is the before/after the startup-seed call fixes.
# BEFORE-fix ordering: the new id (U_LATE) is already in allowFrom when the
# (lazy, on-message) seed first fires → it gets captured as owner. This is the
# hole; the startup seed in main()/on_ready() closes it.
_write_slack({"allowFrom": ["U_E", "U_LATE"]})   # no tierMap; U_LATE present pre-seed
slack._ensure_tier_map_seeded()
check("slack: BEFORE-fix ordering — a pre-seed allowFrom addition is grandfathered owner",
      slack.load_tier_map().get("U_LATE") == "owner", str(slack.load_tier_map()))
# AFTER-fix ordering: the startup seed pins the snapshot to [U_E]; U_LATE added
# later is missing from tierMap and stays read-only.
_write_slack({"allowFrom": ["U_E"]})             # fresh, no tierMap
slack._ensure_tier_map_seeded()                  # STARTUP snapshot = {U_E: owner}
_d = json.loads(_sf.read_text()); _d["allowFrom"].append("U_LATE"); _write_slack(_d)  # owner adds later
slack._ensure_tier_map_seeded()                  # no-op — tierMap already present
check("slack: AFTER-fix ordering — startup seed keeps a later allowFrom addition read-only",
      slack.load_tier_map().get("U_LATE", "other") != "owner", str(slack.load_tier_map()))

# 5. seed swallows a read failure (except Exception -> return, no write)
class _ReadRaises:
    def read_text(self):
        raise OSError("access.json unreadable")
    def write_text(self, *a, **k):
        raise AssertionError("seed must not write when the read failed")
slack.ACCESS_FILE = _ReadRaises()
slack._ensure_tier_map_seeded()  # must swallow and return without writing
check("slack: seed swallows a read failure", True)
slack.ACCESS_FILE = _sf

# 6. seed WRITE failure is ATOMIC (#2161 CR: "add a regression proving a failed
#    migration leaves the original access.json bytes intact"). The migration
#    writes a sibling temp + os.replace(); when the commit fails it must (a)
#    return False, (b) leave the original access.json BYTES INTACT (never
#    truncated), (c) leave no orphan .tmp, and (d) fail CLOSED — an allowlisted
#    user stays read-only ("other"), never owner. Inject the failure at the
#    os.replace commit (the truncate-in-place bug's danger point).
_wf = Path(tempfile.mkdtemp(prefix="sl-writefail-")) / "access.json"
_orig_bytes = json.dumps({"allowFrom": ["U_W"]})  # no tierMap -> triggers the write
_wf.write_text(_orig_bytes)
slack.ACCESS_FILE = _wf
if hasattr(slack, "_access_cache"):
    slack._access_cache = None


def _boom(*a, **k):
    raise OSError("disk full during commit")


_orig_replace = slack.os.replace
slack.os.replace = _boom
try:
    seeded_ok = slack._ensure_tier_map_seeded()  # must swallow the OSError, return False
finally:
    slack.os.replace = _orig_replace
check("slack: seed write failure returns False (explicit, not silent)", seeded_ok is False, f"got {seeded_ok!r}")
check("slack: failed migration leaves access.json bytes intact (atomic — no truncation)",
      _wf.read_text() == _orig_bytes, f"file mutated to {_wf.read_text()!r}")
check("slack: failed migration leaves no orphan .tmp",
      not list(_wf.parent.glob("*.tmp")), str(list(_wf.parent.glob("*.tmp"))))
# Fail closed: with no tierMap persisted, an allowlisted user resolves read-only.
if hasattr(slack, "_access_cache"):
    slack._access_cache = None
_tm = slack.load_tier_map()  # {} — no tierMap on disk
_resolved = _tm["U_W"] if "U_W" in _tm else "other"  # mirror: owner only via membership
check("slack: seed WRITE failure resolves allowlisted user read-only, NOT owner",
      _resolved != "owner" and _resolved == "other", f"got {_resolved!r}")
slack.ACCESS_FILE = _sf
if hasattr(slack, "_access_cache"):
    slack._access_cache = None

# 6a. Empty allowFrom: nothing to grandfather. The seed must return True WITHOUT
#     writing a tierMap — an empty allowFrom is a legitimate locked-down state,
#     not a migration target. Covers the `if not allow: return True` branch.
_ef = Path(tempfile.mkdtemp(prefix="sl-emptyallow-")) / "access.json"
_ef.write_text(json.dumps({"allowFrom": []}))
slack.ACCESS_FILE = _ef
if hasattr(slack, "_access_cache"):
    slack._access_cache = None
_empty_ok = slack._ensure_tier_map_seeded()
check("slack: empty allowFrom seeds nothing and returns True",
      _empty_ok is True, f"got {_empty_ok!r}")
check("slack: empty allowFrom writes no tierMap",
      "tierMap" not in json.loads(_ef.read_text()), _ef.read_text())
slack.ACCESS_FILE = _sf
if hasattr(slack, "_access_cache"):
    slack._access_cache = None

# 6b. Double-fault: the commit fails AND the orphan-temp cleanup ALSO fails. The
#     inner `except OSError: pass` around tmp.unlink() must swallow the second
#     error so the seed still returns False cleanly (never raises). Force it by
#     deleting the temp inside the replace-boom, so the code's own tmp.unlink()
#     then raises FileNotFoundError (an OSError).
_wf2 = Path(tempfile.mkdtemp(prefix="sl-doublefault-")) / "access.json"
_wf2.write_text(_orig_bytes)
slack.ACCESS_FILE = _wf2
if hasattr(slack, "_access_cache"):
    slack._access_cache = None


def _boom_del_tmp(src, dst, *a, **k):
    Path(src).unlink()  # remove the temp so the except's tmp.unlink() raises
    raise OSError("disk full during commit")


_orig_replace2 = slack.os.replace
slack.os.replace = _boom_del_tmp
try:
    df_ok = slack._ensure_tier_map_seeded()  # inner except must swallow the unlink error
finally:
    slack.os.replace = _orig_replace2
check("slack: write failure with a failed temp-cleanup still returns False (no raise)",
      df_ok is False, f"got {df_ok!r}")
check("slack: double-fault leaves original access.json bytes intact",
      _wf2.read_text() == _orig_bytes, f"file mutated to {_wf2.read_text()!r}")
slack.ACCESS_FILE = _sf
if hasattr(slack, "_access_cache"):
    slack._access_cache = None

# 6c. Explicit empty tierMap ({}) is PRESERVED, not re-seeded (#2161 CR). A
#     present-but-empty map is a deliberate "nobody is owner via tierMap" state;
#     the seed keys on truthiness before this fix, so {} looked identical to a
#     missing key and re-grandfathered every allowFrom member as owner. Repro:
#     {"allowFrom":["U_RO"],"tierMap":{}} must stay {} — U_RO resolves read-only.
_etm = Path(tempfile.mkdtemp(prefix="sl-emptytiermap-")) / "access.json"
_etm.write_text(json.dumps({"allowFrom": ["U_RO"], "tierMap": {}}))
slack.ACCESS_FILE = _etm
if hasattr(slack, "_access_cache"):
    slack._access_cache = None
_etm_ok = slack._ensure_tier_map_seeded()
_etm_after = json.loads(_etm.read_text()).get("tierMap")
check("slack: explicit empty tierMap returns True (already-configured, no seed)",
      _etm_ok is True, f"got {_etm_ok!r}")
check("slack: explicit empty tierMap is NOT re-seeded to owner",
      _etm_after == {}, f"tierMap mutated to {_etm_after!r}")
check("slack: allowlisted user under empty tierMap resolves read-only (not owner)",
      slack.load_tier_map().get("U_RO") is None, str(slack.load_tier_map()))
slack.ACCESS_FILE = _sf
if hasattr(slack, "_access_cache"):
    slack._access_cache = None

# 6d. A present-but-invalid tierMap must normalize to empty, not leak a
#     list/scalar into runtime membership/index operations. Invalid config
#     fails closed: the allowlisted user remains unmapped and therefore
#     resolves to the read-only tier.
_itm = Path(tempfile.mkdtemp(prefix="sl-invalidtiermap-")) / "access.json"
_itm.write_text(json.dumps({"allowFrom": ["U_RO"], "tierMap": ["U_RO"]}))
slack.ACCESS_FILE = _itm
if hasattr(slack, "_access_cache"):
    slack._access_cache = None
check("slack: invalid tierMap normalizes to empty and fails closed",
      slack.load_tier_map() == {}, str(slack.load_tier_map()))
slack.ACCESS_FILE = _sf
if hasattr(slack, "_access_cache"):
    slack._access_cache = None

# 7. _write_task runs the tier-map seed call + resolution (covers the seed call
#    site) for a non-owner sender, and writes the task to a redirected TASKS_DIR.
_sf.write_text(json.dumps({"allowFrom": ["U_X"], "tierMap": {"U_SEED": "owner"}}))
if hasattr(slack, "_update_access_cache"):
    slack._update_access_cache(json.loads(_sf.read_text()))
_tasks_tmp = Path(tempfile.mkdtemp(prefix="sl-tasks-"))
_orig_tasks_dir = slack.TASKS_DIR
_orig_woa = getattr(slack, "write_owner_activity", None)
slack.TASKS_DIR = _tasks_tmp
slack.write_owner_activity = lambda *a, **k: None
try:
    _tid = slack._write_task(
        {"user": "U_X", "channel": "C1", "channel_type": "im", "ts": "123.456"},
        "dm", "hello world", "xuser",
    )
    check("slack: _write_task resolves non-owner tier + writes task (covers seed call site)",
          _tid is not None and (_tasks_tmp / f"{_tid}.txt").exists(), f"tid={_tid}")
finally:
    slack.TASKS_DIR = _orig_tasks_dir
    if _orig_woa is not None:
        slack.write_owner_activity = _orig_woa


# ── Discord ──────────────────────────────────────────────────────────────────
os.environ.setdefault("DISCORD_BOT_TOKEN", "faketoken")
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="dc-acl-ccd-")
try:
    import discord  # noqa: F401
    _have_discord = True
except ImportError:
    _have_discord = False
    for _cand in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(_cand) and os.path.realpath(_cand) != os.path.realpath(sys.executable):
            import subprocess
            if subprocess.run([_cand, "-c", "import discord"], capture_output=True).returncode == 0:
                os.execv(_cand, [_cand, os.path.abspath(__file__), *sys.argv[1:]])

if _have_discord:
    spec = importlib.util.spec_from_file_location("discordbridge_acl", REPO / "src" / "discord-bridge.py")
    dmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dmod)
    _df = Path(tempfile.mkdtemp(prefix="dc-acl-")) / "access.json"
    dmod.ACCESS_FILE = _df
    # Isolate the durable access backup too — ensure_tier_map_seeded() now
    # mirrors every valid write to ACCESS_BACKUP_FILE. Without this override the
    # grandfather write below would scribble test data into the real workspace
    # state/auth/discord-access-backup.json (mirrors the slack override above).
    dmod.ACCESS_BACKUP_FILE = _df.parent / "discord-access-backup.json"

    # 5. grandfather
    _df.write_text(json.dumps({"allowFrom": ["111", "222"]}))
    dmod.ensure_tier_map_seeded()
    dseed = json.loads(_df.read_text()).get("tierMap", {})
    check("discord: seed grandfathers existing allowFrom as owner",
          dseed.get("111") == "owner" and dseed.get("222") == "owner", str(dseed))

    # 6. newly-added -> team, resolution mirrors the handler
    data = json.loads(_df.read_text()); data["allowFrom"].append("333"); _df.write_text(json.dumps(data))
    dmod.ensure_tier_map_seeded()  # no-op
    tmap = dmod.load_tier_map()
    resolved = tmap.get("333", "owner" if not tmap else "team")
    check("discord: newly-added allowlist user resolves to team, not owner",
          resolved == "team", f"got {resolved}")

    # 6b. ORDERING (CR #2161 incomplete_fix): pin the snapshot at STARTUP so a
    # new allowFrom id added before the first-ever (lazy) seed isn't itself
    # grandfathered. Discord was the worse offender pre-fix (global allowFrom =
    # unconditional owner). Before/after the on_ready() startup-seed call:
    _dord = Path(tempfile.mkdtemp(prefix="dc-order-")) / "access.json"
    dmod.ACCESS_FILE = _dord
    # BEFORE-fix ordering: new id (444) already in allowFrom when the seed fires.
    _dord.write_text(json.dumps({"allowFrom": ["111", "444"]}))  # no tierMap
    dmod.ensure_tier_map_seeded()
    check("discord: BEFORE-fix ordering — a pre-seed allowFrom addition is grandfathered owner",
          dmod.load_tier_map().get("444") == "owner", str(dmod.load_tier_map()))
    # AFTER-fix ordering: startup seed pins snapshot to [111]; 444 added later stays team.
    _dord.write_text(json.dumps({"allowFrom": ["111"]}))         # fresh, no tierMap
    dmod.ensure_tier_map_seeded()                                # STARTUP snapshot = {111: owner}
    _dd = json.loads(_dord.read_text()); _dd["allowFrom"].append("444"); _dord.write_text(json.dumps(_dd))
    dmod.ensure_tier_map_seeded()                                # no-op — tierMap present
    _dtm = dmod.load_tier_map()
    check("discord: AFTER-fix ordering — startup seed keeps a later allowFrom addition read-only (team)",
          _dtm.get("444", "team" if _dtm else "owner") == "team", str(_dtm))
    dmod.ACCESS_FILE = _df

    # 7. seed WRITE failure is ATOMIC (#2161 CR): the migration writes a sibling
    #    temp + os.replace(); a failed commit must (a) return False, (b) leave
    #    the original access.json BYTES INTACT (never truncated), (c) leave no
    #    orphan .tmp, and (d) fail CLOSED — an allowlisted sender stays "team",
    #    never owner. Inject the failure at the os.replace commit.
    _dwf = Path(tempfile.mkdtemp(prefix="dc-writefail-")) / "access.json"
    _dorig = json.dumps({"allowFrom": ["999"]})  # no tierMap -> triggers the write
    _dwf.write_text(_dorig)
    dmod.ACCESS_FILE = _dwf

    def _dboom(*a, **k):
        raise OSError("disk full during commit")

    _dorig_replace = dmod.os.replace
    dmod.os.replace = _dboom
    try:
        seeded_ok = dmod.ensure_tier_map_seeded()  # must swallow the OSError, return False
    finally:
        dmod.os.replace = _dorig_replace
    check("discord: seed write failure returns False (explicit, not silent)", seeded_ok is False, f"got {seeded_ok!r}")
    check("discord: failed migration leaves access.json bytes intact (atomic — no truncation)",
          _dwf.read_text() == _dorig, f"file mutated to {_dwf.read_text()!r}")
    check("discord: failed migration leaves no orphan .tmp",
          not list(_dwf.parent.glob("*.tmp")), str(list(_dwf.parent.glob("*.tmp"))))
    _dtm = dmod.load_tier_map()  # {} — the write failed, tierMap never persisted
    # Mirror the source resolution (owner strictly via membership; empty → team):
    _dres = _dtm["999"] if "999" in _dtm else "team"
    check("discord: seed WRITE failure resolves allowlisted sender read-only (team), NOT owner",
          _dres != "owner" and _dres == "team", f"got {_dres!r}")
    dmod.ACCESS_FILE = _df

    # 7a. Empty allowFrom: nothing to grandfather — return True without writing.
    _defile = Path(tempfile.mkdtemp(prefix="dc-emptyallow-")) / "access.json"
    _defile.write_text(json.dumps({"allowFrom": []}))
    dmod.ACCESS_FILE = _defile
    _dempty_ok = dmod.ensure_tier_map_seeded()
    check("discord: empty allowFrom seeds nothing and returns True",
          _dempty_ok is True, f"got {_dempty_ok!r}")
    check("discord: empty allowFrom writes no tierMap",
          "tierMap" not in json.loads(_defile.read_text()), _defile.read_text())
    dmod.ACCESS_FILE = _df

    # 7b. Double-fault: commit fails AND the orphan-temp cleanup ALSO fails. The
    #     inner `except OSError: pass` around tmp.unlink() must swallow the second
    #     error so the seed still returns False cleanly. Delete the temp inside
    #     the replace-boom so the code's own tmp.unlink() raises FileNotFoundError.
    _dwf2 = Path(tempfile.mkdtemp(prefix="dc-doublefault-")) / "access.json"
    _dwf2.write_text(_dorig)
    dmod.ACCESS_FILE = _dwf2

    def _dboom_del_tmp(src, dst, *a, **k):
        Path(src).unlink()  # remove the temp so the except's tmp.unlink() raises
        raise OSError("disk full during commit")

    _dorig_replace2 = dmod.os.replace
    dmod.os.replace = _dboom_del_tmp
    try:
        _ddf_ok = dmod.ensure_tier_map_seeded()  # inner except must swallow the unlink error
    finally:
        dmod.os.replace = _dorig_replace2
    check("discord: write failure with a failed temp-cleanup still returns False (no raise)",
          _ddf_ok is False, f"got {_ddf_ok!r}")
    check("discord: double-fault leaves original access.json bytes intact",
          _dwf2.read_text() == _dorig, f"file mutated to {_dwf2.read_text()!r}")
    dmod.ACCESS_FILE = _df

    # 7c. Explicit empty tierMap ({}) is PRESERVED, not re-seeded (#2161 CR).
    #     Discord was the worse offender: a present-but-empty map read as falsy
    #     re-grandfathered every allowFrom id as owner. Repro:
    #     {"allowFrom":["555"],"tierMap":{}} must stay {} — 555 resolves team.
    _detm = Path(tempfile.mkdtemp(prefix="dc-emptytiermap-")) / "access.json"
    _detm.write_text(json.dumps({"allowFrom": ["555"], "tierMap": {}}))
    dmod.ACCESS_FILE = _detm
    _detm_ok = dmod.ensure_tier_map_seeded()
    _detm_after = json.loads(_detm.read_text()).get("tierMap")
    check("discord: explicit empty tierMap returns True (already-configured, no seed)",
          _detm_ok is True, f"got {_detm_ok!r}")
    check("discord: explicit empty tierMap is NOT re-seeded to owner",
          _detm_after == {}, f"tierMap mutated to {_detm_after!r}")
    check("discord: allowlisted id under empty tierMap resolves read-only (not owner)",
          dmod.load_tier_map().get("555") is None, str(dmod.load_tier_map()))
    dmod.ACCESS_FILE = _df

    # 7d. Present-but-invalid maps normalize to empty instead of reaching the
    #      async handler's dict-style membership/index operations.
    _ditm = Path(tempfile.mkdtemp(prefix="dc-invalidtiermap-")) / "access.json"
    _ditm.write_text(json.dumps({"allowFrom": ["555"], "tierMap": ["555"]}))
    dmod.ACCESS_FILE = _ditm
    check("discord: invalid tierMap normalizes to empty and fails closed",
          dmod.load_tier_map() == {}, str(dmod.load_tier_map()))
    dmod.ACCESS_FILE = _df

    # 8. load_tier_map / seed swallow a read failure (except -> {} / return)
    # _locked() touches path.parent/.with_suffix before any read, so this
    # double delegates everything but read_text() to a real tmp Path.
    class _DReadRaises:
        def __init__(self):
            self._real = Path(tempfile.mkdtemp(prefix="dc-unreadable-")) / "access.json"
            self._real.write_text("{}")
        def __getattr__(self, name):
            return getattr(self._real, name)
        def read_text(self, *a, **k):
            raise OSError("access.json unreadable")
    dmod.ACCESS_FILE = _DReadRaises()
    check("discord: load_tier_map swallows a read failure", dmod.load_tier_map() == {})
    dmod.ensure_tier_map_seeded()  # must swallow + return
    check("discord: seed swallows a read failure", True)
    dmod.ACCESS_FILE = _df

    # 9. _handle_discord_message resolves owner via the tier-map seed (covers the
    #    access-tier resolution block): a DM from an allowlisted sender with no
    #    tierMap gets grandfathered owner, and owner activity is recorded.
    import asyncio as _asyncio
    from unittest.mock import patch as _patch, AsyncMock as _AsyncMock
    _df.write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["4242"]}))

    class _FakeDM(discord.DMChannel):
        def __init__(self):
            self.id = 999
            self.sent: list = []
        async def send(self, t):
            self.sent.append(t)

    class _FakeAuthor:
        id = 4242
        bot = False
        def __str__(self):
            return "owneruser#1"

    class _FakeMsg:
        def __init__(self, ch, au):
            self.channel = ch
            self.author = au
            self.content = "hello"
            self.mentions: list = []
            self.role_mentions: list = []
            self.embeds: list = []
            self.attachments: list = []
            self.type = discord.MessageType.default
            self.reference = None
            self.id = 555
            self.message_snapshots: list = []

    _woa_calls: list = []
    _dc_tasks_tmp = Path(tempfile.mkdtemp(prefix="dc-tasks-"))
    _orig_dc_tasks = getattr(dmod, "TASKS_DIR", None)
    dmod.TASKS_DIR = _dc_tasks_tmp  # never write into the real workspace tasks/
    _fake_client = type("_C", (), {"user": object()})()
    with _patch.object(dmod, "client", _fake_client), \
         _patch.object(dmod, "_observe_for_mod", _AsyncMock()), \
         _patch.object(dmod, "_update_dm_checkpoint", lambda *a, **k: None), \
         _patch.object(dmod, "write_owner_activity", lambda *a, **k: _woa_calls.append(1)):
        try:
            _asyncio.run(dmod._handle_discord_message(_FakeMsg(_FakeDM(), _FakeAuthor())))
        except Exception:
            # The tier-resolution block (under test) runs before any later
            # task-write step; a downstream error there does not undo its coverage.
            pass
    if _orig_dc_tasks is not None:
        dmod.TASKS_DIR = _orig_dc_tasks
    _seeded2 = json.loads(_df.read_text()).get("tierMap", {})
    check("discord: allowlisted DM sender resolved owner via tier-map seed",
          _seeded2.get("4242") == "owner" and _woa_calls == [1],
          f"seed={_seeded2} woa={_woa_calls}")
else:
    print("  SKIP discord — discord.py not importable")

print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — allowlist default-readonly (grandfather migration) tests")
