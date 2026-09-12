#!/usr/bin/env python3
"""Regression tests for proactive Slack owner-recipient resolution."""

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
spec = importlib.util.spec_from_file_location("slack_owner", REPO / "src" / "slack_owner.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
resolve = module.resolve_proactive_owner_id


class _RecordingClient:
    def __init__(self):
        self.calls = []

    def conversations_open(self, **kwargs):
        self.opened_for = kwargs["users"]
        return {"channel": {"id": "D-OWNER"}}

    def chat_postMessage(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


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

    os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="sutando-slack-owner-bridge-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
    bridge_spec = importlib.util.spec_from_file_location(
        "slackbridge_owner_test",
        REPO / "src" / "slack-bridge.py",
    )
    bridge = importlib.util.module_from_spec(bridge_spec)
    bridge_spec.loader.exec_module(bridge)
    return bridge


def main():
    current_live_shape = {
        "allowFrom": ["team-first", "rui-owner", "team-second"],
        "tierMap": {
            "team-first": "team",
            "rui-owner": "owner",
            "team-second": "team",
        },
        "tofuOwner": "rui-owner",
    }
    assert resolve(current_live_shape) == "rui-owner"

    # TOFU remains authoritative when multiple owner-tier users exist.
    assert resolve({
        "allowFrom": ["owner-a", "owner-b"],
        "tierMap": {"owner-a": "owner", "owner-b": "owner"},
        "tofuOwner": "owner-b",
    }) == "owner-b"

    # Removed or demoted TOFU owners cannot receive owner notifications.
    assert resolve({
        "allowFrom": ["former-owner", "current-owner"],
        "tierMap": {"former-owner": "team", "current-owner": "owner"},
        "tofuOwner": "former-owner",
    }) == "current-owner"

    # Legacy access files predate tierMap; allowFrom entries default to owner.
    assert resolve({"allowFrom": ["legacy-owner", "second"]}) == "legacy-owner"
    assert resolve({"allowFrom": ["team"], "tierMap": {"team": "team"}}) is None
    assert resolve({"allowFrom": []}) is None

    # A PRESENT tierMap is authoritative. `or {}` collapsed all three of these
    # into the legacy default and elected a deliberately read-only user.
    assert resolve({"allowFrom": ["U_READ_ONLY"], "tierMap": {}}) is None
    assert resolve({"allowFrom": ["U_UNMAPPED"], "tierMap": {"other": "owner"}}) is None
    assert resolve({"allowFrom": ["U"], "tierMap": ["not", "a", "map"]}) is None
    assert resolve({"allowFrom": ["U"], "tierMap": {"U": ["malformed"]}}) is None
    assert resolve({"allowFrom": "not-a-list", "tierMap": {}}) is None
    assert resolve("not-a-dict") is None

    # Mirror controls: without these, a resolver that always returned None
    # would satisfy every assertion above.
    assert resolve({"allowFrom": ["U"], "tierMap": {"U": "owner"}}) == "U"
    assert resolve({"allowFrom": ["a", "b"], "tierMap": {"a": "team", "b": "owner"}}) == "b"

    # Exercise the actual proactive watcher wiring under coverage: it reads
    # access.json, resolves Rui, opens the owner DM, and sends once.
    bridge = _load_bridge()
    access_file = Path(os.environ["SUTANDO_WORKSPACE"]) / "access.json"
    bridge.ACCESS_FILE = access_file
    access_file.write_text(json.dumps(current_live_shape))
    proactive = bridge.RESULTS_DIR / "proactive-owner-resolution-test.txt"
    proactive.write_text("owner-only live-path fixture")
    watcher = threading.Thread(target=bridge.result_watcher, daemon=True)
    watcher.start()
    time.sleep(0.3)
    assert bridge.app.client.opened_for == "rui-owner"
    assert len(bridge.app.client.calls) == 1

    # An unreadable/malformed access file must take the explicit no-owner skip
    # path, never guess a team recipient or crash the watcher.
    access_file.write_text("{not-json")
    (bridge.RESULTS_DIR / "proactive-no-owner-test.txt").write_text("must not send")
    time.sleep(1.2)
    assert len(bridge.app.client.calls) == 1
    print("PASS: proactive Slack notifications resolve only to the configured owner")


if __name__ == "__main__":
    main()
