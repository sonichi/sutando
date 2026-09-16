#!/usr/bin/env python3
"""telegram-bridge core-state notices (silent-core fix) — the sync binder.

Shared dedup/cooldown/recovery logic is covered in
packages/ag2-sparrow/tests/test_core_state_notice.py. This covers only what the
Telegram binder owns: it sweeps the core state on the TELEGRAM ledger (not the
gateway's), sends via the chat-id `api("sendMessage")` path with a PLAIN suffix
(these calls set no parse_mode), and recovers a chat once the core is healthy.

Run: python3 tests/telegram-bridge-core-state-notice.test.py
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

# --- stub the bridge's heavy imports before exec (mirror the access test) ---
_tp = types.ModuleType("task_priority")
_tp.default_priority_for_source = lambda source: "normal"
sys.modules["task_priority"] = _tp
_wd = types.ModuleType("workspace_default")
_wd.resolve_workspace = lambda: REPO
sys.modules["workspace_default"] = _wd
_vp = types.ModuleType("vision_push")
_vp.push_image = lambda path, source="telegram": False
sys.modules["vision_push"] = _vp
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **kw: None
sys.modules["dotenv"] = _dotenv
os.environ["TELEGRAM_BOT_TOKEN"] = "test-stub-token"

_FAILURES: list[str] = []


def expect(cond, label):
    if not cond:
        _FAILURES.append(label)
        print(f"FAIL: {label}", file=sys.stderr)


def _load_bridge():
    src = (REPO / "src" / "telegram-bridge.py").read_text()
    spec = importlib.util.spec_from_loader("telegram_bridge_csn", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(REPO / "src" / "telegram-bridge.py")
    exec(src, mod.__dict__)
    return mod


def _write_state(state_dir: Path, state, kind=None):
    (state_dir / "core-supervisor.json").write_text(json.dumps(
        {"state": state, "detail": "t", "prompt": None, "kind": kind}))


def main():
    bridge = _load_bridge()
    state_dir = Path(tempfile.mkdtemp(prefix="tgcsn-"))
    bridge.STATE_DIR = state_dir
    bridge._CORE_NOTICE_DEBOUNCE_S = 0  # test one-shot behavior, no debounce wait
    ledger = state_dir / bridge._CORE_NOTICE_LEDGER
    sent: list[tuple] = []

    def fake_api(method, **params):
        sent.append((method, params))
        return {"ok": True}

    bridge.api = fake_api

    try:
        # 1. degraded intake → sendMessage to the chat, TELEGRAM ledger written
        _write_state(state_dir, "logged-out")
        bridge._core_notice_sweep({"424242"})
        expect(len(sent) == 1, "intake: one sendMessage")
        expect(sent[0][0] == "sendMessage", "intake: used sendMessage")
        expect(sent[0][1].get("chat_id") == 424242, "intake: chat_id is an int")
        body = sent[0][1].get("text", "")
        expect("logged out" in body, "intake: 'logged out' wording")
        expect(body.endswith(" (automated notice)"), "intake: PLAIN suffix (no markdown)")
        expect("_(" not in body, "intake: no markdown underscores for telegram")
        expect(ledger.exists(), "intake: telegram-specific ledger written")
        expect(not (state_dir / "core-state-notice.json").exists(),
               "intake: gateway ledger untouched")

        # 2. same chat + reason within cooldown → suppressed
        bridge._core_notice_sweep({"424242"})
        expect(len(sent) == 1, "cooldown: no duplicate notice")

        # 3. healthy → recovery to the owed chat, ledger active cleared
        _write_state(state_dir, "idle-ready")
        bridge._core_notice_sweep(set())
        expect(len(sent) == 2 and "back online" in sent[1][1].get("text", ""),
               "recovery: back-online line delivered")
        expect(json.loads(ledger.read_text()).get("active") == {},
               "recovery: ledger active cleared")

        # 4. a failed send burns nothing (retryable)
        _write_state(state_dir, "crashed")
        bridge.api = lambda method, **p: {"ok": False}
        bridge._core_notice_sweep({"999"})
        bridge.api = fake_api
        bridge._core_notice_sweep({"999"})
        expect(any(p.get("chat_id") == 999 for _, p in sent),
               "retry: notice re-attempted after a failed send")
    finally:
        import shutil
        shutil.rmtree(state_dir, ignore_errors=True)

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("PASS: telegram-bridge core-state notices (intake + recovery + ledger isolation)")


if __name__ == "__main__":
    main()
