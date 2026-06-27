#!/usr/bin/env python3
"""Structural regression test for the non-owner codex execution template in
src/slack-bridge.py (PR #1801).

Guards:
  1. ===SUTANDO SYSTEM INSTRUCTIONS=== block is injected for non-owner tiers.
  2. Stage 1 wraps codex exec in codex-bounded.sh --stall 45 --max 240
     (not a bare `codex exec`) — prevents slow-grind sandbox hangs beyond the
     stdin-EOF fix that `< /dev/null` alone provides.
  3. `< /dev/null` stdin redirect is present (guards the EOF hang independently).
  4. Stage 2 fallback explicitly mentions exit 125 and 124 — both stalled and
     cap-hit trigger the fallback, not just generic non-zero.
  5. `codex exec --sandbox read-only` is present (sandbox enforcement not weakened).
  6. Two-stage atomic move pattern: .codex-staging-{id}.txt → task-{id}.txt.

Run: python3 tests/slack-bridge-nonowner-codex.test.py
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "slack-bridge.py"


def fail(msg: str, context: str = "") -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    if context:
        print("---context---", file=sys.stderr)
        print(context[:1200], file=sys.stderr)
    return 1


def main() -> int:
    if not BRIDGE.exists():
        return fail(f"{BRIDGE} not found")

    src = BRIDGE.read_text()

    # 1. SUTANDO SYSTEM INSTRUCTIONS block exists for non-owner tiers
    if "===SUTANDO SYSTEM INSTRUCTIONS" not in src:
        return fail("SUTANDO SYSTEM INSTRUCTIONS block missing from slack-bridge.py")

    # 2. codex-bounded.sh is used in Stage 1 (not bare codex exec)
    if "codex-bounded.sh" not in src:
        return fail("codex-bounded.sh missing — Stage 1 must wrap codex exec to prevent slow-grind hangs")
    if "--stall 45" not in src:
        return fail("--stall 45 missing from codex-bounded.sh invocation")
    if "--max 240" not in src:
        return fail("--max 240 missing from codex-bounded.sh invocation")

    # 3. < /dev/null stdin redirect preserved
    if "< /dev/null" not in src:
        return fail("< /dev/null stdin redirect missing — required to prevent codex EOF hang")

    # 4. exit 125 AND 124 are mentioned in the Stage 2 fallback
    if "125" not in src or "124" not in src:
        return fail("Stage 2 fallback must mention exit 125 (stalled) and 124 (cap hit) explicitly")

    # 5. sandbox read-only enforcement preserved
    if "--sandbox read-only" not in src:
        return fail("--sandbox read-only missing — sandbox enforcement must not be weakened")

    # 6. Two-stage atomic mv pattern (staging file → final task file)
    if ".codex-staging-" not in src:
        return fail(".codex-staging- staging file pattern missing — two-stage atomic move required")

    # 7. Injection is conditional on access_tier != "owner"
    # The guard must exist so owner tasks don't get sandboxed
    if 'access_tier != "owner"' not in src and "access_tier != 'owner'" not in src:
        return fail('access_tier != "owner" guard missing — owner tasks must not be sandboxed')

    print("PASS: slack-bridge non-owner codex template: codex-bounded.sh wrap, < /dev/null, "
          "exit 125/124 fallback, sandbox read-only, two-stage atomic move, owner-tier gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
