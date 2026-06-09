#!/usr/bin/env python3
"""
Structural regression test for the Slack bridge access control (PR #867 / #866).

Guards that refactors of src/slack-bridge.py don't accidentally drop the
TOFU + allowlist gate, which is the only thing keeping a Slack bot from
processing tasks for arbitrary senders once it's installed.

Same scope as tests/discord-bridge-access-tier.test.py: STRUCTURAL —
regex-matches the source. Does NOT import the bridge (slack_bolt dep is
optional + heavy). Run manually:

    python3 tests/slack-bridge-access.test.py

Guards:
  1. `load_allowed()` returns None when ACCESS_FILE is missing (the
     None-vs-empty-set distinction TOFU relies on).
  2. `tofu_onboard()` exists, is gated on `ACCESS_FILE.exists()`, and
     writes the file at 0o600 perms (don't leak the owner's Slack user ID
     world-readable via umask 644).
  3. `_write_task()` checks `user_id not in allowed` and drops with a log
     line — fail-closed for unknown senders.
  4. ACCESS_FILE lives under ~/.claude/channels/slack/ — consistent with
     telegram + discord (so /sutando uninstall scripts find it).
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "slack-bridge.py"


def fail(msg: str, context: str = "") -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    if context:
        print("---context---", file=sys.stderr)
        print(context[:1500], file=sys.stderr)
    return 1


def main() -> int:
    if not BRIDGE.exists():
        return fail(f"{BRIDGE} not found")

    src = BRIDGE.read_text()

    # 1. load_allowed returns None on FileNotFoundError
    load_match = re.search(
        r"def load_allowed\(\):\s*\n([\s\S]{0,1500}?)(?=\n\ndef |\Z)",
        src,
    )
    if not load_match:
        return fail("`load_allowed` function not found")
    block = load_match.group(1)
    if not re.search(r"except\s+FileNotFoundError:\s*\n\s+return\s+None", block):
        return fail("load_allowed must `return None` on FileNotFoundError "
                    "(TOFU relies on None vs empty-set distinction)", block)

    # 2. tofu_onboard exists with race-guard + 0o600 chmod
    # Budget bumped 2000 → 4000: cache-restore block added in #899 fix
    # (PR #1292) pushes tofu_onboard + surrounding module-level code past
    # the 2000-char \ndef boundary.
    tofu_match = re.search(
        r"def tofu_onboard\([^)]*\)[^:]*:\s*\n([\s\S]{0,4000}?)(?=\n\ndef |\Z)",
        src,
    )
    if not tofu_match:
        return fail("`tofu_onboard` function not found")
    tofu_block = tofu_match.group(1)
    if not re.search(r"if\s+ACCESS_FILE\.exists\(\)", tofu_block):
        return fail("tofu_onboard must race-guard with ACCESS_FILE.exists()", tofu_block)
    if not re.search(r"os\.chmod\s*\(\s*ACCESS_FILE\s*,\s*0o600\s*\)", tofu_block):
        return fail("tofu_onboard must chmod ACCESS_FILE to 0o600 — file holds "
                    "owner's Slack user ID, must not inherit umask 644",
                    tofu_block)

    # 3. _write_task fails closed on unknown sender. Budget bumped 2000 →
    # 4000 in 98e188b (file-attachment + thread_ts moved in), then →
    # 6000 in the tierMap PR (tier resolution + in-band system-instruction
    # block for non-owner tiers added another ~1.5k chars), then →
    # 8000 in the secret-vault PR (vault interception block grew the body
    # to ~6.3k), then → 12000 when merging feat/bridge-skill-hints-injection
    # (skill-hints block adds ~1.4k on top of vault interception). The
    # check that actually matters runs against the FIRST ~200 chars of the
    # body (the `user_id not in allowed` gate); the budget only needs to be
    # large enough to terminate at the next `\ndef ` boundary.
    write_match = re.search(
        r"def _write_task\([^)]*\)[^:]*:\s*\n([\s\S]{0,12000}?)(?=\n\ndef |\Z)",
        src,
    )
    if not write_match:
        return fail("`_write_task` function not found")
    write_block = write_match.group(1)
    # Must check `user_id not in allowed` (or equivalent) and return None
    if not re.search(
        r"if\s+user_id\s+not\s+in\s+allowed\s*:\s*\n[\s\S]{0,200}?return\s+None",
        write_block,
    ):
        return fail("_write_task must drop messages from senders not in allowed "
                    "(fail-closed access gate)", write_block)

    # 4. ACCESS_FILE path is ~/.claude/channels/slack/
    if not re.search(
        r"ACCESS_FILE\s*=\s*Path\.home\(\)\s*/\s*['\"]\.claude['\"]\s*/\s*['\"]channels['\"]\s*/\s*['\"]slack['\"]\s*/\s*['\"]access\.json['\"]",
        src,
    ):
        return fail("ACCESS_FILE must be ~/.claude/channels/slack/access.json "
                    "for parity with telegram + discord bridges")

    print("PASS: slack-bridge.py access control looks correct.")
    print("  - load_allowed returns None when ACCESS_FILE missing (TOFU-eligible)")
    print("  - tofu_onboard race-guards and chmods to 0o600")
    print("  - _write_task fails closed on unknown senders")
    print("  - ACCESS_FILE path consistent with telegram/discord bridges")
    return 0


if __name__ == "__main__":
    sys.exit(main())
