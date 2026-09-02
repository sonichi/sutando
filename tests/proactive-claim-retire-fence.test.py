#!/usr/bin/env python3
"""A consumer must not retire a claimed inode a producer is still writing.

The claim is a hard link, so a producer holding the original fd keeps appending
to THIS inode after the bridge's post-claim `read_ready_result`. The bridge then
sent the snapshot and unlinked, destroying every byte that arrived in between —
never guarded, never delivered, unrecoverable.

The append is injected at the SDK boundary, which the shipped code calls after
the read and before the retire, so it lands in the real window with no threads
and no sleeps deciding the outcome.

Drives the real `result_watcher()`; only the Slack SDK client is stubbed.
Case 2 is the mutation control: it re-runs the identical scenario with the fence
neutralised and requires the loss to reappear, so a green case 1 cannot be an
artifact of the harness.

Run: python3 tests/proactive-claim-retire-fence.test.py
Exit: 0 = all pass, 1 = failure
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
_CCD = tempfile.mkdtemp(prefix="sutando-retire-fence-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CCD
_cfg_slack = Path(_CCD) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text('{"allowFrom": []}')

OWNER_DM = "DOWNER1"
FIRST = "first half of the answer"
APPENDED = "SECOND-HALF-ARRIVED-LATE"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


class _RecordingClient:
    """Stubs ONLY the SDK boundary. Everything above it is the shipped code."""

    def __init__(self):
        self.posted: list[dict] = []
        self.append_target: Path | None = None
        self._appended = False

    def conversations_open(self, **kwargs):
        return {"channel": {"id": OWNER_DM}}

    def chat_postMessage(self, **kwargs):
        # The shipped code reaches here AFTER read_ready_result and BEFORE the
        # retire — exactly the window a still-writing producer appends in.
        if (not self._appended and self.append_target is not None
                and self.append_target.exists()):
            with open(self.append_target, "a") as fh:
                fh.write(f"\n{APPENDED}\n")
            self._appended = True
        self.posted.append(kwargs)
        return {"ok": True}

    def files_upload_v2(self, **kwargs):
        self.posted.append(kwargs)
        return {"ok": True}


class _FakeApp:
    def __init__(self, token=None):
        self.client = _RecordingClient()

    def _decorator(self, *args, **kwargs):
        return lambda fn: fn

    event = message = command = action = shortcut = view = _decorator


def _load_bridge(tag: str):
    bolt = types.ModuleType("slack_bolt")
    bolt.App = _FakeApp
    sys.modules["slack_bolt"] = bolt
    sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
    socket = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket.SocketModeHandler = type("SocketModeHandler", (), {})
    sys.modules["slack_bolt.adapter.socket_mode"] = socket

    os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix=f"sutando-retire-{tag}-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
    spec = importlib.util.spec_from_file_location(
        f"slackbridge_retire_fence_{tag}", REPO / "src" / "slack-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    access = Path(os.environ["SUTANDO_WORKSPACE"]) / "access.json"
    access.write_text(json.dumps({"allowFrom": ["owner-1"],
                                  "tierMap": {"owner-1": "owner"},
                                  "tofuOwner": "owner-1"}))
    mod.ACCESS_FILE = access
    return mod


def _settle(predicate, timeout=8.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _run_scenario(tag: str, neutralise_fence: bool):
    """Deliver one proactive file whose producer appends mid-send."""
    bridge = _load_bridge(tag)
    if neutralise_fence:
        # The mutation: retire unconditionally, which is the pre-fix behaviour.
        def _always_retire(claim, delivered):
            Path(claim).unlink(missing_ok=True)
            return True
        bridge.retire_claim_if_unchanged = _always_retire

    client = bridge.app.client
    results = Path(bridge.RESULTS_DIR)
    results.mkdir(parents=True, exist_ok=True)

    name = "proactive-late-append"
    client.append_target = results / f"{name}.sending"

    threading.Thread(target=bridge.result_watcher, daemon=True).start()
    (results / f"{name}.txt").write_text(FIRST)

    delivered_late = _settle(
        lambda: any(APPENDED in str(p.get("text", "")) for p in client.posted))
    settled_gone = _settle(
        lambda: not (results / f"{name}.txt").exists()
        and not (results / f"{name}.sending").exists(), timeout=2.0)
    return client, delivered_late, settled_gone


def main() -> int:
    print("case 1: with the fence, a mid-send append still reaches the owner")
    client, delivered_late, _ = _run_scenario("fenced", neutralise_fence=False)
    check("the first half was delivered",
          any(FIRST in str(p.get("text", "")) for p in client.posted),
          f"posted= {[str(p.get('text'))[:50] for p in client.posted]}")
    check("the late-appended half was NOT destroyed by the retire",
          delivered_late,
          f"posted= {[str(p.get('text'))[:60] for p in client.posted]}")

    print("case 2 (mutation control): fence disabled, the loss must reappear")
    m_client, m_delivered_late, m_gone = _run_scenario("mutated", neutralise_fence=True)
    check("control still delivers the first half (scenario really ran)",
          any(FIRST in str(p.get("text", "")) for p in m_client.posted),
          f"posted= {[str(p.get('text'))[:50] for p in m_client.posted]}")
    check("control retires the inode (the destructive step happened)",
          m_gone, "claim/result file still on disk, so nothing was destroyed")
    check("control LOSES the late-appended half (test detects the defect)",
          not m_delivered_late,
          "appended text was delivered even with the fence disabled — "
          "this test would pass without the fix and proves nothing")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
