#!/usr/bin/env python3
"""Structural tests for stand-identity.json loading in discord-voice-server.ts (#1116).

Verifies:
  1. `personalPath` is imported from util_paths.js
  2. `stand-identity.json` is referenced (not hardcoded "You are Sutando" only)
  3. IIFE pattern exists in owner-tier block (full variant with Origin clause)
  4. IIFE pattern exists in non-owner-tier block (shorter variant, no Origin)
  5. Silent fallthrough on file-read error (catch returns '') in both blocks
  6. `.filter(Boolean)` in non-owner block to drop empty fallthrough lines

Why structural: testing the IIFE's runtime output requires a live discord-voice
session with a mock stand-identity.json. The structural shape is the regression
we care about — "did someone replace the IIFE with a hardcoded name" or "did
someone remove the non-owner block's catch fallthrough."

Run: python3 tests/discord-voice-stand-identity.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "skills" / "discord-voice" / "scripts" / "discord-voice-server.ts"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:600], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def main() -> None:
    if not SERVER.exists():
        fail(f"server file not found: {SERVER}")
        sys.exit(1)

    src = SERVER.read_text()

    # 1. personalPath import
    expect(
        "personalPath" in src and "util_paths.js" in src,
        "personalPath must be imported from util_paths.js",
        ctx=next((l for l in src.splitlines() if "util_paths" in l), ""),
    )

    # 2. stand-identity.json referenced
    expect(
        "stand-identity.json" in src,
        "stand-identity.json must be referenced (not hardcoded identity only)",
    )

    # 3. IIFE pattern — at least two occurrences (owner + non-owner blocks)
    iife_matches = [m.start() for m in re.finditer(r'\(\(\) => \{', src)]
    expect(
        len(iife_matches) >= 2,
        f"expected ≥2 IIFE patterns for owner + non-owner blocks, found {len(iife_matches)}",
    )

    # 4. Owner block has the Origin clause ("nameOrigin")
    expect(
        "nameOrigin" in src,
        "owner-tier block must include nameOrigin (Origin clause) for full Stand identity",
    )

    # 5. Both blocks have silent fallthrough on error
    catch_returns = [m.start() for m in re.finditer(r'catch\s*\{[^}]*return\s*[\'"][\'"]\s*;?\s*\}', src)]
    expect(
        len(catch_returns) >= 2,
        f"expected ≥2 silent-fallthrough catch blocks (owner + non-owner), found {len(catch_returns)}",
        ctx=src[src.find("catch"):src.find("catch") + 200] if "catch" in src else "",
    )

    # 6. Non-owner block uses .filter(Boolean) to drop empty lines
    expect(
        ".filter(Boolean)" in src,
        "non-owner instructions array must use .filter(Boolean) to drop empty IIFE fallthrough",
        ctx=next((l for l in src.splitlines() if "filter(Boolean)" in l), ""),
    )

    # 7. Owner block has "When asked your name or who you are" framing
    expect(
        "When asked your name or who you are" in src,
        "owner-tier IIFE must instruct agent on name-question framing",
    )

    # 8. Non-owner block has shorter "When asked your name" variant (comma-separated, no "or who you are")
    # Owner: "When asked your name or who you are, say ..."
    # Non-owner: "When asked your name, say ..."  (shorter — no "or who you are" clause)
    non_owner_framing_lines = [l for l in src.splitlines() if "When asked your name," in l]
    expect(
        len(non_owner_framing_lines) >= 1,
        "non-owner IIFE must have 'When asked your name, say ...' framing (shorter variant, no 'or who you are')",
        ctx=non_owner_framing_lines[0][:200] if non_owner_framing_lines else "",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 8
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
