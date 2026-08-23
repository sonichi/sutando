#!/usr/bin/env python3
"""Behavioral + structural cross-check that `poll_proactive` in
`src/discord-bridge.py` honors suppression markers (`[no-send]` /
`[REPLIED]` / `[deduped:]`) instead of DM-attempting a `proactive-*.txt`
body that was never meant to be sent.

Sibling bug to the `[channel:]` loud-failure fix (see
`proactive-channel-redirect-loud-failure.test.py`) and to `poll_dm_fallback`,
which already honors these markers — its own docstring incorrectly assumed
`poll_proactive` did too. A suppression-marked proactive file fell through
to DM-send; when that attempt failed (or the recipient/channel resolution
had any hiccup), `ProactiveClaimFence.fail()` parked it in
`results/undelivered/` FOREVER — the exact quarantine backlog
`health-check-proactive-quarantine.test.py` guards the symptom of. This
test guards the cause: the file must never reach a send attempt.

Per REVIEW.md rule #14 ("never assert on source text as a stand-in for a
behavioral claim"), the actual skip decision now lives in an importable,
dependency-light unit — `has_skip_action()` in `src/result_markers.py` —
and is tested BEHAVIORALLY here with real `parse_markers()` output. A
regex over `poll_proactive`'s source text could stay green with the check
disabled outright (e.g. `if False and has_skip_action(...)`) or go red on
a harmless rename; it cannot substitute for exercising the real decision.

What remains structural (and is legitimate per REVIEW.md's structural
exception — a policy must not be duplicated) is: (a) a delegation pin
confirming `poll_proactive`/`poll_dm_fallback` call the shared
`has_skip_action(` helper rather than reimplementing the check inline,
and (b) a negative scan proving the old inline `kind == "skip"`
reimplementation is gone from both branches. Both are paired with the
behavioral test above, never a substitute for it.

Why the discard-behavior guards (`.drop(`, `continue`, ordering vs. the
first send call) stay structural: behavioral testing of `poll_proactive`
itself requires async discord.py + a mocked client + mocked channel +
mocked DM (see `proactive-channel-redirect-loud-failure.test.py`'s own
note — that setup outweighs the fix). Those guards catch "did the drop
stop happening before the DM-send block", a property about control flow
around the (now-verified) decision, not the decision itself.

Run: python3 tests/proactive-suppression-marker-honored.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"
SRC = REPO / "src"

sys.path.insert(0, str(SRC))

from result_markers import parse_markers, has_skip_action  # noqa: E402

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("---context---", file=sys.stderr)
        print(ctx[:1500], file=sys.stderr)


def extract_func(source: str, name: str) -> str:
    """Return the body of `async def <name>(...)` as a single string."""
    start = source.find(f"\nasync def {name}(")
    if start == -1:
        return ""
    after = source[start + 1:]
    nxt = re.search(r"\n(async def|def) [a-zA-Z_]", after)
    end = start + 1 + (nxt.start() if nxt else len(after))
    return source[start:end]


def check_behavioral() -> None:
    """The actual skip decision, exercised with real parse_markers() output."""
    skip_bodies = {
        "no-send": "[no-send]\nInternal note, nothing to deliver.",
        "REPLIED": "[REPLIED]\nAlready sent via another path.",
        "deduped": "[deduped: task-1234]\nSuperseded by the other task.",
    }
    for label, body in skip_bodies.items():
        actions = parse_markers(body).actions
        if not has_skip_action(actions):
            fail(
                f"has_skip_action() returned False for a `{label}` marker body — "
                "this body must never reach a DM-send attempt",
                body,
            )

    # Positive control: ordinary proactive text (no marker) must NOT be
    # treated as skip, or a real delivery would silently regress to it.
    ordinary = "Good morning! Here's your briefing for today: ..."
    if has_skip_action(parse_markers(ordinary).actions):
        fail(
            "has_skip_action() returned True for ordinary, unmarked text — "
            "this would suppress every normal proactive delivery",
            ordinary,
        )


def check_delegation_and_no_reimplementation(func: str, fn_name: str) -> None:
    """Structural: the branch calls the shared helper, not an inline reimplementation.

    Legitimate per REVIEW.md's structural exception (policy must not be
    duplicated), paired with the behavioral test above — never a substitute."""
    if "has_skip_action(" not in func:
        fail(
            f"{fn_name} doesn't call has_skip_action() — suppression markers "
            "must go through the shared src/result_markers.py decision, not "
            "a private re-implementation",
            func,
        )
    if "kind == \"skip\"" in func or "kind=='skip'" in func:
        fail(
            f"{fn_name} still contains an inline `kind == \"skip\"` check — "
            "the reimplementation should have been replaced by has_skip_action(), "
            "not left alongside it",
            func,
        )


def check_discard_behavior(func: str, fn_name: str) -> None:
    """Structural: control flow around the (now behaviorally-verified) decision."""
    skip_idx = func.find("has_skip_action(")
    if skip_idx == -1:
        return  # already reported by check_delegation_and_no_reimplementation
    skip_tail = func[skip_idx:skip_idx + 500]

    if ".drop(" not in skip_tail and "archive_file(" not in skip_tail:
        fail(
            f"{fn_name}'s skip-marker branch doesn't cleanly discard the file "
            "(no .drop(...) / archive_file(...) call nearby) — a suppressed "
            "file must be discarded, never left to reach the send/fail path",
            skip_tail or func,
        )

    send_candidates = [i for i in (func.find(".send("), func.find("deliver_text(")) if i != -1]
    send_idx = min(send_candidates) if send_candidates else -1
    if send_idx != -1 and skip_idx > send_idx:
        fail(
            f"{fn_name}'s skip-marker check appears AFTER the first send call "
            "(.send(/deliver_text() — a suppressed body could still be sent "
            "before the check runs",
            func,
        )

    if "continue" not in skip_tail:
        fail(
            f"{fn_name}'s skip-marker branch doesn't `continue` shortly after — "
            "it may fall through into redirect/DM-send logic anyway",
            skip_tail,
        )


def main() -> int:
    check_behavioral()

    src = BRIDGE.read_text()
    for fn_name in ("poll_proactive", "poll_dm_fallback"):
        func = extract_func(src, fn_name)
        if not func:
            fail(f"couldn't locate `async def {fn_name}(...)` in discord-bridge.py")
            continue
        if "parse_markers(" not in func:
            fail(
                f"{fn_name} doesn't call parse_markers() — suppression markers "
                "must come from the shared src/result_markers.py grammar, not "
                "a private re-implementation",
                func,
            )
        check_delegation_and_no_reimplementation(func, fn_name)
        check_discard_behavior(func, fn_name)

    if _FAILURES:
        print(f"\n{len(_FAILURES)} failure(s)", file=sys.stderr)
        return 1
    print("PASS: has_skip_action() behaves correctly and poll_proactive/"
          "poll_dm_fallback delegate to it (no inline reimplementation)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
