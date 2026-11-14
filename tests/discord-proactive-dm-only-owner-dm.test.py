#!/usr/bin/env python3
"""Discord: a proactive result goes to the owner's DM; [dm-only] keeps it there over a [channel:].

Drives the real `poll_proactive` coroutine with a fake client (the owner's DM and a shared
channel) and records the channel every text is delivered to. No network.

  a) a plain proactive body                          -> delivered to the owner DM
  b) [dm-only] + [channel: <a Discord channel>]      -> delivered to the owner DM, not the channel
  c) control: [channel: <a Discord channel>] alone   -> delivered to that channel (the redirect is seen)
"""
import asyncio
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent

# Isolate BEFORE any bridge import: channel_access_path() falls back to the real home.
prior = {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "SUTANDO_DM_OWNER_ID")}
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-dc-dmonly-")
OWNER, OWNER_DM, SHARED = "111111111111111111", 222222222222222222, 333333333333333333
os.environ["SUTANDO_DM_OWNER_ID"] = OWNER
_cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n")
(_cfg / "access.json").write_text('{"allowFrom": ["%s"], "groups": {}}\n' % OWNER)
tmp = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])
BRIDGE = REPO / "src" / "discord-bridge.py"
fail = 0


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


def _stub_discord():
    """The sibling suite's full stub: a partial one fails at import in unrelated ways."""
    sib = REPO / "tests" / "discord-bridge-collaborator-tier.test.py"
    spec = importlib.util.spec_from_loader("sib_stub", loader=None)
    m = importlib.util.module_from_spec(spec)
    m.__file__ = str(sib)
    exec(compile(sib.read_text(), str(sib), "exec"), m.__dict__)
    m._install_discord_stub()


class _Channel:
    def __init__(self, cid):
        self.id, self.name = cid, f"c{cid}"

    async def send(self, *a, **k):
        return None


class _User:
    name = "owner"

    async def create_dm(self):
        return _Channel(OWNER_DM)


class _Client:
    def get_channel(self, cid):
        return _Channel(cid)

    async def fetch_channel(self, cid):
        return _Channel(cid)

    async def fetch_user(self, uid):
        return _User()


try:
    _stub_discord()
    spec = importlib.util.spec_from_loader("bridge_dmonly", loader=None)
    bridge = importlib.util.module_from_spec(spec)
    bridge.__file__ = str(BRIDGE)
    exec(compile(BRIDGE.read_text(), str(BRIDGE), "exec"), bridge.__dict__)

    results, state = tmp / "results", tmp / "state"
    for d in (results, state, results / "undelivered"):
        d.mkdir(parents=True, exist_ok=True)
    bridge.RESULTS_DIR, bridge.STATE_DIR = results, state
    bridge.client = _Client()
    delivered = []

    def _deliver_text(provider, channel_id, text, item_id, chunker):
        delivered.append(int(channel_id))
        return 1

    bridge.discord_proactive_send.deliver_text = _deliver_text

    def deliver(name, body):
        before = len(delivered)
        (results / name).write_text(body)

        async def one_pass():
            try:
                await asyncio.wait_for(bridge.poll_proactive(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass

        asyncio.run(one_pass())
        return delivered[before:]

    got = deliver("proactive-dc-plain.txt", "a plain owner update\n")
    check(got == [OWNER_DM], f"a) a plain proactive body goes to the owner DM (got {got})")
    got = deliver("proactive-dc-dmonly.txt", f"[dm-only]\n[channel: {SHARED}]\nprivate detail\n")
    check(got == [OWNER_DM], f"b) [dm-only] keeps it in the owner DM over a [channel:] (got {got})")
    got = deliver("proactive-dc-redirect.txt", f"[channel: {SHARED}]\nfor the channel\n")
    check(got == [SHARED], f"c) control: without [dm-only] the redirect is taken (got {got})")
finally:
    for k, v in prior.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)

if fail:
    print("FAIL: discord proactive owner DM")
    sys.exit(1)
print("PASS: discord proactive results go to the owner DM; [dm-only] overrides a [channel:]")
