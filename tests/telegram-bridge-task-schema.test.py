#!/usr/bin/env python3
"""Source-inspection tests for telegram-bridge.py task file schema alignment.

Prior to this fix (2026-05-26), telegram-bridge.py wrote task files with:
  - `chat_id:` instead of `channel_id:` (schema inconsistency vs all other bridges)
  - no `user_id:` field (sender's Telegram user ID)
  - no `access_tier:` field (bridge access tier for proactive-loop routing)
  - `task:` was placed before `source:` (violates PR #982 stop-at-delimiter rule)

Fix: align with the canonical task file schema used by discord-bridge,
slack-bridge, and the chat-path writer in CLAUDE.md:
  channel_id / user_id / access_tier / priority / task (task: last)

These tests pin the structural properties of the fix so a future refactor
can't silently regress to the old schema.

Run: python3 tests/telegram-bridge-task-schema.test.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "telegram-bridge.py"

src = BRIDGE.read_text()

_pass = 0
_fail = 0


def ok(msg: str) -> None:
    global _pass
    _pass += 1
    print(f"PASS: {msg}")


def fail(msg: str, detail: str = "") -> None:
    global _fail
    _fail += 1
    print(f"FAIL: {msg}" + (f" — {detail}" if detail else ""))


def _task_write_block() -> str:
    """Extract a window of source around the task_file.write_text() call."""
    idx = src.find("task_file.write_text(")
    assert idx >= 0, "task_file.write_text() not found in telegram-bridge.py"
    return src[idx: idx + 600]


# ---------------------------------------------------------------------------
# Schema field presence
# ---------------------------------------------------------------------------

def test_channel_id_present():
    block = _task_write_block()
    if "channel_id:" in block:
        ok("task file includes channel_id: field")
    else:
        fail("task file missing channel_id: field",
             "telegram-bridge must use channel_id: (not chat_id:) for schema consistency")


def test_chat_id_not_used_as_task_field():
    block = _task_write_block()
    # chat_id is a local variable — it must NOT appear as a task-file key
    # (the field should be named channel_id: per the canonical schema)
    if re.search(r'"chat_id:', block) or re.search(r"'chat_id:", block):
        fail("task file still writes chat_id: field instead of channel_id:",
             "rename to channel_id: for schema consistency with discord/slack bridges")
    else:
        ok("task file does NOT use chat_id: as a field key (uses channel_id:)")


def test_user_id_present():
    block = _task_write_block()
    if "user_id:" in block:
        ok("task file includes user_id: field")
    else:
        fail("task file missing user_id: field",
             "sender_id must be recorded so the core can correlate tasks to sender")


def test_access_tier_present():
    block = _task_write_block()
    if "access_tier:" in block:
        ok("task file includes access_tier: field")
    else:
        fail("task file missing access_tier: field",
             "access_tier: owner is required for proactive-loop access control routing")


def test_access_tier_is_owner():
    block = _task_write_block()
    if "access_tier: owner" in block:
        ok("access_tier is set to 'owner'")
    else:
        fail("access_tier not set to owner",
             "all telegram senders pass the allowFrom check → access_tier: owner")


def test_source_field_present():
    block = _task_write_block()
    if "source: telegram" in block:
        ok("task file includes source: telegram")
    else:
        fail("source: telegram missing from task file")


def test_priority_field_present():
    block = _task_write_block()
    if "priority:" in block:
        ok("task file includes priority: field")
    else:
        fail("task file missing priority: field")


# ---------------------------------------------------------------------------
# PR #982 / #1023 field ordering: task: must be last
# ---------------------------------------------------------------------------

def test_task_field_is_last():
    """task: must appear AFTER all header fields.

    PR #982 + #1023 established that `task:` must be the last field in task
    files. Consumers that enforce the stop-at-delimiter rule
    (e.g. _isVoiceTask, _isContextDropTask) stop scanning at the first
    `task:` line — so any header appearing after it is invisible to them,
    closing the task-body injection vector.
    """
    block = _task_write_block()
    task_idx = block.find('"task:') if '"task:' in block else block.find("'task:")
    # All header fields that MUST appear before task:
    for field in ("source:", "channel_id:", "user_id:", "access_tier:", "priority:"):
        field_idx = block.find(field)
        if field_idx < 0:
            continue  # field may not exist yet — covered by separate tests
        if field_idx > task_idx:
            fail(f"'{field}' appears AFTER 'task:' in task file template",
                 "all header fields must come before task: (PR #982 stop-at-delimiter rule)")
            return
    ok("all header fields appear before task: (PR #982 ordering satisfied)")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_channel_id_present()
    test_chat_id_not_used_as_task_field()
    test_user_id_present()
    test_access_tier_present()
    test_access_tier_is_owner()
    test_source_field_present()
    test_priority_field_present()
    test_task_field_is_last()

    total = _pass + _fail
    print(f"\nResults: {_pass}/{total} passed")
    sys.exit(0 if _fail == 0 else 1)
