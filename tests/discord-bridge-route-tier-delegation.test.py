#!/usr/bin/env python3
"""BEHAVIOURAL: Discord's reply leg resolves a missing tier through the shared resolver.

A route whose tier map entry is gone (bridge restart) falls back through
`resolve_route_tier` (src/policy/egress/result.py), the same helper the Slack
bridge uses, so an archived task's tier is honoured instead of reading as guest.

Run: python3 tests/discord-bridge-route-tier-delegation.test.py
"""

from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = Path(tempfile.mkdtemp(prefix="sutando-discord-route-tier-test-"))
os.environ["SUTANDO_WORKSPACE"] = str(WORKSPACE)
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-discord-route-tier-")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["DISCORD_BOT_TOKEN"] = "test-token-not-real"


discord_stub = types.ModuleType("discord")


class _Intents:
    @staticmethod
    def default():
        return types.SimpleNamespace(message_content=False, members=False)


class _Client:
    def __init__(self, *_args, **_kwargs):
        self.user = None
        self.loop = types.SimpleNamespace(create_task=lambda *_a, **_k: None)

    def event(self, fn):
        return fn

    def is_ready(self):
        return True

    def get_channel(self, _channel_id):
        return None


class _DMChannel:
    pass


class _MessageReference:
    def __init__(self, message_id=None, channel_id=None, fail_if_not_exists=True):
        self.message_id = message_id
        self.channel_id = channel_id
        self.fail_if_not_exists = fail_if_not_exists


discord_stub.Intents = _Intents
discord_stub.Client = _Client
discord_stub.DMChannel = _DMChannel
discord_stub.MessageReference = _MessageReference
discord_stub.MessageType = types.SimpleNamespace(default=0, reply=1)
discord_stub.AllowedMentions = type("AllowedMentions", (), {})
discord_stub.File = lambda path: types.SimpleNamespace(path=path)
discord_stub.Object = lambda id: types.SimpleNamespace(id=id)
sys.modules["discord"] = discord_stub


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "discord_bridge_route_tier_test", REPO / "src" / "discord-bridge.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()

ERROR_BODY = ("Recovery failed: [Errno 2] No such file or directory: "
              "'/Users/someone/workspace/tasks/task-1.txt'")


class _CapturingChannel(_DMChannel):
    def __init__(self):
        self.id = 1526767280925577328
        self.recipient = types.SimpleNamespace(name="asker")
        self.sent = []

    async def send(self, content=None, **_kwargs):
        self.sent.append(content)
        return types.SimpleNamespace(id=1)


async def _deliver(task_id: str, archived_tier: str) -> list:
    """One poll pass for a route whose tier map entry was lost; the task is archived."""
    month = bridge.TASKS_DIR / "archive" / "2026-10"
    month.mkdir(parents=True, exist_ok=True)
    (month / f"{task_id}.txt").write_text(
        f"id: {task_id}\nsource: discord\naccess_tier: {archived_tier}\ntask: hi\n")
    bridge.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (bridge.RESULTS_DIR / f"{task_id}.txt").write_text(ERROR_BODY)
    channel = _CapturingChannel()
    bridge.pending_replies.clear()
    bridge.pending_replies[task_id] = channel
    bridge.pending_task_tiers.clear()
    bridge.pending_reply_anchors.clear()
    bridge.save_pending_replies = lambda: None
    bridge._emit_channel = lambda *_a, **_k: None
    try:
        await asyncio.wait_for(bridge.poll_results(), timeout=0.25)
    except asyncio.TimeoutError:
        pass
    return [t for t in channel.sent if t]


def main():
    fails = []

    seen = []
    real = bridge.resolve_route_tier

    def _spy(tier, tasks_dir, task_id):
        seen.append((tier, tasks_dir, task_id))
        return "team"

    bridge.resolve_route_tier = _spy
    try:
        sent = asyncio.run(_deliver("task-1792700000001", "owner"))
    finally:
        bridge.resolve_route_tier = real
    if seen != [("unknown", bridge.TASKS_DIR, "task-1792700000001")]:
        fails.append(f"the reply leg did not delegate the tier fallback: {seen}")
    if any("[Errno" in t for t in sent):
        fails.append("the resolver's tier did not drive the guard")

    # Converged behaviour: an archived owner task is owner, not guest.
    sent = asyncio.run(_deliver("task-1792700000002", "owner"))
    if not any(ERROR_BODY in t for t in sent):
        fails.append(f"an archived owner task was guarded as non-owner: {sent}")
    sent = asyncio.run(_deliver("task-1792700000003", "team"))
    if any("[Errno" in t for t in sent):
        fails.append("an archived team task's internal error reached the channel")

    if fails:
        for f in fails:
            print("FAIL:", f)
        sys.exit(1)
    print("PASS — Discord reply leg delegates the tier fallback to resolve_route_tier")


if __name__ == "__main__":
    main()
