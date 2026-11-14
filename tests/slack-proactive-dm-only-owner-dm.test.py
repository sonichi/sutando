#!/usr/bin/env python3
"""Slack: a proactive result goes to the owner's DM; [dm-only] keeps it there over a [channel:].

Drives the real `result_watcher` loop and the real `_send_reply`, with a fake Slack client that
records where every message is posted. No network.

  a) a plain proactive body                         -> posted in the owner DM
  b) [dm-only] + [channel: <a Slack channel>]       -> posted in the owner DM, not the channel
  c) control: [channel: <a Slack channel>] alone    -> posted in that channel (the redirect is seen)
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# MODULE level, before any exec_module: the bridge resolves ACCESS_FILE during import.
_CCD = tempfile.mkdtemp(prefix="sutando-slack-dmonly-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CCD
(Path(_CCD) / "channels" / "slack").mkdir(parents=True, exist_ok=True)
(Path(_CCD) / "channels" / "slack" / "access.json").write_text('{"allowFrom": []}')

OWNER_DM = "D-OWNER"
SHARED = "C0SHAREDROOM"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" {detail}"))
    if not cond:
        FAILURES.append(name)


class _RecordingClient:
    def __init__(self):
        self.posts: list[dict] = []

    def conversations_open(self, **kwargs):
        return {"channel": {"id": OWNER_DM}}

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1.0"}

    def __getattr__(self, name):  # any other API call is answered, never sent anywhere
        return lambda **kwargs: {"ok": True}


class _FakeApp:
    def __init__(self, token=None):
        self.client = _RecordingClient()

    def _decorator(self, *args, **kwargs):
        return lambda fn: fn

    event = message = command = action = shortcut = view = _decorator


def _load_bridge():
    bolt = types.ModuleType("slack_bolt")
    bolt.App = _FakeApp
    sys.modules["slack_bolt"] = bolt
    sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
    socket = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket.SocketModeHandler = type("SocketModeHandler", (), {})
    sys.modules["slack_bolt.adapter.socket_mode"] = socket
    os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="sutando-slack-dmonly-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
    spec = importlib.util.spec_from_file_location("slackbridge_dmonly_test", REPO / "src" / "slack-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _settle(predicate, timeout=6.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def main() -> int:
    bridge = _load_bridge()
    results = Path(bridge.RESULTS_DIR)
    access = Path(os.environ["SUTANDO_WORKSPACE"]) / "access.json"
    access.write_text(json.dumps({"allowFrom": ["owner-1"], "tierMap": {"owner-1": "owner"},
                                  "tofuOwner": "owner-1"}))
    bridge.ACCESS_FILE = access
    posts = bridge.app.client.posts
    threading.Thread(target=bridge.result_watcher, daemon=True).start()

    def deliver(name: str, body: str) -> list:
        before = len(posts)
        (results / name).write_text(body)
        _settle(lambda: not (results / name).exists() and len(posts) > before)
        return [p.get("channel") for p in posts[before:]]

    got = deliver("proactive-slack-plain.txt", "a plain owner update\n")
    check("a) a plain proactive body is posted in the owner DM", got == [OWNER_DM], f"got {got}")
    got = deliver("proactive-slack-dmonly.txt", f"[dm-only]\n[channel: {SHARED}]\nprivate detail\n")
    check("b) [dm-only] keeps it in the owner DM over a [channel:]", got == [OWNER_DM], f"got {got}")
    check("b) the marker text is not posted", all("[dm-only]" not in (p.get("text") or "") for p in posts))
    got = deliver("proactive-slack-redirect.txt", f"[channel: {SHARED}]\nfor the channel\n")
    check("c) control: without [dm-only] the redirect is taken", got == [SHARED], f"got {got}")

    print()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s)")
        return 1
    print("PASS: slack proactive results go to the owner DM; [dm-only] overrides a [channel:]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
