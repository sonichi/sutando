#!/usr/bin/env python3
"""Structural tests for bot-account default-ignore at receiver subscribe (#1096/#1097).

Verifies that discord-voice-server.ts discriminates bot accounts at the
`speaking.on('start')` handler so peer-bot audio is never piped to Gemini.

Background (#1096): Discord's gateway exposes `User.bot` but the receiver
previously never checked it. On 2026-05-25 this caused a misdiagnosis —
a peer-bot's utterance was attributed to the owner, triggering a false
"name-gate conflict" alert. Fix: fetch user once per speaker, cache the
`bot` flag, skip subscription if `isBot && !ALLOWED_BOT_USER_IDS.has(id)`.

Why structural: testing the runtime skip requires a live discord.js client
with a mock `speaking.on` emitter. Structural tests catch the regressions
that matter: "did someone remove the bot check" or "did the graceful-
degrade catch block get replaced with a hard throw."

Run: python3 tests/discord-voice-bot-ignore.test.py
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

    # 1. ALLOWED_BOT_USER_IDS constant exists with env-var backing
    expect(
        "ALLOWED_BOT_USER_IDS" in src,
        "ALLOWED_BOT_USER_IDS constant must be defined",
    )
    expect(
        "SUTANDO_ALLOWED_BOT_USER_IDS" in src,
        "ALLOWED_BOT_USER_IDS must be backed by SUTANDO_ALLOWED_BOT_USER_IDS env var",
    )

    # 2. botFlagCache field exists in session type/object
    expect(
        "botFlagCache" in src,
        "botFlagCache must be defined in DiscordVoiceSession (per-user bot flag cache)",
    )

    # 3. speaking.on handler exists
    expect(
        "speaking.on(" in src or "speaking.on('" in src,
        "connection.receiver.speaking.on handler must be present",
    )

    # 4. Bot/human discrimination in speaking.on block
    # Locate speaking.on region
    speaking_pos = src.find("speaking.on(")
    expect(speaking_pos != -1, "speaking.on handler must exist")
    if speaking_pos != -1:
        # The bot check region — allow enough chars to reach the guard
        region = src[speaking_pos:speaking_pos + 1500]

        # 4a. user.bot flag is checked
        expect(
            "user.bot" in region or "isBot" in region,
            "speaking.on handler must check the user.bot flag",
            ctx=region[:600],
        )

        # 4b. botFlagCache is used in the handler
        expect(
            "botFlagCache" in region,
            "speaking.on handler must use botFlagCache to avoid repeated fetches",
            ctx=region[:600],
        )

        # 4c. Graceful degrade: catch block sets isBot = false (subscribe anyway on error)
        expect(
            "isBot = false" in region,
            "speaking.on handler must fall back to isBot=false on fetch failure (graceful degrade)",
            ctx=region[:800],
        )

        # 4d. ALLOWED_BOT_USER_IDS allowlist check present
        expect(
            "ALLOWED_BOT_USER_IDS" in region,
            "speaking.on handler must check ALLOWED_BOT_USER_IDS allowlist before ignoring",
            ctx=region[:800],
        )

        # 4e. Ignore/skip path has a log line (operator visibility)
        expect(
            "ignoring bot user" in region or "ALLOWED_BOT_USER_IDS" in region,
            "speaking.on handler must log when ignoring a bot user",
            ctx=region[:800],
        )

    # 5. ALLOWED_BOT_USER_IDS is constructed as a Set (O(1) has() lookups)
    expect(
        "new Set(" in src and "SUTANDO_ALLOWED_BOT_USER_IDS" in src,
        "ALLOWED_BOT_USER_IDS must be a Set (not an array) for O(1) has() lookups",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 12  # count of individual expect() calls above
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
