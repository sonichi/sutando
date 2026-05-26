#!/usr/bin/env python3
"""Structural tests for single-bot voice-channel enforcement (#1089/#1109).

Verifies that discord-voice-server.ts implements the two-layer defense-in-depth
design so multiple Sutando bots never co-occupy a voice channel:

  Layer 1 — cooperative pre-join check (refuses to join if peer already present)
  Layer 2 — adversarial post-join watcher (leaves if peer joins after us)

Both layers are gated on !SUTANDO_PEER_ENFORCEMENT_DISABLED so tests and
deliberate overrides can bypass them.

Why structural: behavioral tests require a live discord.js client + voice
connection. Structural tests catch the regressions that matter: "did someone
delete the peer-check before joinVoiceChannel" or "did both layer gates get
removed while refactoring."

Run: python3 tests/discord-voice-peer-enforcement.test.py
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

    # 1. SUTANDO_PEER_USERNAME_PATTERNS constant exists with default patterns
    expect(
        "SUTANDO_PEER_USERNAME_PATTERNS" in src,
        "SUTANDO_PEER_USERNAME_PATTERNS constant must be defined",
    )
    # Default must include the canonical peer-bot username prefixes
    for pattern in ["Sutando-", "Sutando_", "Lucy-", "Lucy_", "Maddy", "Mini"]:
        expect(
            pattern in src,
            f"default SUTANDO_PEER_USERNAME_PATTERNS must include '{pattern}'",
        )

    # 2. SUTANDO_PEER_ENFORCEMENT_DISABLED env var gate exists
    expect(
        "SUTANDO_PEER_ENFORCEMENT_DISABLED" in src,
        "SUTANDO_PEER_ENFORCEMENT_DISABLED env var gate must exist",
    )
    expect(
        "process.env.SUTANDO_PEER_ENFORCEMENT_DISABLED" in src,
        "SUTANDO_PEER_ENFORCEMENT_DISABLED must be read from process.env",
    )

    # 3. looksLikeSutandoPeer helper function exists
    expect(
        "looksLikeSutandoPeer" in src,
        "looksLikeSutandoPeer helper function must be defined",
    )
    # It must use the patterns array (startsWith check)
    expect(
        "startsWith" in src and "SUTANDO_PEER_USERNAME_PATTERNS" in src,
        "looksLikeSutandoPeer must use SUTANDO_PEER_USERNAME_PATTERNS with startsWith",
    )

    # 4. Layer-1 comment (#1089) exists before joinVoiceChannel
    layer1_comment_pos = src.find("#1089 single-bot enforcement, layer 1")
    expect(
        layer1_comment_pos != -1,
        "layer 1 enforcement comment (#1089) must be present",
    )

    # 5. Layer-1 is gated on !SUTANDO_PEER_ENFORCEMENT_DISABLED
    # Find the layer-1 region and verify the gate
    layer1_region = src[layer1_comment_pos:layer1_comment_pos + 600]
    expect(
        "SUTANDO_PEER_ENFORCEMENT_DISABLED" in layer1_region,
        "layer 1 must be gated on SUTANDO_PEER_ENFORCEMENT_DISABLED",
        ctx=layer1_region[:400],
    )

    # 6. Layer-2 comment (#1089) exists (post-join watcher)
    layer2_comment_pos = src.find("#1089 single-bot enforcement, layer 2")
    expect(
        layer2_comment_pos != -1,
        "layer 2 enforcement comment (#1089) must be present",
    )
    expect(
        layer2_comment_pos > layer1_comment_pos,
        "layer 2 must appear after layer 1 in source",
    )

    # 7. Layer-2 is gated on !SUTANDO_PEER_ENFORCEMENT_DISABLED
    layer2_region = src[layer2_comment_pos:layer2_comment_pos + 1000]
    expect(
        "SUTANDO_PEER_ENFORCEMENT_DISABLED" in layer2_region,
        "layer 2 must be gated on SUTANDO_PEER_ENFORCEMENT_DISABLED",
        ctx=layer2_region[:400],
    )

    # 8. Both layers call looksLikeSutandoPeer (not inline-duplicated logic)
    layer1_uses_helper = "looksLikeSutandoPeer" in src[layer1_comment_pos:layer2_comment_pos]
    layer2_uses_helper = "looksLikeSutandoPeer" in src[layer2_comment_pos:]
    expect(
        layer1_uses_helper,
        "layer 1 must call looksLikeSutandoPeer (not duplicate the check inline)",
    )
    expect(
        layer2_uses_helper,
        "layer 2 must call looksLikeSutandoPeer (not duplicate the check inline)",
    )

    # 9. Layer-1 exits cleanly when peer detected (process.exit(0))
    expect(
        "process.exit(0)" in src[layer1_comment_pos:layer2_comment_pos],
        "layer 1 must call process.exit(0) when peer detected (clean exit for checkWatcher retry)",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 13  # count individual expect() calls above
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
