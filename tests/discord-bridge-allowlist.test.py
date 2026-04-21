#!/usr/bin/env python3
"""
Structural regression test for the discord-bridge file-send allowlist
(PR #494). Guards against accidental removal of the CodeQL sanitizer
pattern or the fail-closed allowlist behavior.

The CodeQL py/path-injection rule relies on the realpath+startswith
sanitizer being inline at the sink. If a refactor moves that logic into
a helper whose return is used inconsistently, or drops the allowlist in
favor of bare `discord.File(fpath)`, we want to catch it at test time —
not after a talk demo leaks an attacker-supplied path.

Scope: STRUCTURAL — regex-matches the source file. Does NOT import the
bridge (discord.py dep weight is huge). Mirrors the style of
`discord-bridge-access-tier.test.py`.

Guards:
  1. `_is_path_sendable` function is defined.
  2. Inside `_is_path_sendable`, `os.path.realpath` is used (NOT
     replaceable with `Path.resolve()` without re-proving to CodeQL).
  3. `_is_path_sendable` checks both SEND_ALLOWED_ROOTS and
     SEND_ALLOWED_PREFIXES before returning True.
  4. Fail-closed default: the helper returns False when no allowlist
     entry matches.
  5. `discord.File(fpath)` is always gated by a call to
     `_is_path_sendable` — no bare sends.

Run: python3 tests/discord-bridge-allowlist.test.py
Exit: 0 on pass, 1 on fail.
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"


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

    # 1. Helper defined
    helper_match = re.search(
        r"def _is_path_sendable\(fpath:\s*str\)\s*->\s*bool:\s*\n([\s\S]{0,2000}?)(?=\n\ndef |\n\n[A-Z]|\Z)",
        src,
    )
    if not helper_match:
        return fail("`_is_path_sendable` function not found")
    helper_body = helper_match.group(1)

    # 2. realpath used (CodeQL sanitizer pattern)
    if "os.path.realpath" not in helper_body:
        return fail(
            "_is_path_sendable must use os.path.realpath (CodeQL py/path-injection sanitizer)",
            helper_body,
        )

    # 3. Both ROOTS and PREFIXES consulted
    if "SEND_ALLOWED_ROOTS" not in helper_body or "SEND_ALLOWED_PREFIXES" not in helper_body:
        return fail(
            "_is_path_sendable must check both SEND_ALLOWED_ROOTS and SEND_ALLOWED_PREFIXES",
            helper_body,
        )

    # 4. Fail-closed default: final `return False` after the loops
    if not re.search(r"for prefix in SEND_ALLOWED_PREFIXES:[\s\S]+?return\s+False", helper_body):
        return fail(
            "_is_path_sendable must return False after iterating both allowlists (fail-closed)",
            helper_body,
        )

    # 5. Every discord.File(fpath) send call must be gated by _is_path_sendable.
    # Find all `discord.File(...)` sink calls; for each, check that the
    # enclosing 6-line window above contains an `_is_path_sendable` guard.
    for match in re.finditer(r"discord\.File\(\s*(\w+)\s*\)", src):
        arg = match.group(1)
        # Find the 6-line window ending at this match
        start = src.rfind("\n", 0, match.start())
        for _ in range(6):
            prev = src.rfind("\n", 0, start)
            if prev < 0:
                break
            start = prev
        window = src[start:match.end()]
        if f"_is_path_sendable({arg})" not in window and f"_is_path_sendable( {arg}" not in window:
            return fail(
                f"discord.File({arg}) sink found without preceding _is_path_sendable gate",
                window,
            )

    print("PASS: discord-bridge.py file-send allowlist looks correct.")
    print("  - _is_path_sendable uses os.path.realpath (CodeQL sanitizer)")
    print("  - checks both SEND_ALLOWED_ROOTS and SEND_ALLOWED_PREFIXES")
    print("  - fail-closed default (returns False if no allowlist match)")
    print("  - all discord.File() sinks gated by _is_path_sendable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
