#!/usr/bin/env python3
"""Telegram: a proactive result goes to the owner's chat; [dm-only] keeps it there over a [channel:].

Drives one pass of the real `main()` loop (the sleep ends it), the real claim policy and the real
`send_reply`, with a fake Bot API that records the chat every message is sent to. No network.

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
import json
import time
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




class _Stop(BaseException):
    """Raised by the patched sleep to end main() after one pass."""


def _load(workspace: Path):
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-not-real"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"
    spec = importlib.util.spec_from_file_location("telegram_bridge_dmonly_under_test",
                                                  REPO / "src" / "telegram-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def deliver(body: str) -> list:
    """One proactive file through one real main() pass; the chats sendMessage was called for."""
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
        return {"ok": True, "result": []}

    mod.api = _api
    mod.load_allowed = lambda: {OWNER}
    mod.presenter_mode_active = lambda *_a, **_k: False
    # Real claim policy: telegram must be the owner's last-active channel to claim.
    mod.OWNER_ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    mod.OWNER_ACTIVITY_FILE.write_text(json.dumps({"channel": "telegram", "ts": time.time()}))
    (results / "proactive-tg.txt").write_text(body)

    def _stop(_seconds=0):
        raise _Stop()

    real_sleep, time.sleep = time.sleep, _stop
    try:
        mod.main()  # the loop's end-of-pass sleep stops it; any other exception fails the test
    except _Stop:
        pass
    finally:
        time.sleep = real_sleep
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
