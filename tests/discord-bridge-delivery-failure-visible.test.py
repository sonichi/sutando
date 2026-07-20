#!/usr/bin/env python3
"""A Discord attachment failure must be visible, audited, and observable.

Regression: ``poll_results`` sent the text portion first, then Discord rejected
an oversized attachment with HTTP 413.  The broad exception handler printed to
the bridge console, archived the task/result, and sent no failure signal.  The
owner saw success-looking text with no attachment and the agent had no signal
to correct it.

Run: python3 tests/discord-bridge-delivery-failure-visible.test.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = Path(tempfile.mkdtemp(prefix="sutando-discord-failure-test-"))
os.environ["SUTANDO_WORKSPACE"] = str(WORKSPACE)
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
        "discord_bridge_delivery_failure_test", REPO / "src" / "discord-bridge.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


class _RejectingDM(_DMChannel):
    def __init__(self):
        self.id = 1526767280925577328
        self.recipient = types.SimpleNamespace(name="owner")
        self.sent_text = []
        self.file_attempts = 0

    async def send(self, content=None, *, reference=None, file=None, **_kwargs):
        if file is not None:
            self.file_attempts += 1
            raise RuntimeError("413 Payload Too Large")
        self.sent_text.append(content)
        return types.SimpleNamespace(id=123)


async def _exercise():
    task_id = "task-1784556770535"
    channel = _RejectingDM()
    attachment = bridge.RESULTS_DIR / "oversized.mp4"
    attachment.parent.mkdir(parents=True, exist_ok=True)
    attachment.write_bytes(b"video")
    bridge.TASKS_DIR.mkdir(parents=True, exist_ok=True)
    (bridge.TASKS_DIR / f"{task_id}.txt").write_text(
        f"id: {task_id}\nsource: discord\naccess_tier: owner\n"
    )
    (bridge.RESULTS_DIR / f"{task_id}.txt").write_text(
        f"Your video is ready.\n[file: {attachment}]\n"
    )
    bridge.pending_replies.clear()
    bridge.pending_replies[task_id] = channel
    bridge.pending_task_tiers.clear()
    bridge.pending_task_tiers[task_id] = "owner"
    bridge.pending_reply_anchors.clear()
    bridge.save_pending_replies = lambda: None
    events = []
    bridge._emit_channel = lambda *args, **kwargs: events.append((args, kwargs))

    try:
        await asyncio.wait_for(bridge.poll_results(), timeout=0.25)
    except asyncio.TimeoutError:
        pass
    return task_id, channel, events


def main():
    task_id, channel, events = asyncio.run(_exercise())
    assert channel.file_attempts == 1, "the attachment failure was not exercised"
    notices = [text for text in channel.sent_text if text and "Result delivery failed" in text]
    assert notices, "attachment failure was silent — no owner-visible failure notice"
    assert task_id in notices[0] and "413 Payload Too Large" in notices[0]

    audit = WORKSPACE / "state" / "result-audit.log"
    assert audit.exists(), "failed delivery did not reach the audit ledger"
    assert f"\t{task_id}\tfailed\tdiscord" in audit.read_text()
    assert not any(f"\t{task_id}\tdelivered\tdiscord" in line for line in audit.read_text().splitlines())

    error_events = [kwargs for _args, kwargs in events if kwargs.get("outcome") == "error"]
    assert error_events, "failed delivery did not emit channel.discord.out outcome=error"
    assert error_events[0]["data"]["task_id"] == task_id
    assert "413 Payload Too Large" in error_events[0]["data"]["error"]
    print("PASS — Discord attachment failures are visible, audited, and observable")


if __name__ == "__main__":
    main()
