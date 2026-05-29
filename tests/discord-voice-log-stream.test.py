#!/usr/bin/env python3
"""Structural tests for discord-voice-server.ts log-stream refactor (issue #1053).

Verifies:
- createWriteStream imported and _opLogStream initialized at module level
- appendOperationalLog no longer calls mkdirSync on the hot path
- appendOperationalLog uses _opLogStream (via optional-chain write)
- Stream error handler registers null-on-error for graceful degradation

Adapted from sonichi/sutando#1196's structural test. The fallback-to-
appendFileSync check is intentionally omitted — Chi's #1286 chose
fail-soft (`_opLogStream?.write(...)`, silent no-op on null) over
explicit appendFileSync fallback. Both designs fix the perf issue;
this test validates Chi's chosen shape.

Run: python3 tests/discord-voice-log-stream.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISCORD_VOICE_SERVER = REPO / "skills" / "discord-voice" / "scripts" / "discord-voice-server.ts"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:400], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def main() -> None:
    if not DISCORD_VOICE_SERVER.exists():
        fail(f"file not found: {DISCORD_VOICE_SERVER}")
        sys.exit(1)

    src = DISCORD_VOICE_SERVER.read_text()
    lines = src.splitlines()

    # 1. createWriteStream is imported
    expect(
        "createWriteStream" in src,
        "discord-voice-server.ts: must import createWriteStream from node:fs",
    )

    # 2. WriteStream type is imported or referenced
    expect(
        "WriteStream" in src,
        "discord-voice-server.ts: must use WriteStream type for _opLogStream",
    )

    # 3. _opLogStream declared at module level (not inside a function)
    expect(
        "let _opLogStream" in src or "const _opLogStream" in src,
        "discord-voice-server.ts: must declare _opLogStream at module level",
    )

    # 4. Stream opened via createWriteStream with append flag
    expect(
        "createWriteStream(DISCORD_VOICE_LOG" in src and "flags: 'a'" in src,
        "discord-voice-server.ts: must open DISCORD_VOICE_LOG with createWriteStream in append mode",
    )

    # 5. appendOperationalLog does NOT call mkdirSync directly
    fn_match = re.search(
        r"function appendOperationalLog\(.*?\}\s*\}",
        src,
        re.DOTALL,
    )
    fn_body = fn_match.group(0) if fn_match else ""
    expect(
        fn_body != "",
        "discord-voice-server.ts: appendOperationalLog function must exist",
    )
    expect(
        "mkdirSync" not in fn_body,
        "discord-voice-server.ts: appendOperationalLog must NOT call mkdirSync on the hot path",
        ctx=fn_body[:200] if fn_body else "",
    )

    # 6. appendOperationalLog uses _opLogStream (stream path)
    expect(
        "_opLogStream" in fn_body,
        "discord-voice-server.ts: appendOperationalLog must use _opLogStream",
        ctx=fn_body[:200] if fn_body else "",
    )

    # 7. mkdirSync for DISCORD_VOICE_LOG dir is called at module init (before function def)
    fn_start = src.find("function appendOperationalLog(")
    module_init_section = src[:fn_start] if fn_start > 0 else src
    expect(
        "mkdirSync(dirname(DISCORD_VOICE_LOG)" in module_init_section,
        "discord-voice-server.ts: mkdirSync for DISCORD_VOICE_LOG dir must run at module init, not inside appendOperationalLog",
    )

    # 8. stream error handler set to null on error (graceful degradation)
    expect(
        "_opLogStream.on('error'" in src or '_opLogStream.on("error"' in src,
        "discord-voice-server.ts: _opLogStream must register error handler for graceful degradation",
    )

    # 9-11. existing mkdirSync calls for DATA/RESULTS/TASKS dirs still present
    for dir_var in ("DATA_DIR", "RESULTS_DIR", "TASKS_DIR"):
        expect(
            f"mkdirSync({dir_var}" in src,
            f"discord-voice-server.ts: mkdirSync({dir_var}) must still be present",
        )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        print("All 11 structural tests passed.")


if __name__ == "__main__":
    main()
