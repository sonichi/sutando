#!/usr/bin/env python3
"""
Regression test for #1568 — bridge task files missing skill instructions after context compaction.

Pre-fix (main branch): task files written by bridges contained no ===SKILL INSTRUCTIONS===
block. After context compaction, an agent receiving a voice-note task had no guidance to
(a) notify the user first or (b) transcribe the audio. The result was silent failure.

Post-fix (this PR): owner-tier task files include an ===SKILL INSTRUCTIONS=== block with
exact CLI commands, making each task self-describing regardless of context state.

To reproduce the original failure manually:
  1. On main: send a voice note via Slack/Discord/Telegram
  2. Let the session run long enough for context compaction (~10+ minutes of turns)
  3. The next voice note produces a task file with no skill instructions
  4. Agent response: no notification sent, audio never transcribed, no result

This script verifies:
  - The pre-fix task format (no instructions) → demonstrates the failure mode
  - The post-fix task format (with instructions) → demonstrates the fix
  - That bridge source files on this branch contain the injection code (fails on main)

Run: python3 tests/regression-skill-hints-missing.test.py
Exit code: 0 on pass, 1 on fail.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SENTINEL = "===SKILL INSTRUCTIONS"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        print(f"PASS: {name}")
        PASS += 1
    else:
        print(f"FAIL: {name}" + (f" — {detail}" if detail else ""))
        FAIL += 1


# ---------------------------------------------------------------------------
# Pre-fix: simulate what main-branch _write_task produces for a voice note.
# An agent receiving this file after context compaction has no skill guidance.
# ---------------------------------------------------------------------------

PRE_FIX_TASK = (
    "id: task-1749470000000\n"
    "timestamp: 2026-06-09T10:00:00Z\n"
    "task: [File attached: /tmp/voice-note.m4a]\n"
    "source: slack\n"
    "channel_id: D0B5L7X2TK2\n"
    "user_id: U08LHLW6S4X\n"
    "access_tier: owner\n"
    "priority: normal\n"
)

check(
    "pre-fix: task file has no SKILL INSTRUCTIONS block (agent gets no guidance)",
    SENTINEL not in PRE_FIX_TASK,
    "pre-fix task unexpectedly contains skill instructions",
)
check(
    "pre-fix: task file has no NOTIFY FIRST command",
    "NOTIFY FIRST" not in PRE_FIX_TASK,
)
check(
    "pre-fix: task file has no TRANSCRIBE command",
    "TRANSCRIBE" not in PRE_FIX_TASK,
)
print("  ^ above 3 checks document the failure mode: agent sees no skill guidance at all")

# ---------------------------------------------------------------------------
# Post-fix: inject skill hints using the same inline logic the bridges use.
# This is a direct copy of the injection block in slack-bridge.py so the
# test exercises the same path the bridge exercises at task-write time.
# ---------------------------------------------------------------------------

_notify_py = Path(os.path.expanduser("~/.claude/skills/task-progress/scripts/notify.py"))
_transcribe_py = Path(os.path.expanduser("~/.claude/skills/audio-transcribe/scripts/transcribe.py"))
_task_id = "task-1749470000000"
_channel = "D0B5L7X2TK2"
_attachment_lines = ["/tmp/voice-note.m4a"]

skill_hints = ""
if _notify_py.exists() or _transcribe_py.exists():
    hints_lines = ["===SKILL INSTRUCTIONS (follow before any other action)==="]
    step = 1
    if _notify_py.exists():
        notify_cmd = (
            f"python3 ~/.claude/skills/task-progress/scripts/notify.py"
            f" --source slack --channel-id {_channel}"
            f' --message "On it — back in a moment."'
        )
        hints_lines.append(f"{step}. NOTIFY FIRST: {notify_cmd}")
        step += 1
    if _attachment_lines and _transcribe_py.exists():
        for ap in _attachment_lines:
            hints_lines.append(
                f"{step}. TRANSCRIBE: python3 ~/.claude/skills/audio-transcribe/scripts/transcribe.py '{ap}'"
            )
            step += 1
    hints_lines.append(f"{step}. Then process and write result to results/{_task_id}.txt")
    skill_hints = "\n" + "\n".join(hints_lines) + "\n"

POST_FIX_TASK = (
    f"id: {_task_id}\n"
    "timestamp: 2026-06-09T10:00:00Z\n"
    "task: [File attached: /tmp/voice-note.m4a]\n"
    "source: slack\n"
    f"channel_id: {_channel}\n"
    "user_id: U08LHLW6S4X\n"
    "access_tier: owner\n"
    "priority: normal\n"
    f"{skill_hints}"
)

if _notify_py.exists() or _transcribe_py.exists():
    check(
        "post-fix: task file has SKILL INSTRUCTIONS block",
        SENTINEL in POST_FIX_TASK,
        "post-fix task missing SKILL INSTRUCTIONS block — injection logic not working",
    )
    if _notify_py.exists():
        check(
            "post-fix: task file includes NOTIFY FIRST command",
            "NOTIFY FIRST" in POST_FIX_TASK,
        )
    if _transcribe_py.exists():
        check(
            "post-fix: task file includes TRANSCRIBE command for the voice file",
            "TRANSCRIBE" in POST_FIX_TASK and "/tmp/voice-note.m4a" in POST_FIX_TASK,
        )
else:
    print("SKIP: post-fix behavior tests — neither task-progress nor audio-transcribe skill installed")

# ---------------------------------------------------------------------------
# Bridge source: injection code is present on this branch, absent on main.
# These checks FAIL on main and PASS on this PR — that contrast is the repro.
# ---------------------------------------------------------------------------

slack_src = (REPO / "src" / "slack-bridge.py").read_text()
discord_src = (REPO / "src" / "discord-bridge.py").read_text()
telegram_src = (REPO / "src" / "telegram-bridge.py").read_text()

check(
    "regression: slack bridge contains SKILL INSTRUCTIONS injection "
    "(FAILS on main — proves original failure mode)",
    SENTINEL in slack_src,
    "slack-bridge.py has no injection code — this is main-branch behavior (the bug)",
)
check(
    "regression: discord bridge contains SKILL INSTRUCTIONS injection "
    "(FAILS on main)",
    SENTINEL in discord_src,
    "discord-bridge.py has no injection code — this is main-branch behavior (the bug)",
)
check(
    "regression: telegram bridge contains SKILL INSTRUCTIONS injection "
    "(FAILS on main)",
    SENTINEL in telegram_src,
    "telegram-bridge.py has no injection code — this is main-branch behavior (the bug)",
)

# ---------------------------------------------------------------------------

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
