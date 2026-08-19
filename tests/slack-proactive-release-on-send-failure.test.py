#!/usr/bin/env python3
"""A proactive Slack send that RAISES must release its claim, not destroy it.

Drives the real `result_watcher` loop, so the failure path actually executes.
"""
from __future__ import annotations

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

# MODULE level, before any exec_module: the bridge resolves ACCESS_FILE during
# import and falls back to the real ~/.claude when CLAUDE_CONFIG_DIR is unset.
_CCD = tempfile.mkdtemp(prefix="sutando-slack-releasefail-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CCD
_cfg_slack = Path(_CCD) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text('{"allowFrom": []}')

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


class _RecordingClient:
    def __init__(self):
        self.calls = []
        self.opened_for = None

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


class _Boom(Exception):
    """What a real Slack refusal looks like to the caller."""


def _load_bridge():
    bolt = types.ModuleType("slack_bolt")
    bolt.App = _FakeApp
    sys.modules["slack_bolt"] = bolt
    sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
    socket = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket.SocketModeHandler = type("SocketModeHandler", (), {})
    sys.modules["slack_bolt.adapter.socket_mode"] = socket

    os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="sutando-slack-releasefail-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
    spec = importlib.util.spec_from_file_location(
        "slackbridge_releasefail_test", REPO / "src" / "slack-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_either(*paths):
    """Return the body from whichever name currently exists.
    exists()-then-read races the watcher's rename between the two calls."""
    for _ in range(100):
        for p in paths:
            try:
                return p.read_text()
            except FileNotFoundError:
                continue
        time.sleep(0.02)
    return None


def _settle(predicate, timeout=4.0, interval=0.1):
    """Poll until predicate holds; the watcher runs on its own cadence."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def main() -> int:
    bridge = _load_bridge()

    # The watcher must never touch the operator's real results/ directory.
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

    # PHASE 1 — every send raises. The message must survive in some form.
    raised = {"n": 0}

    def _raises(*_a, **_k):
        raised["n"] += 1
        raise _Boom("slack refused: not_in_channel")

    bridge._send_reply = _raises

    name = "proactive-release-on-send-failure.txt"
    body = "a proactive message slack cannot deliver"
    (results / name).write_text(body)

    watcher = threading.Thread(target=bridge.result_watcher, daemon=True)
    watcher.start()

    check("the failing send was actually attempted",
          _settle(lambda: raised["n"] > 0), "watcher never reached the send")

    txt = results / name
    sending = results / name.replace(".txt", ".sending")
    # It oscillates .txt -> .sending -> .txt while retrying; the defect is
    # DESTRUCTION, so assert the body exists under either name.
    survived = _settle(lambda: txt.exists() or sending.exists())
    check("a refused proactive DM is NOT destroyed", survived,
          "both the claim and the released file are gone — message lost")

    if survived:
        # The watcher oscillates .txt/.sending, so exists()-then-read is a TOCTOU.
        got = _read_either(txt, sending)
        check("its body is intact", got == body, f"body became {str(got)[:40]!r}")

    check("nothing was delivered on the failure path",
          len(bridge.app.client.calls) == 0,
          f"{len(bridge.app.client.calls)} send(s) recorded")

    check("it returns to the .txt stream that pollers scan",
          _settle(lambda: txt.exists()),
          "left stranded as .sending — invisible until the next restart sweep")

    # PHASE 1b — REFUSED WITHOUT RAISING. This is the ordinary Slack failure and
    # the `except` above never sees it; only the delivery result does.
    refused = {"n": 0}

    def _returns_false(*_a, **_k):
        refused["n"] += 1
        return False

    bridge._send_reply = _returns_false
    name2 = "proactive-slack-refused-not-raised.txt"
    body2 = "a proactive message slack refuses without raising"
    (results / name2).write_text(body2)
    txt2 = results / name2
    sending2 = results / name2.replace(".txt", ".sending")

    check("the non-raising refusal was attempted",
          _settle(lambda: refused["n"] > 0), "drain never called the refusing send")
    check("a NON-RAISING refusal does not consume the claim",
          _settle(lambda: txt2.exists() or sending2.exists()),
          "consumed on the ordinary refusal — the except never fires for it")
    check("it too returns to the .txt stream",
          _settle(lambda: txt2.exists()), "left stranded as .sending")

    # PHASE 2 — positive control. Without this, an unconditional "always
    # release" would pass every assertion above.
    delivered = {"n": 0}

    def _ok(*_a, **_k):
        delivered["n"] += 1
        return True

    bridge._send_reply = _ok

    check("a successful send delivers", _settle(lambda: delivered["n"] > 0),
          "the retry never succeeded after the send was repaired")
    check("and CONSUMES the file (release is not unconditional)",
          _settle(lambda: not txt.exists() and not sending.exists()),
          "a delivered proactive file was left behind — it would send again")

    print()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("PASS: a refused proactive Slack DM is released and retried, a delivered one is consumed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
