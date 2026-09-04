#!/usr/bin/env python3
"""The pairing code is an approval credential, so it must never reach a shared
channel on ANY branch — reachable owner, fresh install, or unreachable owner."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent

# Importing the bridge without discord.py triggers its rescue re-exec, which
# would exec the BRIDGE as __main__ and collide with a live singleton lock.
try:
    import discord  # noqa: F401
except ImportError:
    for _cand in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(_cand) and os.path.realpath(_cand) != os.path.realpath(sys.executable):
            import subprocess
            if subprocess.run([_cand, "-c", "import discord"], capture_output=True).returncode == 0:
                os.execv(_cand, [_cand, os.path.abspath(__file__), *sys.argv[1:]])
    print("SKIP — discord.py not importable under any known interpreter")
    sys.exit(0)

# Isolate module-global paths BEFORE import — the bridge derives ACCESS_FILE
# from CLAUDE_CONFIG_DIR at import time (never touch the real access.json).
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="sutando-pair-test-")
# The bridge exit(1)s at import when no token resolves; a fake one is fine —
# nothing connects until client.run(), which tests never call.
os.environ.setdefault("DISCORD_BOT_TOKEN", "faketoken-for-tests")

spec = importlib.util.spec_from_file_location("discordbridge_pair", REPO / "src" / "discord-bridge.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


# ACCESS_BACKUP_FILE resolves under the LIVE workspace at import, so the temp tree
# above is not isolation: _backup_access_to_disk() overwrites the real durable file.
mod.ACCESS_BACKUP_FILE = Path(tempfile.mkdtemp(prefix="acl-bk-")) / "discord-access-backup.json"

# CLAUDE_CONFIG_DIR alone is not isolation: channel_access_path() falls back to
# the real legacy access.json when the fresh canonical path does not exist.
mod.ACCESS_FILE = Path(tempfile.mkdtemp(prefix="pair-acl-")) / "channels" / "discord" / "access.json"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


class FakeMessageable:
    def __init__(self, name=None):
        if name is not None:
            self.name = name
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)


class FakeDMMessageable(discord.DMChannel):
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)


class FakeClient:
    def __init__(self, owner):
        self.owner = owner

    async def fetch_user(self, uid):
        if self.owner is None:
            raise RuntimeError("user not found")
        return self.owner


CODE = "zzz999"

# ── Case 1: owner reachable → code in DM only, channel stays generic ─────────
owner = FakeMessageable()
channel = FakeMessageable(name="review-preflight")
with patch.object(mod, "client", FakeClient(owner)):
    route = asyncio.run(
        mod._deliver_pairing_prompt(channel, CODE, "newuser", "42", {"777"})
    )
check("route reported as dm", route == "dm")
check("owner DM contains the pairing code", any(CODE in m for m in owner.sent))
check("owner DM names the requester and channel",
      any("newuser" in m and "review-preflight" in m for m in owner.sent))
check("channel got exactly one message", len(channel.sent) == 1)
check("channel message does NOT contain the code",
      all(CODE not in m for m in channel.sent), f"leaked: {channel.sent}")

# ── Case 1b: BEFORE/AFTER channel-leak contrast ──────────────────────────────
# Reconstructs the pre-fix call so the leak is demonstrated, not just asserted.
_before_ch = FakeMessageable(name="review-preflight")
asyncio.run(_before_ch.send(f"Pairing required. Ask the owner to run:\n`/discord:access pair {CODE}`"))
before_leaked = any(CODE in m for m in _before_ch.sent)
# AFTER: same event, owner reachable → channel post generic, code only in DM.
after_channel_leaked = any(CODE in m for m in channel.sent)
after_dm_has_code = any(CODE in m for m in owner.sent)
check("BEFORE-fix: the in-channel pairing post leaked the code to the channel",
      before_leaked, f"{_before_ch.sent}")
check("AFTER-fix: the channel post no longer carries the code",
      not after_channel_leaked, f"{channel.sent}")
check("AFTER-fix: the code is delivered only via the owner DM",
      after_dm_has_code and not after_channel_leaked)

# ── Case 2: owner unreachable → fail-SAFE fallback, code NOT leaked ──────────
# The fallback must not recreate the leak: channel notice stays code-free.
channel2 = FakeMessageable(name="review-preflight")
with patch.object(mod, "client", FakeClient(None)):
    route2 = asyncio.run(
        mod._deliver_pairing_prompt(channel2, CODE, "newuser", "42", {"777"})
    )
check("route reported as channel fallback", route2 == "channel")
check("fallback still notifies the channel (one message)", len(channel2.sent) == 1)
check("fallback channel message does NOT contain the code (no leak in the fallback)",
      all(CODE not in m for m in channel2.sent), f"leaked: {channel2.sent}")

# ── Case 3: empty allowFrom + private DM → first owner can self-pair ──────────
channel3 = FakeDMMessageable()
with patch.object(mod, "client", FakeClient(owner)):
    route3 = asyncio.run(
        mod._deliver_pairing_prompt(channel3, CODE, "newuser", "42", set())
    )
check("empty allowFrom in a DM returns the code privately", route3 == "dm")
check("fresh-install DM contains the pairing code",
      any(CODE in m for m in channel3.sent), f"{channel3.sent}")

# ── Case 4: on_message pairing branch drives _deliver_pairing_prompt ──────────
# Covers the changed CALL SITE; delivery itself is stubbed and tested above.
import json as _json
from unittest.mock import AsyncMock

class _FakeDM(discord.DMChannel):  # isinstance(_, discord.DMChannel) must be True
    def __init__(self, cid=999):
        self.id = cid
        self.sent: list[str] = []
    async def send(self, text):
        self.sent.append(text)

class _FakeAuthor:
    def __init__(self, uid=424242):
        self.id = uid
        self.bot = False
    def __str__(self):
        return "pairme#0001"

class _FakeMsg:
    def __init__(self, channel, author):
        self.channel = channel
        self.author = author
        self.content = "hello"
        self.mentions: list = []
        self.role_mentions: list = []
        self.embeds: list = []
        self.type = discord.MessageType.default
        self.reference = None
        self.id = 555
        self.message_snapshots: list = []

# Empty allowFrom leaves the sender unpaired, so the pairing branch fires. The
# nested parent must exist first: both this seed and the code under test write it.
mod.ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
mod.ACCESS_FILE.write_text(_json.dumps({"dmPolicy": "pairing", "allowFrom": [], "pending": {}}))

_fake_client = type("_C", (), {"user": object()})()
_dm = _FakeDM()
_msg = _FakeMsg(_dm, _FakeAuthor())
_deliver = AsyncMock(return_value="dm")
with patch.object(mod, "client", _fake_client), \
     patch.object(mod, "_deliver_pairing_prompt", _deliver), \
     patch.object(mod, "_observe_for_mod", AsyncMock()), \
     patch.object(mod, "_update_dm_checkpoint", lambda *a, **k: None):
    asyncio.run(mod._handle_discord_message(_msg))
check("on_message pairing branch invoked _deliver_pairing_prompt once",
      _deliver.await_count == 1)
check("pairing branch passed the DM channel + generated code to delivery",
      _deliver.await_count == 1 and _deliver.await_args.args[0] is _dm
      and len(_deliver.await_args.args[1]) == 6)
check("pairing code persisted to access.pending",
      len(_json.loads(mod.ACCESS_FILE.read_text()).get("pending", {})) == 1)

print()
if failures:
    print(f"FAIL — {len(failures)} assertion(s): {failures}")
    sys.exit(1)
print("PASS — pairing-code DM routing behavioral tests")
