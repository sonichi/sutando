#!/usr/bin/env python3
"""The resolver-stub arity check in lint-hermetic-bridge-tests.py (#2621).

Hermetic BY CONSTRUCTION: parses source strings and never touches the workspace,
which is the property the check itself exists to protect. Verified by the sibling
behavioural sweep, not asserted here.

The cases below are the ones that actually occurred. `lambda: tmp` against
`resolve_workspace(migrate=False)` raised TypeError at every production call site
and, behind a broad `except`, silently DISABLED the write path — which is how
#2619 stayed green through three review rounds.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "lint_hermetic", REPO / "scripts" / "lint-hermetic-bridge-tests.py"
)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


def flags(src: str) -> bool:
    return bool(lint.resolver_stub_violations(ast.parse(src)))


def main() -> int:
    print("resolver-stub arity check:")

    # --- FLAG: the forms that broke production -----------------------------
    check("zero-arg on a module attribute",
          flags('_wd.resolve_workspace = lambda: REPO'))
    check("zero-arg on a bare name",
          flags('resolve_workspace = lambda: REPO'))
    check("zero-arg on the UNDERSCORE-local name",
          flags('rh._resolve_workspace = lambda: "/tmp/x"'),
          "src/runtime-health.py and src/workspace_lock.py define their own")

    # --- PASS: stubs that can absorb what callers pass ---------------------
    check("*args/**kwargs absorbs anything",
          not flags('_wd.resolve_workspace = lambda *a, **kw: REPO'))
    check("matching positional arity",
          not flags('rh._resolve_workspace = lambda repo: "/tmp/x"'))
    check("keyword-only with a default",
          not flags('_wd.resolve_workspace = lambda *, migrate=False: REPO'))
    check("**kwargs only",
          not flags('_wd.resolve_workspace = lambda **kw: REPO'))

    # --- FLAG: the INDIRECT form, which is what a correct fix produces ------
    # Regression for the gap Sutando-Pro found reviewing #2622. Patching a
    # resolver across an already-imported tree REQUIRES an intermediate name,
    # so this is the common shape, not an exotic one. The single-walk predicate
    # saw neither half: the lambda binds to `_fake` (not a resolver name), and
    # the resolver assignment's value is a Name, not a Lambda.
    check("indirect: zero-arg lambda via an alias",
          flags('_fake = lambda: tmp\nwd.resolve_workspace = _fake'))
    check("indirect: the real sys.modules patch loop",
          flags(
              '_fake = lambda: tmp\n'
              'for _mod in list(sys.modules.values()):\n'
              '    if getattr(_mod, "resolve_workspace", None) is orig:\n'
              '        _mod.resolve_workspace = _fake\n'
              'wd.resolve_workspace = _fake\n'
          ),
          "the exact form in tests/discord-bridge-reply-directive.test.py")
    check("indirect: an ABSORBING alias is still fine",
          not flags('_fake = lambda *a, **kw: tmp\nwd.resolve_workspace = _fake'))
    check("indirect: an alias that never reaches a resolver is ignored",
          not flags('_fake = lambda: tmp\nwd.something_else = _fake'))

    # --- OUT OF SCOPE ------------------------------------------------------
    check("an unrelated zero-arg lambda is ignored",
          not flags('foo.something_else = lambda: REPO'))
    check("a def, not a lambda, is ignored",
          not flags('def resolve_workspace():\n    return REPO'))

    # --- the check must be able to FAIL ------------------------------------
    # Without this, every assertion above passes just as happily against a
    # predicate that returns [] unconditionally.
    neutered = ast.parse('_wd.resolve_workspace = lambda: REPO')
    check("POSITIVE CONTROL — the bad form really is detected",
          lint.resolver_stub_violations(neutered) != [],
          "if this passes while the FLAG cases pass, the predicate is inert")

    # --- reported location is usable ---------------------------------------
    hits = lint.resolver_stub_violations(
        ast.parse('x = 1\ny = 2\n_wd.resolve_workspace = lambda: REPO')
    )
    check("reports line number and name",
          hits == [(3, "resolve_workspace")], f"got {hits}")

    # --- non-simple assignment targets ------------------------------------
    # `a[0] = ...` and `a, b = ...` bind no single bare name. The predicate must
    # skip them rather than raise, and it must not treat them as resolver names.
    check("a subscript target is skipped, not crashed on",
          not flags('reg["resolve_workspace"] = lambda: REPO'))
    check("a tuple target is skipped, not crashed on",
          not flags('resolve_workspace, other = (lambda: REPO), 2'))

    # --- unreadable / unparseable files are skipped, never reported --------
    # scan_resolver_stubs must swallow OSError and SyntaxError: a file it cannot
    # read is "no evidence", never a violation. Uses a path that does not exist,
    # so this stays hermetic — nothing is written.
    missing = lint.scan_resolver_stubs(["tests/__no_such_file_for_lint_test__.py"])
    check("a nonexistent path yields no violation (OSError swallowed)",
          missing == {}, f"got {missing}")

    # --- the REPORTING path, exercised ------------------------------------
    # The error loop and FAIL summary in main() are unreachable while the tree is
    # clean, so inject one synthetic hit. Restores the original either way.
    _orig_scan = lint.scan_resolver_stubs
    lint.scan_resolver_stubs = lambda paths: {"tests/synthetic.test.py": [(7, "resolve_workspace")]}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = lint.main()
    finally:
        lint.scan_resolver_stubs = _orig_scan
    printed = buf.getvalue()
    check("a violation makes main() exit non-zero", rc != 0, f"rc={rc}")
    check("the report names file, line and symbol",
          "tests/synthetic.test.py:7" in printed and "resolve_workspace" in printed,
          f"got: {printed[:160]!r}")
    check("the report explains the DISABLES consequence, not just the arity",
          "DISABLES" in printed and "lambda *a, **kw" in printed)

    # --- the grandfather list must not rot ---------------------------------
    for rel in sorted(lint.KNOWN_RESOLVER_STUBS):
        p = REPO / rel
        check(f"grandfathered entry still exists: {rel}", p.exists())
        if p.exists():
            still = lint.resolver_stub_violations(ast.parse(p.read_text(errors="ignore")))
            check(f"grandfathered entry still violates: {rel}", bool(still),
                  "fixed? remove it from KNOWN_RESOLVER_STUBS in the same PR")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("resolver-stub arity: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
