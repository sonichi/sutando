#!/usr/bin/env python3
"""Regression test for PR #1077 port: task file includes channel_name, guild_name,
and source_message_id so the task consumer can identify the origin channel without
grepping numeric IDs against a memory file.

Guards:
  1. channel_name field present in task_file.write_text block.
  2. guild_name field present in task_file.write_text block.
  3. source_message_id field present in task_file.write_text block.
  4. Newline sanitization applied to both human-readable fields.
  5. DM fallback ("DM") used when channel has no .name / guild is None.

Run: python3 tests/discord-bridge-task-file-fields.test.py
Exit code: 0 on pass, 1 on fail.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"


def main() -> int:
    if not BRIDGE.exists():
        print(f"FAIL: {BRIDGE} not found", file=sys.stderr)
        return 1

    src = BRIDGE.read_text()

    # Find the task_file.write_text block by anchoring on its unique `id:` field.
    match = re.search(
        r"task_file\.write_text\s*\(([\s\S]{0,1500}?)\)\s*\n\s*pending_replies",
        src,
    )
    if not match:
        print("FAIL: could not locate task_file.write_text(...) block", file=sys.stderr)
        return 1

    block = match.group(1)
    fails = []

    # 1. channel_name field
    if "channel_name:" not in block:
        fails.append("channel_name: field missing from task_file.write_text block")

    # 2. guild_name field
    if "guild_name:" not in block:
        fails.append("guild_name: field missing from task_file.write_text block")

    # 3. source_message_id field
    if "source_message_id:" not in block:
        fails.append("source_message_id: field missing from task_file.write_text block")

    # 4. Newline sanitization — look for .replace("\n", " ") near the assignments
    assign_region = src[max(0, match.start() - 600):match.start()]
    has_replace = '.replace("\\n"' in assign_region or ".replace('\\n'" in assign_region
    if not has_replace:
        fails.append("channel_name newline sanitization (.replace(\\\\n)) not found before write block")

    # 5. DM fallback — getattr with "DM" fallback for channel, None guard for guild
    assign_region = src[max(0, match.start() - 500):match.start()]
    if '"DM"' not in assign_region and "'DM'" not in assign_region:
        fails.append('DM fallback ("DM") not found in channel_name/guild_name assignments')

    if fails:
        for f in fails:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1

    print("PASS: discord-bridge.py task_file includes channel_name, guild_name, source_message_id.")
    print("  - channel_name: field present")
    print("  - guild_name: field present")
    print("  - source_message_id: field present")
    print("  - DM fallback present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
