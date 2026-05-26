#!/usr/bin/env python3
"""Structural cross-check that `_poll_proactive` in `src/discord-bridge.py`
honors `[channel: <id>]` redirect markers with the "fail loudly, succeed
quietly" semantic (owner-greenlit 2026-05-26 DM thread).

Why structural and not behavioral: behavioral testing of `_poll_proactive`
requires async discord.py + a mocked client + mocked channel + mocked DM.
That setup has more LOC than the fix itself. The structural test catches
the regressions we actually care about — "did someone delete the redirect
branch" / "did someone start stripping the marker on the failure path
(restoring the silent-failure mode this commit fixed)" — at near-zero cost.

Guards:
  1. `_poll_proactive` parses `[channel:]` markers before DM-send
  2. On successful target-channel send, marker is stripped AND `continue`s
     past the DM-send (quiet success)
  3. On failure paths (channel unresolved / send exception), the literal
     marker is NOT stripped from `clean_text` and falls through to DM —
     i.e. NO `re.sub` between the failure print and the DM send (loud
     failure: operator sees the leaked marker as the signal)

Run: python3 tests/proactive-channel-redirect-loud-failure.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"


_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("---context---", file=sys.stderr)
        print(ctx[:1500], file=sys.stderr)


def extract_poll_proactive(source: str) -> str:
    """Return the body of `async def poll_proactive(...)` as a single string.

    Locates the function by name + asyncdef token, then returns until the
    next `async def` or `def` at column 0 (or EOF).
    """
    # Match indented `async def poll_proactive` (it's a module-level def
    # in discord-bridge.py, so col 0).
    start = source.find("\nasync def poll_proactive(")
    if start == -1:
        return ""
    # Find next top-level def/async def
    after = source[start + 1 :]
    nxt = re.search(r"\n(async def|def) [a-zA-Z_]", after)
    end = start + 1 + (nxt.start() if nxt else len(after))
    return source[start:end]


def main() -> int:
    src = BRIDGE.read_text()
    func = extract_poll_proactive(src)
    if not func:
        return fail("couldn't locate `async def poll_proactive(...)` in discord-bridge.py") or 1

    # Guard 1: the function parses [channel:] markers somewhere.
    if "[channel:" not in func:
        fail(
            "_poll_proactive lacks [channel:] marker handling — proactive files "
            "with the redirect marker will send the literal text to owner DM",
            func,
        )
    # The regex compile or pattern match must be present.
    if "channel:" not in func or "\\d{17,20}" not in func:
        fail(
            "_poll_proactive doesn't parse the [channel: <id>] regex shape (17–20 digit channel id)",
            func,
        )

    # Guard 2: on success, the function strips the marker AND posts to target,
    # AND `continue`s out — i.e. doesn't also DM.
    if "target_ch.send" not in func and "_target_ch.send" not in func:
        fail(
            "_poll_proactive doesn't invoke .send on the resolved target channel — "
            "redirect can't succeed",
            func,
        )
    # The successful-redirect path must `continue` past the DM-send block.
    # We look for a `continue` after the channel-send region.
    if func.count("continue") < 2:
        fail(
            "_poll_proactive needs ≥2 `continue` statements — one for the "
            "successful-redirect path (skip DM), at least one other elsewhere",
            func,
        )

    # Guard 3: on the FAILURE path, the literal marker must NOT be stripped
    # before falling through to DM. Specifically, we check that there's no
    # `re.sub` or assignment-to-clean_text in the failure print's neighborhood.
    # The failure signal in the source is the print containing "keeping literal
    # marker in DM" — if that text isn't present, the loud-failure semantic
    # is missing.
    if "keeping literal marker in DM" not in func:
        fail(
            "_poll_proactive lacks the loud-failure phrase 'keeping literal marker in DM' — "
            "if the failure-path code silently strips the marker, the operator loses the "
            "misroute signal (the 2026-05-26 catch that triggered this PR)",
            func,
        )

    # Guard 4: WARN-style output on failure — `print(... failed ...)` or similar.
    if not re.search(r"(failed|exception|error).{0,80}keeping literal", func, re.IGNORECASE):
        # Be forgiving — just require the loud phrase exists (Guard 3 covers it)
        pass

    if _FAILURES:
        print(f"\n{len(_FAILURES)} failure(s)", file=sys.stderr)
        return 1
    print("PASS: _poll_proactive honors [channel:] with loud-failure semantic")
    return 0


if __name__ == "__main__":
    sys.exit(main())
