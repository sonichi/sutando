#!/usr/bin/env python3
"""Tests for skill-hints injection in slack/discord/telegram bridge task files.

Guards that the ===SKILL INSTRUCTIONS=== block is present in owner task files
and correctly references the notify + transcribe commands. Structural tests
only — no live bridge needed.

Run: python3 tests/bridge-skill-hints-injection.test.py
Exit code: 0 on pass, 1 on fail.
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
SLACK_BRIDGE = REPO / "src" / "slack-bridge.py"
DISCORD_BRIDGE = REPO / "src" / "discord-bridge.py"
TELEGRAM_BRIDGE = REPO / "src" / "telegram-bridge.py"

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
# Slack bridge
# ---------------------------------------------------------------------------

slack_src = SLACK_BRIDGE.read_text()

check(
    "slack: skill_hints block defined for owner tasks",
    'skill_hints = ""' in slack_src and 'access_tier == "owner"' in slack_src,
)
check(
    "slack: skill injection guarded by skill file existence check",
    "_notify_py.exists()" in slack_src and "_transcribe_py.exists()" in slack_src,
)
check(
    "slack: notify command uses task-progress skill",
    '"task-progress", "scripts", "notify.py"' in slack_src,
)
check(
    "slack: audio transcription command uses audio-transcribe skill",
    "audio-transcribe/scripts/transcribe.py" in slack_src,
)
check(
    "slack: skill_hints appended to task_file.write_text",
    re.search(r'task_file\.write_text\(.*skill_hints', slack_src, re.DOTALL) is not None,
    "skill_hints not found inside write_text call",
)
check(
    "slack: SKILL INSTRUCTIONS sentinel present",
    "===SKILL INSTRUCTIONS" in slack_src,
)
# The owner-only gate and the thread-reply embed are covered behaviourally in
# tests/slack-bridge-write-task.test.py; a source-grep here pinned only text.

# ---------------------------------------------------------------------------
# Discord bridge
# ---------------------------------------------------------------------------

discord_src = DISCORD_BRIDGE.read_text()

check(
    "discord: skill hints block defined for owner tasks",
    'discord_skill_hints = ""' in discord_src and 'access_tier == "owner"' in discord_src,
)
check(
    "discord: skill injection guarded by skill file existence check",
    "_notify_py.exists()" in discord_src and "_transcribe_py.exists()" in discord_src,
)
check(
    "discord: notify command uses task-progress skill",
    '"task-progress", "scripts", "notify.py"' in discord_src,
)
check(
    "discord: audio transcription command uses audio-transcribe skill",
    "audio-transcribe/scripts/transcribe.py" in discord_src,
)
check(
    "discord: skill hints appended to task_file.write_text",
    re.search(r'task_file\.write_text\(.*discord_skill_hints', discord_src, re.DOTALL) is not None,
    "discord_skill_hints not found inside write_text call",
)
check(
    "discord: SKILL INSTRUCTIONS sentinel present",
    "===SKILL INSTRUCTIONS" in discord_src,
)
check(
    "discord: audio detection checks common voice extensions",
    all(ext in discord_src for ext in (".m4a", ".ogg", ".opus")),
)

# ---------------------------------------------------------------------------
# Telegram bridge
# ---------------------------------------------------------------------------

telegram_src = TELEGRAM_BRIDGE.read_text()

check(
    "telegram: skill hints block defined",
    "tg_skill_hints" in telegram_src,
)
check(
    "telegram: skill injection guarded by skill file existence check",
    "_notify_py.exists()" in telegram_src and "_transcribe_py.exists()" in telegram_src,
)
check(
    "telegram: notify command uses task-progress skill",
    '"task-progress", "scripts", "notify.py"' in telegram_src,
)
check(
    "telegram: audio transcription command uses audio-transcribe skill",
    "audio-transcribe/scripts/transcribe.py" in telegram_src,
)
# The body is assembled into `_task_content` so the envelope can stamp it before
# the write, so the hints must land there, not in write_text()'s argument list.
_tg_assign = telegram_src.find("_task_content = (")
_tg_hints_use = telegram_src.find('f"{tg_skill_hints}"')
_tg_write = telegram_src.find("task_file.write_text(_task_content)")
check(
    "telegram: skill hints reach the content write_text consumes",
    -1 not in (_tg_assign, _tg_hints_use, _tg_write)
    and _tg_assign < _tg_hints_use < _tg_write,
    "tg_skill_hints must be interpolated into _task_content before write_text(_task_content)",
)
check(
    "telegram: SKILL INSTRUCTIONS sentinel present",
    "===SKILL INSTRUCTIONS" in telegram_src,
)
check(
    "telegram: audio detection checks ogg/oga for Telegram voice notes",
    ".oga" in telegram_src and ".ogg" in telegram_src,
)
check(
    "telegram: uses --chat-id (not --channel-id) for Telegram notify",
    "--chat-id" in telegram_src,
)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
