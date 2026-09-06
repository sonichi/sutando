#!/usr/bin/env python3
"""
The briefing's closing Insight line must not be cut at a path or a decimal.

`synthesize()` renders the insight as a "first sentence". It used to take
`insight.split('.')[0]`, which ends the sentence at ANY period — including the
one inside `.github/` or `1.4s`. Observed live 2026-09-06: daily-insight.py
produced

    "Sutando authored 2 commits in the last 24h, mostly in .github/, src/,
     tests/ (branch work; landed count unavailable)."

and the delivered briefing read "Insight: Sutando authored 2 commits in the
last 24h, mostly in." — a broken half-sentence in the owner's daily message.

A sentence-ending period is followed by whitespace or end-of-string; the
period in a path or a decimal is not. Every case here drives the PRODUCTION
`synthesize()`, because the defect was in what it renders, not in a helper.

Run: python3 tests/morning-briefing-insight-sentence.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("mb", REPO / "src" / "morning-briefing.py")
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)

FAILS = []


def check(cond, msg):
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILS.append(msg)


def render(insight):
    """The production path: synthesize() with every other source absent."""
    return mb.synthesize(None, None, None, None, None, None, insight=insight)


def main() -> int:
    # a) the live 2026-09-06 case: a dotted path must not end the sentence
    real = ("Sutando authored 2 commits in the last 24h, mostly in .github/, "
            "src/, tests/ (branch work; landed count unavailable).")
    out = render(real)
    check("mostly in .github/, src/, tests/" in out,
          "a dotted path does not truncate the Insight line")
    check("mostly in." not in out,
          "the broken half-sentence 'mostly in.' is not emitted")

    # b) CONTROL — the intended behaviour is unchanged: still FIRST sentence only
    two = "You shipped 5 PRs yesterday. That is double your weekly median."
    out2 = render(two)
    check("Insight: You shipped 5 PRs yesterday." in out2,
          "a real sentence boundary still ends the Insight line")
    check("double your weekly median" not in out2,
          "the second sentence is still dropped (first-sentence rule intact)")

    # c) a decimal is not a sentence end either
    dec = "Median latency was 1.4s across 12 calls. Slower than usual."
    out3 = render(dec)
    check("1.4s across 12 calls" in out3, "a decimal does not truncate the Insight line")
    check("Slower than usual" not in out3, "the second sentence is still dropped")

    # d) CONTROL — the raw-data guard still suppresses, so this test cannot
    #    pass merely because every insight is now emitted verbatim.
    raw = 'insight {"commits": 2, "files": 9, "branch": "x", "extra": "y"}'
    check("Insight:" not in render(raw), "raw-data insight is still suppressed")

    print(("FAILED: %d" % len(FAILS)) if FAILS else "ALL PASS")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
