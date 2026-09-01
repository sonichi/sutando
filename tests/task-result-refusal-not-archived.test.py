#!/usr/bin/env python3
"""A refused ORDINARY task reply must not be archived.

The proactive drains already gate consumption on the delivery result; these
drive the real task-result watchers, where a refusal used to archive silently.
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

# MODULE level, before any exec_module: both bridges resolve their access file
# during import and fall back to the real ~/.claude when CLAUDE_CONFIG_DIR is unset.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-taskrefusal-")
_cfg_slack = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text('{"allowFrom": []}')
_cfg_tg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_cfg_tg.mkdir(parents=True, exist_ok=True)
(_cfg_tg / "access.json").write_text('{"allowFrom": []}')

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


def _settle(predicate, timeout=4.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _swallow(fn):
    try:
        fn()
    except Exception:
        pass


def _seed(mod, task_id: str, body: str, headers: str = "") -> tuple[Path, Path]:
    results = Path(mod.RESULTS_DIR)
    tasks = Path(mod.TASKS_DIR)
    results.mkdir(parents=True, exist_ok=True)
    tasks.mkdir(parents=True, exist_ok=True)
    rf = results / f"{task_id}.txt"
    tf = tasks / f"{task_id}.txt"
    rf.write_text(body)
    # `task:` terminates the header block, so it must come last or the routing
    # headers after it are invisible to parse_task_headers.
    tf.write_text(f"id: {task_id}\n{headers}task: anything\n")
    return rf, tf


# ---------------------------------------------------------------- slack


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


def _load_slack():
    bolt = types.ModuleType("slack_bolt")
    bolt.App = _FakeApp
    sys.modules["slack_bolt"] = bolt
    sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
    socket = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket.SocketModeHandler = type("SocketModeHandler", (), {})
    sys.modules["slack_bolt.adapter.socket_mode"] = socket

    os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="sutando-taskrefusal-slack-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
    spec = importlib.util.spec_from_file_location(
        "slackbridge_taskrefusal_test", REPO / "src" / "slack-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def slack_phase() -> None:
    print("\nSLACK — ordinary task-result drain")
    mod = _load_slack()

    results = Path(mod.RESULTS_DIR)
    check("harness is sandboxed",
          "/var/folders/" in str(results.resolve()) or str(results.resolve()).startswith(
              str(Path(tempfile.gettempdir()).resolve())),
          f"results dir is {results}")

    attempts = {"n": 0}

    # PHASE 1 — the helper REFUSES without raising, which is the ordinary
    # Slack failure; the watcher's `except` never sees it.
    def _refuses(*_a, **_k):
        attempts["n"] += 1
        return False

    mod._send_reply = _refuses

    tid = "task-refusal-1"
    rf, tf = _seed(mod, tid, "a reply slack will refuse", "source: slack\n")
    with mod.pending_replies_lock:
        mod.pending_replies[tid] = {"channel": "C-TEST", "thread_ts": None,
                                    "access_tier": "owner"}

    threading.Thread(target=lambda: _swallow(mod.result_watcher), daemon=True).start()

    check("the refused send was attempted", _settle(lambda: attempts["n"] > 0),
          "watcher never reached the send")

    # Give the drain a chance to (wrongly) archive before asserting survival.
    time.sleep(0.6)
    check("a refused reply is NOT archived", rf.exists(),
          "result file was archived despite the refusal — reply lost")
    check("its task file is NOT archived", tf.exists(),
          "task file was archived despite the refusal")
    with mod.pending_replies_lock:
        still = tid in mod.pending_replies
    check("the route survives for the retry", still,
          "route dropped, so no later poll can deliver it")

    # PHASE 2 — positive control: a delivered reply MUST be consumed, else the
    # assertions above would pass on a watcher that simply archives nothing.
    delivered = {"n": 0}

    def _delivers(*_a, **_k):
        delivered["n"] += 1
        return True

    mod._send_reply = _delivers
    check("a delivered reply IS consumed",
          _settle(lambda: not rf.exists() and delivered["n"] > 0),
          "result survived a successful delivery — the gate is stuck closed")


# ------------------------------------------------------------- telegram


def _load_telegram():
    ws = tempfile.mkdtemp(prefix="sutando-taskrefusal-tg-")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-not-real"
    os.environ["SUTANDO_WORKSPACE"] = ws
    os.environ["SUTANDO_TEST_MODE"] = "1"
    spec = importlib.util.spec_from_file_location(
        "tgbridge_taskrefusal_test", REPO / "src" / "telegram-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ACCESS_FILE = _cfg_tg / "access.json"
    return mod


def telegram_phase() -> None:
    print("\nTELEGRAM — ordinary task-result drain")
    mod = _load_telegram()

    attempts = {"n": 0}

    # send_reply reports a swallowed API failure as ok=False, never by raising.
    def _refuses(*_a, **_k):
        attempts["n"] += 1
        return {"ok": False, "text_chunks": 1, "files_sent": 0}

    mod.send_reply = _refuses
    mod.api = lambda *_a, **_k: {"ok": True}

    # main() owns pending_replies as a local, so routing is seeded the way the
    # bridge really recovers it: from the task file's own headers.
    tid = "task-tg-refusal-1"
    rf, tf = _seed(mod, tid, "a reply telegram will refuse",
                   "source: telegram\nchat_id: 4242\n")

    threading.Thread(target=lambda: _swallow(mod.main), daemon=True).start()

    check("the refused send was attempted", _settle(lambda: attempts["n"] > 0),
          "watcher never reached the send")

    time.sleep(0.6)
    check("a refused reply is NOT archived", rf.exists(),
          "result file was archived despite ok=False — reply lost")
    check("its task file is NOT archived", tf.exists(),
          "task file was archived despite ok=False")

    # A later delivery can only fire if routing survived the refusal, so this
    # doubles as the route-retention check main()'s local dict hides.
    delivered = {"n": 0}

    def _delivers(*_a, **_k):
        delivered["n"] += 1
        return {"ok": True, "text_chunks": 1, "files_sent": 0}

    mod.send_reply = _delivers
    check("a delivered reply IS consumed, proving routing survived",
          _settle(lambda: not rf.exists() and delivered["n"] > 0),
          "result survived a successful delivery — gate stuck closed or route lost")


def main() -> int:
    slack_phase()
    telegram_phase()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("PASS: a refused task reply is retained by both adapters, a delivered one consumed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
