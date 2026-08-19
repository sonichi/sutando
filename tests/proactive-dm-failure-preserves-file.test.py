#!/usr/bin/env python3
"""A proactive DM that fails to send must not be deleted.

THE BUG. In `poll_proactive`, `f.unlink(missing_ok=True)` sat OUTSIDE the
try/except that wraps the send, so it ran on the failure path too. A DM Discord
rejected was destroyed, leaving only a log line. Observed live on this host:

    [proactive] failed to DM 1022910063620390932: 413 Payload Too Large
                (error code: 40005): Request entity too large

That message is unrecoverable. The channel-redirect branch a few lines above
already did the right thing — unlink only after a successful send, fall through
otherwise — so the two halves of one function disagreed.

WHY BOTH THIS AND A BEHAVIOURAL SIBLING. This file asserts on the SOURCE — that
the unlink is unreachable from the failure path — which is cheap and survives
refactors of the surrounding loop. `proactive-dm-failure-keeps-file-behaviour.test.py`
drives one real iteration with a failing send and asserts the body survives.

My first version of this docstring claimed a behavioural test was infeasible
because importing the bridge pulls discord.py and resolves the operator's config
dir. That was WRONG: several tests already exec this module
(`bridge-audit-wiring`, `bridge-not-allowlisted-ack`, `bridge-timeout-guards`)
via a stub-and-redirect pattern. The behavioural test then found something this
one could not — the quarantined file kept its `.sending` claim suffix. A stated
limitation nobody re-checks becomes a permanent excuse.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


def _poll_proactive(tree: ast.AST) -> ast.AST | None:
    for n in ast.walk(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "poll_proactive":
            return n
    return None


def _is_unlink(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "unlink"
    )


def main() -> int:
    print("proactive DM failure preserves the file:")
    src = BRIDGE.read_text()
    tree = ast.parse(src)
    fn = _poll_proactive(tree)
    check("poll_proactive is present", fn is not None,
          "renamed or removed — this test is measuring nothing")
    if fn is None:
        print("\nFAILED (1)")
        return 1

    # Every unlink inside poll_proactive must be reachable only from a success
    # path: inside a `try` BODY, never as a sibling AFTER the try/except (which
    # runs regardless) and never inside a handler.
    bad_after, bad_handler, good = [], [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if _is_unlink(sub):
                        good.append(sub.lineno)
            for h in node.handlers:
                for stmt in h.body:
                    for sub in ast.walk(stmt):
                        if _is_unlink(sub):
                            bad_handler.append(sub.lineno)
        # a `try` immediately followed by a bare unlink in the same block
        body = getattr(node, "body", None)
        if isinstance(body, list):
            for i, stmt in enumerate(body[:-1]):
                if isinstance(stmt, ast.Try) and _is_unlink(body[i + 1]):
                    bad_after.append(body[i + 1].lineno)

    check("no unlink runs AFTER a try/except (the bug)", not bad_after,
          f"unconditional unlink at line(s) {bad_after}")
    check("no unlink inside an exception handler", not bad_handler,
          f"unlink at line(s) {bad_handler}")
    # Post-5b the success-path cleanup is the fence's confirm() (behaviorally
    # pinned in its suite); the bad_* checks still guard any raw unlinks.
    confirms = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "confirm"]
    check("at least one success-path cleanup remains", bool(good or confirms),
          "the file is never cleaned up — every proactive message would re-send forever")

    # The failure path must actively preserve the file, not merely skip the
    # delete: silently leaving it makes the 3s poll re-send it forever.
    check("failure path quarantines instead of deleting",
          "undelivered" in src and ".rename(" in src,
          "no quarantine move found — a failed send would re-poll every 3s")
    check("the quarantine says it did NOT delete",
          "NOT deleted" in src, "log line does not distinguish kept-vs-dropped")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All proactive-failure preservation checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
