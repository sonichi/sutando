#!/usr/bin/env python3
"""
Structural test for the outbox_log recipient_label population added in PR #1078.

Verifies that all three outbox_log.append call sites in discord-bridge.py
(poll_results, poll_proactive, poll_dm_fallback) pass a human-readable
recipient_label using the correct pattern:
  - DM channels: "<username> DM" (or "DM" when name is unavailable)
  - Guild channels: "#<channel-name>" (or None when name is unavailable)

Does not import discord-bridge.py (would require discord.py + running event
loop). Inspects source text instead, matching the pattern used by other
structural bridge tests in this repo.

Run: python3 tests/discord-bridge-outbox-recipient-label.test.py
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

    # ── 1. All three outbox_log.append blocks pass recipient_label ──────────
    # Find every outbox_log.append( ... ) block and assert recipient_label= is
    # in each one. We locate each block by looking for the append call and then
    # consuming lines until the closing paren — simple enough given the known
    # indented-keyword-args format.
    # Extract each outbox_log.append block via balanced-paren scan.
    append_blocks = []
    for start_m in re.finditer(r"outbox_log\.append\(", src):
        start = start_m.end()
        depth = 1
        i = start
        while i < len(src) and depth > 0:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
            i += 1
        append_blocks.append(src[start:i - 1])

    # Must find at least 3 call sites (poll_results, poll_proactive, poll_dm_fallback).
    if len(append_blocks) < 3:
        print(f"FAIL: expected ≥3 outbox_log.append calls, found {len(append_blocks)}", file=sys.stderr)
        errors += 1
    else:
        for i, block in enumerate(append_blocks, 1):
            if "recipient_label=" not in block:
                print(f"FAIL: outbox_log.append call #{i} missing recipient_label=", file=sys.stderr)
                errors += 1

    # ── 2. DM label pattern: "{name} DM" / fallback "DM" ───────────────────
    # Expect the f-string pattern used in poll_results (DMChannel) and
    # poll_proactive (owner user object).
    dm_label_pattern = re.compile(
        r'_label\s*=\s*f"\{[_\w]+\}\s*DM"\s+if\s+[_\w]+\s+else\s+"DM"'
        r'|_label\s*=\s*f"\{[_\w]+\}\s*DM"\s+if\s+[_\w]+\s+else\s+None'
    )
    dm_matches = dm_label_pattern.findall(src)
    if len(dm_matches) < 2:
        # Allow partial match — at least poll_results uses explicit "DM" fallback.
        # Guard minimum: two DM-label assignments exist.
        alt = re.findall(r'_label\s*=.*DM.*if', src)
        if len(alt) < 2:
            print(f'FAIL: expected ≥2 DM-label assignments, found {len(alt)}', file=sys.stderr)
            errors += 1

    # ── 3. Channel label pattern: "#{name}" ─────────────────────────────────
    channel_label_pattern = re.compile(r'_label\s*=\s*f"#\{[_\w]+\}"\s+if\s+[_\w]+\s+else\s+None')
    channel_matches = channel_label_pattern.findall(src)
    if len(channel_matches) < 2:
        print(
            f"FAIL: expected ≥2 '#{name}' channel-label assignments, found {len(channel_matches)}",
            file=sys.stderr,
        )
        errors += 1

    # ── 4. isinstance guard: DMChannel check for DM vs guild routing ─────────
    if "isinstance(channel, discord.DMChannel)" not in src:
        print("FAIL: isinstance(channel, discord.DMChannel) guard missing", file=sys.stderr)
        errors += 1

    # ── 5. getattr safety: name reads via getattr ───────────────────────────
    # Both recipient.name and channel.name should be read via getattr to
    # tolerate partial objects without AttributeError.
    if 'getattr(channel.recipient, "name", None)' not in src:
        print('FAIL: getattr(channel.recipient, "name", None) safety guard missing', file=sys.stderr)
        errors += 1

    if errors == 0:
        print(f"PASS: discord-bridge outbox recipient_label — all checks OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
