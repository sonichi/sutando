#!/usr/bin/env python3
"""Telegram: a proactive result goes to the owner's chat; [dm-only] keeps it there over a [channel:].

Drives the real `main()` poll loop and the real `send_reply`, with a fake Bot API that records
the chat every message is sent to. No network.

  a) a plain proactive body                          -> sent to the owner's chat
  b) [dm-only] + [channel: <another chat>]           -> sent to the owner's chat, not that chat
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-tg-dmonly-")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": ["4242"]}')
OWNER = "4242"
OTHER_CHAT = "-1001234567890"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" {detail}"))
    if not cond:
        FAILURES.append(name)


sys.path.insert(0, str(REPO / "src"))
import proactive_routing as _real_routing  # noqa: E402


class _Stop(Exception):
    """Breaks main()'s poll loop after the drain has run."""


def _load(workspace: Path):
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-not-real"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"
    spec = importlib.util.spec_from_file_location("telegram_bridge_dmonly_under_test",
                                                  REPO / "src" / "telegram-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _swallow(fn):
    try:
        fn()
    except BaseException:
        pass


def deliver(body: str) -> list:
    """One proactive file through a fresh bridge's main(); the chats sendMessage was called for."""
    ws = Path(tempfile.mkdtemp(prefix="tg-dmonly-ws-"))
    results = ws / "results"
    results.mkdir(parents=True)
    (ws / "tasks").mkdir()
    mod = _load(ws)
    mod.RESULTS_DIR = results
    mod.ACCESS_FILE = _cfg / "access.json"
    sent: list[str] = []

    def _api(method, **kw):
        if method == "sendMessage":
            sent.append(str(kw.get("chat_id")))
            return {"ok": True, "result": {"message_id": 1}}
        if method == "getUpdates" and sent:
            raise _Stop()
        return {"ok": True, "result": []}

    mod.api = _api
    mod.load_allowed = lambda: {OWNER}
    mod.presenter_mode_active = lambda *_a, **_k: False
    # main() imports the routing module INSIDE its loop: the claim legs are stubbed to claim,
    # and the body checks stay real.
    routing = types.ModuleType("proactive_routing")
    routing.should_claim_proactive = lambda *_a, **_k: True
    routing.should_claim_proactive_file = lambda *_a, **_k: True
    routing.proactive_destination = lambda *_a, **_k: None
    routing.body_claimable_by = _real_routing.body_claimable_by
    routing.body_target_channel = _real_routing.body_target_channel
    routing.lane_claims_proactive = _real_routing.lane_claims_proactive
    routing.should_claim_proactive_peeked = types.FunctionType(
        _real_routing.should_claim_proactive_peeked.__code__, routing.__dict__)
    sys.modules["proactive_routing"] = routing
    (results / "proactive-tg.txt").write_text(body)
    threading.Thread(target=lambda: _swallow(mod.main), daemon=True).start()
    deadline = time.time() + 6
    while time.time() < deadline and not sent:
        time.sleep(0.1)
    time.sleep(0.4)
    return sent


def main() -> int:
    got = deliver("a plain owner update\n")
    check("a) a plain proactive body is sent to the owner's chat", got == [OWNER], f"got {got}")
    got = deliver(f"[dm-only]\n[channel: {OTHER_CHAT}]\nprivate detail\n")
    check("b) [dm-only] keeps it in the owner's chat over a [channel:]", got == [OWNER], f"got {got}")
    print()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s)")
        return 1
    print("PASS: telegram proactive results go to the owner's chat; [dm-only] overrides a [channel:]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
