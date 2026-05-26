#!/usr/bin/env python3
"""
Structural test for PR #1077: channel_name + guild_name injected into Discord
task files.

Before #1077 task files only had numeric `channel_id` and `user_id` — reading
the task required a separate Discord API lookup to know WHICH channel or server
the message came from. #1077 adds two human-readable fields so the task
consumer can act without a live Discord connection.

Guards:
  1. Task-write block contains `channel_name:` and `guild_name:` lines
  2. Both fields are sanitized (newline-stripped via .replace)
  3. DM fallback: guild is None → guild_name = "DM"
  4. Channel name fallback: no `name` attribute → "DM"
  5. Field order: channel_name appears before guild_name in the task body

Uses source-text inspection — no discord.py or event loop required.

Run: python3 tests/discord-bridge-task-channel-fields.test.py
Exit code: 0 on pass, 1 on fail.
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"


def fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def main() -> int:
    if not BRIDGE.exists():
        return fail(f"{BRIDGE} not found")

    src = BRIDGE.read_text(encoding="utf-8")
    errors = 0

    # ── 1. channel_name and guild_name present in task-write block ───────────
    # Find the task-write block by looking for the task_content multi-line
    # f-string that writes the task file (introduced in #1077).
    task_write_block_m = re.search(
        r'channel_name:\s*\{.*?channel_name.*?\}.*?'
        r'guild_name:\s*\{.*?guild_name.*?\}',
        src,
        re.DOTALL,
    )
    if not task_write_block_m:
        # Looser check: at least each field appears somewhere near the task-file write
        if '"channel_name: ' not in src and "f\"channel_name:" not in src:
            print("FAIL: channel_name: field not found in task-write block", file=sys.stderr)
            errors += 1
        if '"guild_name: ' not in src and "f\"guild_name:" not in src:
            print("FAIL: guild_name: field not found in task-write block", file=sys.stderr)
            errors += 1

    # ── 2. Sanitization: newline-stripped via .replace ───────────────────────
    # Both values should be sanitized before writing. Look for `.replace("\n", " ")`
    # on channel_name and guild_name assignments.
    ch_name_assign = re.search(
        r'channel_name\s*=\s*\(getattr\(.*?"name".*?or\s*"DM"\)\.replace\(.*?\\n.*?\)',
        src,
    )
    guild_name_assign = re.search(
        r'guild_name\s*=\s*\(.*?message\.guild\.name.*?else\s*"DM"\)\.replace\(.*?\\n.*?\)',
        src,
    )
    if not ch_name_assign:
        # Looser: just check replace is called on channel_name somewhere near its assignment
        ctx = re.search(r'channel_name\s*=.*?replace.*?\\n', src, re.DOTALL)
        if not ctx:
            print("FAIL: channel_name assignment missing .replace(newline) sanitization", file=sys.stderr)
            errors += 1
    if not guild_name_assign:
        ctx = re.search(r'guild_name\s*=.*?replace.*?\\n', src, re.DOTALL)
        if not ctx:
            print("FAIL: guild_name assignment missing .replace(newline) sanitization", file=sys.stderr)
            errors += 1

    # ── 3. DM fallback for guild_name ───────────────────────────────────────
    # When message.guild is None (a DM), guild_name should fall back to "DM".
    if 'else "DM"' not in src and "else 'DM'" not in src:
        print('FAIL: DM fallback (else "DM") missing for guild_name', file=sys.stderr)
        errors += 1

    # ── 4. Channel name fallback ─────────────────────────────────────────────
    # getattr(message.channel, "name", None) or "DM" — the None + or-"DM" pattern.
    ch_fallback = re.search(
        r'getattr\(message\.channel,\s*"name",\s*None\)\s+or\s+"DM"',
        src,
    )
    if not ch_fallback:
        print('FAIL: channel name fallback pattern (getattr(..., None) or "DM") missing', file=sys.stderr)
        errors += 1

    # ── 5. Field order: channel_name before guild_name in the task body ──────
    ch_pos = src.find('"channel_name: ')
    guild_pos = src.find('"guild_name: ')
    if ch_pos == -1 or guild_pos == -1:
        # Try f-string variant
        ch_pos = src.find('f"channel_name:')
        guild_pos = src.find('f"guild_name:')
    if ch_pos != -1 and guild_pos != -1:
        if ch_pos > guild_pos:
            print("FAIL: channel_name appears after guild_name (field order regression)", file=sys.stderr)
            errors += 1
    else:
        print("FAIL: could not locate channel_name / guild_name task-file write lines", file=sys.stderr)
        errors += 1

    if errors == 0:
        print("PASS: discord-bridge task channel_name + guild_name fields — all checks OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
