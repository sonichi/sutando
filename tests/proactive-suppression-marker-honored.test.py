#!/usr/bin/env python3
"""Structural cross-check that `poll_proactive` in `src/discord-bridge.py`
honors suppression markers (`[no-send]` / `[REPLIED]` / `[deduped:]`) instead
of DM-attempting a `proactive-*.txt` body that was never meant to be sent.

Sibling bug to the `[channel:]` loud-failure fix (see
`proactive-channel-redirect-loud-failure.test.py`) and to `poll_dm_fallback`,
which already honors these markers — its own docstring incorrectly assumed
`poll_proactive` did too: "Proactive files are handled by `poll_proactive()`
already, so we don't touch those either." It didn't. A suppression-marked
proactive file fell through to DM-send; when that attempt failed (or the
recipient/channel resolution had any hiccup), `ProactiveClaimFence.fail()`
parked it in `results/undelivered/` FOREVER — the exact quarantine backlog
`health-check-proactive-quarantine.test.py` guards the symptom of. This test
guards the cause: the file must never reach a send attempt in the first place.

Why structural and not behavioral: behavioral testing of `poll_proactive`
requires async discord.py + a mocked client + mocked channel + mocked DM.
See `proactive-channel-redirect-loud-failure.test.py`'s own note — that
setup outweighs the fix. The structural test catches the regression that
matters: "did someone delete the skip-marker check" / "did the check stop
using the shared `parse_markers()` grammar" / "did the drop stop happening
before the DM-send block."

Run: python3 tests/proactive-suppression-marker-honored.test.py
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
    """Return the body of `async def poll_proactive(...)` as a single string."""
    start = source.find("\nasync def poll_proactive(")
    if start == -1:
        return ""
    after = source[start + 1:]
    nxt = re.search(r"\n(async def|def) [a-zA-Z_]", after)
    end = start + 1 + (nxt.start() if nxt else len(after))
    return source[start:end]


def main() -> int:
    src = BRIDGE.read_text()
    func = extract_poll_proactive(src)
    if not func:
        return fail("couldn't locate `async def poll_proactive(...)` in discord-bridge.py") or 1

    # Guard 1: markers come from the shared grammar, not a private regex.
    # CLAUDE.md: "Marker parsing is centralised — do not re-implement it."
    if "parse_markers(" not in func:
        fail(
            "poll_proactive doesn't call parse_markers() — suppression markers "
            "must come from the shared src/result_markers.py grammar, not a "
            "private re-implementation",
            func,
        )

    # Guard 2: it actually checks for a skip-kind action.
    if "kind == \"skip\"" not in func and "kind=='skip'" not in func:
        fail(
            "poll_proactive doesn't check for a skip-kind marker action — "
            "[no-send]/[REPLIED]/[deduped:] bodies would still be DM-attempted",
            func,
        )

    # Guard 3: the skip branch itself must call .drop(...), checked in the
    # post-skip-check window (not "anywhere in func" — a later, unrelated drop() exists).
    skip_idx = func.find("kind == \"skip\"")
    if skip_idx == -1:
        skip_idx = func.find("kind=='skip'")
    skip_tail = func[skip_idx:skip_idx + 400] if skip_idx != -1 else ""
    if ".drop(" not in skip_tail:
        fail(
            "poll_proactive's skip-marker branch doesn't call "
            "_proactive_fence().drop(...) — a suppressed file must be "
            "cleanly discarded (unlinked, never DM-attempted), not left to "
            "reach the send/fail path",
            skip_tail or func,
        )

    # Guard 4: ordering — the skip check must precede the first send call.
    # Checks both .send( and deliver_text( — the latter is the real text-DM path.
    send_candidates = [i for i in (func.find(".send("), func.find("deliver_text(")) if i != -1]
    send_idx = min(send_candidates) if send_candidates else -1
    if skip_idx != -1 and send_idx != -1 and skip_idx > send_idx:
        fail(
            "the skip-marker check appears AFTER the first send call "
            "(.send(/deliver_text() in poll_proactive — a suppressed body "
            "could still be sent before the check runs",
            func,
        )

    # Guard 5: the skip branch `continue`s past the rest of the loop body
    # (does not fall through to redirect/DM resolution).
    if skip_idx != -1:
        if "continue" not in skip_tail:
            fail(
                "the skip-marker branch doesn't `continue` shortly after — "
                "it may fall through into redirect/DM-send logic anyway",
                skip_tail,
            )

    if _FAILURES:
        print(f"\n{len(_FAILURES)} failure(s)", file=sys.stderr)
        return 1
    print("PASS: poll_proactive honors suppression markers (no-send/REPLIED/deduped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
