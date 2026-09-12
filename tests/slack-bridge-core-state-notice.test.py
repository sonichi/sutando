#!/usr/bin/env python3
"""slack-bridge core-state notices (silent-core fix) — the sync binder.

Shared dedup/cooldown/recovery logic is covered in
packages/ag2-sparrow/tests/test_core_state_notice.py. This covers only what the
Slack binder owns: it sweeps the core state on the SLACK ledger (not the
gateway's), sends via app.client.chat_postMessage, and recovers a channel once
the core is healthy.

Run: python3 tests/slack-bridge-core-state-notice.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

_FAILURES: list[str] = []


def expect(cond, label):
    if not cond:
        _FAILURES.append(label)
        print(f"FAIL: {label}", file=sys.stderr)


class _StubApp:
    def __init__(self, *a, **kw):
        self.client = types.SimpleNamespace()

    def event(self, _name):
        return lambda fn: fn


def _load_module():
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
    os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token")
    os.environ.setdefault("SUTANDO_WORKSPACE", tempfile.mkdtemp(prefix="slcsn-ws-"))
    try:
        import slack_bolt as _real_bolt
        _real_bolt.App = _StubApp
    except ImportError:
        stub_bolt = types.ModuleType("slack_bolt")
        stub_bolt.App = _StubApp
        sys.modules["slack_bolt"] = stub_bolt
        adapter_pkg = types.ModuleType("slack_bolt.adapter")
        sys.modules["slack_bolt.adapter"] = adapter_pkg
        sm_mod = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm_mod.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm_mod
    repo = REPO
    spec = importlib.util.spec_from_file_location(
        "slack_bridge_csn", repo / "src" / "slack-bridge.py")
    sys.path.insert(0, str(repo / "src"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_state(state_dir: Path, state, kind=None):
    (state_dir / "core-supervisor.json").write_text(json.dumps(
        {"state": state, "detail": "t", "prompt": None, "kind": kind}))


def main():
    mod = _load_module()
    state_dir = Path(tempfile.mkdtemp(prefix="slcsn-"))
    mod.STATE_DIR = state_dir
    mod._CORE_NOTICE_DEBOUNCE_S = 0  # test one-shot behavior, no debounce wait
    ledger = state_dir / mod._CORE_NOTICE_LEDGER
    sent: list[dict] = []

    class _Client:
        def __init__(self, ok=True):
            self.ok = ok

        def chat_postMessage(self, **kwargs):
            if not self.ok:
                raise RuntimeError("slack post failed")
            sent.append(kwargs)

    mod.app.client = _Client()

    try:
        # 1. degraded intake → chat_postMessage to the channel, SLACK ledger
        _write_state(state_dir, "logged-out")
        mod._core_notice_sweep({"C0ABC"})
        expect(len(sent) == 1, "intake: one chat_postMessage")
        expect(sent[0].get("channel") == "C0ABC", "intake: posted to the channel")
        expect("logged out" in sent[0].get("text", ""), "intake: 'logged out' wording")
        expect("_(automated notice)_" in sent[0].get("text", ""), "intake: slack suffix")
        expect(ledger.exists(), "intake: slack-specific ledger written")
        expect(not (state_dir / "core-state-notice.json").exists(),
               "intake: gateway ledger untouched")

        # 2. cooldown suppresses a repeat for the same channel/reason
        mod._core_notice_sweep({"C0ABC"})
        expect(len(sent) == 1, "cooldown: no duplicate")

        # 3. healthy → recovery to the owed channel, ledger cleared
        _write_state(state_dir, "idle-ready")
        mod._core_notice_sweep(set())
        expect(len(sent) == 2 and "back online" in sent[1].get("text", ""),
               "recovery: back-online delivered")
        expect(json.loads(ledger.read_text()).get("active") == {},
               "recovery: ledger active cleared")

        # 4. failed send burns nothing
        _write_state(state_dir, "crashed")
        mod.app.client = _Client(ok=False)
        mod._core_notice_sweep({"C0XYZ"})
        mod.app.client = _Client(ok=True)
        mod._core_notice_sweep({"C0XYZ"})
        expect(any(k.get("channel") == "C0XYZ" for k in sent),
               "retry: notice re-attempted after a failed send")

        # 5. thread targeting (review should-fix #3): a channel @mention's notice
        # threads under the ask, and recovery threads into the same place.
        sent.clear()
        _write_state(state_dir, "logged-out")
        target = mod._core_notice_target("C0THREAD", "1700000000.000100")
        mod._core_notice_sweep({target})
        expect(len(sent) == 1 and sent[0].get("channel") == "C0THREAD"
               and sent[0].get("thread_ts") == "1700000000.000100",
               "intake: channel @mention notice posts in-thread")
        _write_state(state_dir, "idle-ready")
        mod._core_notice_sweep(set())
        expect(any(k.get("thread_ts") == "1700000000.000100"
                   and "back online" in k.get("text", "") for k in sent),
               "recovery: back-online threads into the same conversation")

        # 6. #4 — the sender derives targets from pending unanswered tasks
        mod.RESULTS_DIR = state_dir
        mod.pending_replies = {
            "task-a": {"channel": "C0PEND", "thread_ts": None},
            "task-b": {"channel": "C0THR", "thread_ts": "1700000000.000200"},
            "task-done": {"channel": "C0DONE", "thread_ts": None},
        }
        (state_dir / "task-done.txt").write_text("answered")
        rooms = mod._core_notice_pending_rooms()
        expect(rooms == {"C0PEND", mod._core_notice_target("C0THR", "1700000000.000200")},
               "pending rooms: unanswered only, thread targets encoded")
    finally:
        import shutil
        shutil.rmtree(state_dir, ignore_errors=True)

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("PASS: slack-bridge core-state notices (intake + recovery + ledger isolation)")


if __name__ == "__main__":
    main()
