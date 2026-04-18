#!/usr/bin/env python3
"""
Regression test for PR #416: `.env` must win over stale shell env.

The original bug was `os.environ.setdefault(k, v)` in three independent env
loaders — setdefault silently refused to overwrite an already-set shell var,
so a rotated credential in `.env` didn't take effect until the user ran
`unset FOO && restart`. This masked itself as "X billing 402" for ~4 hours
because the 402 blamed the account, not the (wrong) token.

This test guards the fix structurally. It's intentionally cheap — no live
processes, no subprocess, no network. It reads the 3 loader files and
asserts:

  1. No `os.environ.setdefault(` call in the env-loading region
  2. A direct `os.environ[key` assignment exists (the fix)
  3. A comment references "PR #416" or "stale shell env" so the intent
     isn't stripped by a blind reformat

Run: python3 tests/env-loader.test.py
Exit code: 0 on pass, 1 on fail.
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent

# Files that load `.env`-style config into os.environ. Add to this list
# when new loaders ship — the test enforces the same pattern for all of
# them.
LOADERS = [
    REPO / "skills/x-twitter/x-post.py",
    REPO / "src/telegram-bridge.py",
    REPO / "skills/image-generation/scripts/generate.py",
]


def check(path: Path) -> list[str]:
    """Return list of failures for this file (empty = passing)."""
    if not path.exists():
        return [f"{path}: file missing"]

    text = path.read_text()
    failures = []

    if re.search(r"os\.environ\.setdefault\s*\(", text):
        failures.append(
            f"{path.relative_to(REPO)}: uses os.environ.setdefault(...) — "
            f"reverts PR #416 bug class"
        )

    if not re.search(r"os\.environ\s*\[\s*['\"]?\w", text) and \
       not re.search(r"os\.environ\s*\[\s*\w+\.strip", text):
        failures.append(
            f"{path.relative_to(REPO)}: no `os.environ[key] = val` "
            f"assignment found — loader pattern changed, re-verify"
        )

    low = text.lower()
    if "pr #416" not in low and "stale shell env" not in low:
        failures.append(
            f"{path.relative_to(REPO)}: missing PR #416 / stale-shell-env "
            f"comment — intent may be lost in next refactor"
        )

    return failures


def main() -> int:
    all_failures = []
    for loader in LOADERS:
        all_failures.extend(check(loader))

    if all_failures:
        print("FAIL — env-loader regression test", file=sys.stderr)
        for f in all_failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"OK — {len(LOADERS)} env loaders use direct assignment (PR #416)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
