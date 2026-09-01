#!/usr/bin/env python3
"""Regression tests for recreated proactive-result delivery IDs."""

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
spec = importlib.util.spec_from_file_location(
    "slack_proactive_receipts",
    REPO / "src" / "slack_proactive_receipts.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class _RecordingClient:
    def __init__(self):
        self.calls = []

    def conversations_open(self, **kwargs):
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

    os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="sutando-slack-dedupe-bridge-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
    bridge_spec = importlib.util.spec_from_file_location(
        "slackbridge_dedupe_test",
        REPO / "src" / "slack-bridge.py",
    )
    bridge = importlib.util.module_from_spec(bridge_spec)
    bridge_spec.loader.exec_module(bridge)
    return bridge


def main():
    state = Path(tempfile.mkdtemp(prefix="sutando-slack-proactive-receipt-"))
    delivery_id = "proactive-daily-top-ai-news-1784638800.txt"

    assert not module.was_delivered(state, delivery_id)
    module.mark_delivered(state, delivery_id)
    assert module.was_delivered(state, delivery_id)

    # Recreating the same deterministic filename must remain a duplicate even
    # if its content changes. A genuinely new schedule slot gets a new ID.
    assert module.was_delivered(state, delivery_id)
    assert not module.was_delivered(
        state,
        "proactive-daily-top-ai-news-1784725200.txt",
    )

    # Content-keyed producers intentionally reuse a filename on a slower
    # cadence (pending questions re-notify hourly). The short race receipt must
    # expire so those later deliveries are not suppressed forever.
    content_keyed = "proactive-pending-q-deadbeef.txt"
    module.mark_delivered(state, content_keyed)
    content_receipt = module._receipt_path(state, content_keyed)
    expired = time.time() - module.RECEIPT_TTL_SECONDS - 1
    os.utime(content_receipt, (expired, expired))
    assert not module.was_delivered(state, content_keyed)
    assert not content_receipt.exists()

    # Marking a new delivery also prunes unrelated expired receipts, bounding
    # the sentinel directory instead of accumulating one file per ID forever.
    stale_id = "proactive-old-slot.txt"
    module.mark_delivered(state, stale_id)
    stale_receipt = module._receipt_path(state, stale_id)
    os.utime(stale_receipt, (expired, expired))
    module.mark_delivered(state, "proactive-current-slot.txt")
    assert not stale_receipt.exists()

    # Receipt paths are hashes, so a malformed filename cannot escape state/.
    hostile = "../../outside.txt"
    module.mark_delivered(state, hostile)
    assert module.was_delivered(state, hostile)
    assert not (state.parent / "outside.txt").exists()

    # Receipt I/O is deliberately best-effort and must never break delivery.
    original_receipt_path = module._receipt_path
    module._receipt_path = lambda *_args: (_ for _ in ()).throw(OSError("read-only"))
    assert module.was_delivered(state, "unreadable") is False
    module.mark_delivered(state, "unwritable")
    module._receipt_path = original_receipt_path

    # Exercise the actual watcher twice with one deterministic filename.
    bridge = _load_bridge()
    access_file = Path(os.environ["SUTANDO_WORKSPACE"]) / "access.json"
    bridge.ACCESS_FILE = access_file
    access_file.write_text(json.dumps({"allowFrom": ["owner-id"]}))
    proactive = bridge.RESULTS_DIR / "proactive-same-slot.txt"
    proactive.write_text("first body")
    watcher = threading.Thread(target=bridge.result_watcher, daemon=True)
    watcher.start()
    time.sleep(0.3)
    assert len(bridge.app.client.calls) == 1
    proactive.write_text("recreated body")
    time.sleep(1.2)
    assert len(bridge.app.client.calls) == 1
    assert not proactive.exists()
    audit = bridge.STATE_DIR / "result-audit.log"
    assert "\tproactive-same-slot.txt\tdeduped\tslack" in audit.read_text()

    bridge = (REPO / "src" / "slack-bridge.py").read_text()
    check_pos = bridge.index("proactive_was_delivered(STATE_DIR, delivery_id)")
    claim_pos = bridge.index("claim_for_delivery(f, owner_id)", check_pos)
    send_pos = bridge.index("_send_reply(dm_channel", claim_pos)
    mark_pos = bridge.index("mark_proactive_delivered(STATE_DIR, delivery_id)", send_pos)
    assert check_pos < claim_pos < send_pos < mark_pos

    print("PASS: recreated Slack proactive delivery IDs are suppressed within the race window")


if __name__ == "__main__":
    main()
