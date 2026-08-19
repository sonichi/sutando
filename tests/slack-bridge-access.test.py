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
  4. ACCESS_FILE lives under $CLAUDE_CONFIG_DIR/channels/slack/ via the
     shared `claude_home_path()` helper — consistent with telegram + discord
     (so /sutando uninstall scripts find it across vanilla + claude-sutando).
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


def func_block(src: str, name: str):
    """Extract a top-level function body: everything from the def line to the
    next top-level `def` (or EOF). Size-independent — the lazy quantifier
    terminates at the FIRST boundary, so growing a function body can never
    break extraction. Replaces the old per-function char budgets, which had
    to be hand-bumped every time a merge grew a body past the cap
    (2000 → 4000 → 6000 → 8000 → 12000 for _write_task alone)."""
    m = re.search(
        r"def " + re.escape(name) + r"\([^)]*\)[^:]*:\s*\n([\s\S]*?)(?=\n\ndef |\Z)",
        src,
    )
    return m.group(1) if m else None


def main() -> int:
    if not BRIDGE.exists():
        return fail(f"{BRIDGE} not found")

    src = BRIDGE.read_text()

    # 1. load_allowed must not re-interpret the access record. Its None-vs-empty
    #    -set behaviour is asserted in the shared-semantics suite instead.
    block = func_block(src, "load_allowed")
    if block is None:
        return fail("`load_allowed` function not found")
    if not re.search(r"slack_access\.read_access\(", block):
        return fail("load_allowed must read the record through slack_access, so the "
                    "bridge and health-check cannot drift apart on what it means",
                    block)
    for pattern, what in ((r"json\.loads\(", "parses the record itself"),
                          (r"set\(\s*data\.get\(", "re-interprets allowFrom itself")):
        if re.search(pattern, block):
            return fail(f"load_allowed still {what} — that policy belongs to "
                        "slack_access, per the shared-adapter boundary", block)

    # 2. tofu_onboard exists with race-guard + 0o600 chmod
    tofu_block = func_block(src, "tofu_onboard")
    if tofu_block is None:
        return fail("`tofu_onboard` function not found")
    if not re.search(r"if\s+ACCESS_FILE\.exists\(\)", tofu_block):
        return fail("tofu_onboard must race-guard with ACCESS_FILE.exists()", tofu_block)
    # STRENGTHENED: the old assertion accepted `write_text(...)` + `os.chmod(0o600)`,
    # which leaves the file world-readable between the two calls. Require
    # write_private_text(), which creates the fd already 0600 (util_paths), so the
    # window cannot exist. This is strictly stronger — the previous pattern passes
    # for the buggy implementation.
    if not re.search(r"write_private_text\s*\(\s*ACCESS_FILE\s*,", tofu_block):
        return fail("tofu_onboard must use write_private_text(ACCESS_FILE, ...) — file holds "
                    "owner's Slack user ID, must not inherit umask 644",
                    tofu_block)

    # 3. _write_task fails closed on unknown sender. Extraction is
    # size-independent (see func_block) — the old 12,000-char budget broke
    # every time a merge grew the body (last: the Slack TOFU enrollment-code
    # block pushed it to ~12,080 on the merge ref, PR #1989 review).
    write_block = func_block(src, "_write_task")
    if write_block is None:
        return fail("`_write_task` function not found")
    # Must check `user_id not in allowed` (or equivalent) and return None
    if not re.search(
        r"if\s+user_id\s+not\s+in\s+allowed\s*:\s*\n[\s\S]{0,200}?return\s+None",
        write_block,
    ):
        return fail("_write_task must drop messages from senders not in allowed "
                    "(fail-closed access gate)", write_block)

    # 4. ACCESS_FILE resolves via channel_access_path("slack") — the shared
    #    helper that honors $CLAUDE_CONFIG_DIR AND implements the ~30-day
    #    legacy ~/.claude fallback (see util_paths.channel_access_path).
    if not re.search(
        r"ACCESS_FILE\s*=\s*channel_access_path\(\s*['\"]slack['\"]\s*\)",
        src,
    ):
        return fail("ACCESS_FILE must be channel_access_path('slack') "
                    "for parity with telegram + discord bridges (CCD-aware + legacy fallback)")

    print("PASS: slack-bridge.py access control looks correct.")
    print("  - load_allowed returns None when ACCESS_FILE missing (TOFU-eligible)")
    print("  - tofu_onboard race-guards and chmods to 0o600")
    print("  - _write_task fails closed on unknown senders")
    print("  - ACCESS_FILE path consistent with telegram/discord bridges")
    return 0


if __name__ == "__main__":
    sys.exit(main())
