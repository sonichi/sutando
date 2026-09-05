#!/usr/bin/env python3
"""A `.to-slack` proactive file must not be re-routed by a FOREIGN body redirect.

The claim gate already applies filename-over-body (`proactive_body_guard`), but
the decision has to survive into the SEND SINK. Before this test, `_send_reply`
re-parsed the marker and unconditionally replaced the resolved owner DM with the
foreign id, so the file was posted to a Discord snowflake, refused, released,
and retried by Slack once per poll forever.

Drives the real `result_watcher()` and the real `_send_reply()`; only the Slack
SDK client is stubbed, and the stub refuses any target outside Slack's own
`[CDG]...` channel grammar — the same thing the live API does with
`channel_not_found`.

Run: python3 tests/slack-proactive-destined-redirect-sink.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# MODULE level, before any exec_module: the bridge resolves ACCESS_FILE during
# import and falls back to the real ~/.claude when CLAUDE_CONFIG_DIR is unset.
_CCD = tempfile.mkdtemp(prefix="sutando-slack-destined-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CCD
_cfg_slack = Path(_CCD) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text('{"allowFrom": []}')

# Slack's own address grammar. Anything else is not a Slack channel, and the
# live API answers `channel_not_found` — the stub below does the same.
SLACK_ID_RE = re.compile(r"[CDG][A-Z0-9]{6,}\Z")

DISCORD_ID = "1535008729106485288"
OWNER_DM = "DOWNER1"
SLACK_ROOM = "C0SLACKROOM"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


class _SlackApiRefusal(Exception):
    """What the live client raises for a target that is not a Slack channel."""


class _RecordingClient:
    """Stubs ONLY the SDK boundary. Everything above it is the shipped code."""

    def __init__(self):
        self.attempted_channels: list[str] = []
        self.posted: list[dict] = []
        self.uploads: list[dict] = []

    def conversations_open(self, **kwargs):
        return {"channel": {"id": OWNER_DM}}

    def chat_postMessage(self, **kwargs):
        channel = str(kwargs.get("channel", ""))
        self.attempted_channels.append(channel)
        if not SLACK_ID_RE.fullmatch(channel):
            raise _SlackApiRefusal("channel_not_found")
        self.posted.append(kwargs)
        return {"ok": True}

    def files_upload_v2(self, **kwargs):
        channel = str(kwargs.get("channel", ""))
        self.attempted_channels.append(channel)
        if not SLACK_ID_RE.fullmatch(channel):
            raise _SlackApiRefusal("channel_not_found")
        self.uploads.append(kwargs)
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

    os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="sutando-slack-destined-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
    spec = importlib.util.spec_from_file_location(
        "slackbridge_destined_redirect_test", REPO / "src" / "slack-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _settle(predicate, timeout=6.0, interval=0.1):
    """Poll until predicate holds; the watcher runs on its own cadence."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def main() -> int:
    bridge = _load_bridge()
    client = bridge.app.client

    results = Path(bridge.RESULTS_DIR)
    check("harness is sandboxed (results dir is a temp path)",
          str(results.resolve()).startswith(str(Path(tempfile.gettempdir()).resolve()))
          or "/var/folders/" in str(results.resolve()) or "/T/" in str(results.resolve()),
          f"results dir resolved to {results}")

    access = Path(os.environ["SUTANDO_WORKSPACE"]) / "access.json"
    access.write_text(json.dumps({"allowFrom": ["owner-1"],
                                  "tierMap": {"owner-1": "owner"},
                                  "tofuOwner": "owner-1"}))
    bridge.ACCESS_FILE = access

    # The allowlist is a DIFFERENT policy than the one under test; pin it open
    # for one probe path so the attachment leg reaches the SDK boundary.
    probe = Path(os.environ["SUTANDO_WORKSPACE"]) / "attach-probe.txt"
    probe.write_text("probe")
    _real_sendable = bridge._is_path_sendable
    bridge._is_path_sendable = (
        lambda p: True if p == str(probe) else _real_sendable(p))

    watcher = threading.Thread(target=bridge.result_watcher, daemon=True)
    watcher.start()

    # ---------------------------------------------------------------- case 1
    # The conflict: FILENAME says slack, BODY redirects to Discord.
    conflict = results / "proactive-1.to-slack.txt"
    conflict.write_text(f"[channel: {DISCORD_ID}]\nowner notification body")

    check("the destined file is consumed (delivered, not retried forever)",
          _settle(lambda: not conflict.exists()
                  and not (results / "proactive-1.to-slack.sending").exists()),
          "still on disk — released and re-claimed once per poll")

    check("no send was ever attempted against the foreign Discord id",
          DISCORD_ID not in client.attempted_channels,
          f"attempted_channels= {client.attempted_channels}")

    check("its body reached the owner DM the filename destined it to",
          any(p["channel"] == OWNER_DM and "owner notification body" in p["text"]
              for p in client.posted),
          f"posted= {[(p['channel'], p['text'][:40]) for p in client.posted]}")

    # ---------------------------------------------------------------- case 2
    # Positive control A: the fix cannot pass by refusing every redirect.
    plain = results / "proactive-2.txt"
    plain.write_text(f"[channel: {SLACK_ROOM}]\nredirect me please")

    check("an undestined body still follows its [channel:] redirect",
          _settle(lambda: any(p["channel"] == SLACK_ROOM
                              and "redirect me please" in p["text"]
                              for p in client.posted)),
          f"posted= {[(p['channel'], p['text'][:40]) for p in client.posted]}")

    # ---------------------------------------------------------------- case 3
    # Positive control B: suppression is FOREIGN-only; this is a ROOM selection.
    same_bridge = results / "proactive-3.to-slack.txt"
    same_bridge.write_text(f"[channel: {SLACK_ROOM}]\nsame-bridge room selection")

    check("a .to-slack file still follows a SLACK-addressed redirect",
          _settle(lambda: any(p["channel"] == SLACK_ROOM
                              and "same-bridge room selection" in p["text"]
                              for p in client.posted)),
          f"posted= {[(p['channel'], p['text'][:40]) for p in client.posted]}")

    # ---------------------------------------------------------------- case 4
    # Attachment actions survive suppression and ride the corrected channel.
    attach = results / "proactive-4.to-slack.txt"
    attach.write_text(f"[channel: {DISCORD_ID}]\nbody with a file\n[file: {probe}]")

    check("the attachment is still uploaded (actions preserved)",
          _settle(lambda: any(u["file"] == str(probe) for u in client.uploads)),
          f"uploads= {client.uploads}")

    check("and it is uploaded to the owner DM, not the foreign id",
          all(u["channel"] == OWNER_DM
              for u in client.uploads if u["file"] == str(probe)),
          f"uploads= {[(u['channel'], u['file']) for u in client.uploads]}")

    print()
    print(f"attempted_channels= {client.attempted_channels}")
    print(f"conflict_retained= {conflict.exists()}")
    print(f"plain_control_consumed= {not plain.exists()}")
    print()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("PASS: filename-over-body survives into Slack's send sink; "
          "same-bridge redirects and attachments are preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
