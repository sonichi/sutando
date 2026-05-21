#!/usr/bin/env python3
"""
Behavioral tests for telegram-bridge.extract_forward_note().

Pins the parsing surface added in PR #905: when a Telegram user forwards a
message, the bridge must preserve original-sender attribution in the task
header so downstream agents can tell "Chi forwarded Boris's note" from
"Chi wrote this".

Run: python3 tests/telegram-bridge-forward-attribution.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def load_bridge_module():
    """Load src/telegram-bridge.py as a module. See telegram-bridge-tofu.test.py
    for why we monkeypatch TELEGRAM_BOT_TOKEN and use importlib."""
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-placeholder-token")
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge", REPO / "src" / "telegram-bridge.py"
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge


CASES = [
    (
        "non-forwarded msg",
        {"text": "hello world"},
        "",
    ),
    (
        "forward_origin user with username",
        {
            "text": "hi",
            "forward_origin": {
                "type": "user",
                "sender_user": {"id": 999, "username": "alice", "first_name": "Alice"},
            },
        },
        " [forwarded from @alice]",
    ),
    (
        "forward_origin user only first_name",
        {
            "text": "hi",
            "forward_origin": {
                "type": "user",
                "sender_user": {"id": 999, "first_name": "Alice"},
            },
        },
        " [forwarded from @Alice]",
    ),
    (
        "forward_origin hidden_user",
        {
            "text": "hi",
            "forward_origin": {
                "type": "hidden_user",
                "sender_user_name": "Anonymous Sender",
            },
        },
        " [forwarded from Anonymous Sender]",
    ),
    (
        "forward_origin channel",
        {
            "text": "hi",
            "forward_origin": {
                "type": "channel",
                "chat": {"id": -1001, "title": "Some News", "username": "some_news"},
                "message_id": 42,
            },
        },
        " [forwarded from channel: Some News]",
    ),
    (
        "forward_origin chat (group)",
        {
            "text": "hi",
            "forward_origin": {
                "type": "chat",
                "sender_chat": {"id": -1001, "title": "Some Group"},
            },
        },
        " [forwarded from chat: Some Group]",
    ),
    (
        "legacy forward_from",
        {
            "text": "hi",
            "forward_from": {"id": 999, "username": "legacy_user"},
        },
        " [forwarded from @legacy_user]",
    ),
    (
        "legacy forward_sender_name",
        {
            "text": "hi",
            "forward_sender_name": "Anonymous Legacy",
        },
        " [forwarded from Anonymous Legacy]",
    ),
    (
        "unknown forward_origin.type falls through gracefully",
        {
            "text": "hi",
            "forward_origin": {"type": "future_unknown_kind", "stuff": "xyz"},
        },
        "",
    ),
]


def main():
    bridge = load_bridge_module()
    fn = bridge.extract_forward_note
    failed = 0
    for name, msg, expected in CASES:
        got = fn(msg)
        ok = got == expected
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: got={got!r} expected={expected!r}")
        if not ok:
            failed += 1
    total = len(CASES)
    print(f"\n{total - failed}/{total} passed.")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
