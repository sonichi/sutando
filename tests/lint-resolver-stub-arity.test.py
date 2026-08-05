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

    # --- try/finally: `finally` RUNS ON EVERY PATH, so its rebinding wins -----
    # qingyun-wu's P1, independently reproduced by bassilkhilo-ag2 at e82c01b6.
    # `finalbody` was collected as just another alternative branch and OR-merged
    # with body/handlers, so the pre-`finally` state stayed live beside it. A
    # `finally` that rebinds the stub to a SAFE absorbing lambda therefore left
    # the unsafe binding in the merge and the following line was flagged —
    # a mandatory lint blocking a safe test with no actionable repair, which is
    # the exact false-positive class this PR exists to remove.
    #
    # All four directions are pinned. The three FLAG cases are what stop the fix
    # from being "stop analysing try at all": disabling the construct would make
    # the first case pass and these three regress.
    check("finally rebinding to an absorbing lambda is SAFE (the P1 repro)",
          not flags('_fake = lambda: x\n'
                   'try:\n    pass\n'
                   'finally:\n    _fake = lambda *a, **k: x\n'
                   'wd.resolve_workspace = _fake'))
    check("finally rebinding to a BARE lambda is still flagged",
          flags('_fake = lambda *a, **k: x\n'
                'try:\n    pass\n'
                'finally:\n    _fake = lambda: x\n'
                'wd.resolve_workspace = _fake'))
    check("finally overrides an unsafe except-branch binding",
          not flags('_fake = lambda: x\n'
                   'try:\n    pass\n'
                   'except Exception:\n    _fake = lambda: x\n'
                   'finally:\n    _fake = lambda *a, **k: x\n'
                   'wd.resolve_workspace = _fake'))
    check("an unsafe binding in the try BODY, with an empty finally, still flags",
          flags('try:\n    _fake = lambda: x\n'
                'finally:\n    pass\n'
                'wd.resolve_workspace = _fake'))
    check("an unsafe binding in an EXCEPT handler still flags",
          flags('_fake = lambda *a, **k: x\n'
                'try:\n    pass\n'
                'except Exception:\n    _fake = lambda: x\n'
                'wd.resolve_workspace = _fake'))

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

    # --- FALSE POSITIVES: a mandatory gate must not reject safe tests -------
    # All three reported by @qingyun-wu on #2622 against the file-global alias
    # set. They are the expensive direction for a required check: it blocks an
    # unrelated safe test AND names the wrong line, so the author cannot act on
    # it. Each is a distinct way the old model was wrong — wrong scope, wrong
    # order, wrong reaching binding — not three spellings of one case.
    def viols(src):
        return lint.resolver_stub_violations(ast.parse(src))

    check("scope: a sibling function's zero-arg lambda does not condemn this one",
          viols("""
def old_case():
    _fake = lambda: tmp

def fixed_case():
    _fake = lambda *a, **kw: tmp
    wd.resolve_workspace = _fake
""") == [], "a binding in another scope must not reach here")

    check("order: a LATER bad rebinding does not condemn an earlier safe assign",
          viols("""
_fake = lambda *a, **kw: tmp
wd.resolve_workspace = _fake
_fake = lambda: tmp
""") == [], "statement order ignored — the assignment was safe when it ran")

    check("reaching: the nearest preceding binding wins, not any binding",
          viols("""
_fake = lambda: tmp
_fake = lambda *a, **kw: tmp
wd.resolve_workspace = _fake
""") == [], "the bad binding was superseded before the assignment")

    # --- TRUE POSITIVES that the fix must not lose -------------------------
    check("still flags the indirect loop form (#2619's actual shape)",
          viols("""
_fake = lambda: tmp
for m in mods:
    m.resolve_workspace = _fake
""") != [], "this is the form a correct redirect fix takes; losing it guts the check")

    check("still flags the direct form",
          viols("wd.resolve_workspace = lambda: tmp") != [])

    # --- FALSE NEGATIVES: a path that may not run is still a path -----------
    # @john-the-dev on #2622. The first reaching-binding walk merged only the
    # branch bodies, so a safe rebinding INSIDE a conditional overwrote an
    # unsafe binding that still reached the assignment when the branch did not
    # run. The PR claimed "unsafe on any path is unsafe"; it did not hold.
    check("if with NO else: the fallthrough path still reaches the assignment",
          viols("""
_fake = lambda: tmp
if cond:
    _fake = lambda *a, **kw: tmp
wd.resolve_workspace = _fake
""") != [], "cond False leaves the zero-arg lambda live")

    check("a loop body may run ZERO times",
          viols("""
_fake = lambda: tmp
for x in xs:
    _fake = lambda *a, **kw: tmp
wd.resolve_workspace = _fake
""") != [], "an empty iterable leaves the zero-arg lambda live")

    check("nested scope reads a LATE-bound global (resolved at call time)",
          viols("""
def patch():
    wd.resolve_workspace = _fake
_fake = lambda: tmp
patch()
""") != [], "the def precedes the binding, but the body runs after it")

    # --- a class body is NOT deferred ---------------------------------------
    # @john-the-dev on #2622: the late-binding rule was applied to ClassDef too,
    # but a class body executes IMMEDIATELY at its statement, so definition-point
    # state is exact there and widening it is a false positive.
    check("a class body executes NOW, so a later outer binding cannot reach it",
          viols("""
_fake = lambda *a, **kw: tmp
class Patch:
    wd.resolve_workspace = _fake
_fake = lambda: tmp
""") == [], "the assignment was safe when it ran")

    # A class namespace is NOT a lexical scope for its methods: an unqualified
    # name inside a method skips the class body and resolves module/enclosing.
    # So a class ATTRIBUTE of the same name must not condemn the method
    # (@john-the-dev, #2622). Pairs with the method case just below — one says
    # methods DO inherit module late-binding, this says they do NOT inherit the
    # class body's own names. Both must hold or the model is wrong in one
    # direction.
    check("a class attribute is not in scope for the method",
          viols("""
_fake = lambda *a, **kw: tmp
class P:
    _fake = lambda: tmp
    def patch(self):
        wd.resolve_workspace = _fake
""") == [], "the method's unqualified _fake resolves the SAFE module global")

    # --- a control-flow block is the SAME scope ------------------------------
    # @john-the-dev, #2622. The branch walker built a bare `_ScopeWalk(env, out)`
    # and dropped `ever_unsafe`, so a `def` nested under control flow lost the
    # module's late bindings — unflagged, while the identical top-level `def`
    # was caught. Real runtime semantics: when the branch executes, the function
    # reads `_fake` at CALL time, after the zero-arg binding.
    _nested = """
%s
    def patch():
        wd.resolve_workspace = _fake
_fake = lambda: tmp
patch()
"""
    for _lead in ("if cond:", "for x in xs:", "while cond:", "with open(f) as g:"):
        check(f"deferred scope under `{_lead}` still sees module late-binding",
              viols(_nested % _lead) != [], "control-flow block is the same scope")

    check("deferred scope under try/except still sees it",
          viols("""
try:
    def patch():
        wd.resolve_workspace = _fake
except Exception:
    pass
_fake = lambda: tmp
""") != [])

    # Counter-cases: the fix must not flag a safe binding, and must not re-leak
    # the class namespace through the branch path (the round-5 defect).
    check("SAFE: absorbing lambda under a branch is not flagged",
          viols("""
if cond:
    def patch():
        wd.resolve_workspace = _fake
_fake = lambda *a, **kw: tmp
""") == [])

    check("SAFE: a class attr inside a branch still does not reach its method",
          viols("""
_fake = lambda *a, **kw: tmp
if cond:
    class P:
        _fake = lambda: tmp
        def patch(self):
            wd.resolve_workspace = _fake
""") == [], "branch path must not re-leak the class namespace")

    # A class namespace encloses NOTHING nested in it — not just methods. An
    # inner class does not see the outer class's attributes either
    # (@john-the-dev, #2622, the surface after the method one).
    check("a nested class does not inherit the outer class namespace",
          viols("""
_fake = lambda *a, **kw: tmp
class Outer:
    _fake = lambda: tmp
    class Inner:
        wd.resolve_workspace = _fake
""") == [], "Inner resolves the SAFE module global, not Outer._fake")

    check("a method inside a NESTED class still gets module late-binding",
          viols("""
class Outer:
    class Inner:
        def patch(self):
            wd.resolve_workspace = _fake
_fake = lambda: tmp
""") != [], "deferred body + module binding must still flag through two class layers")

    # ...and the counter-case that stops the cheap fix. Simply excluding ClassDef
    # from late-binding would also lose it for METHODS, whose bodies ARE deferred.
    check("a METHOD inside a class is still late-bound",
          viols("""
class P:
    def patch(self):
        wd.resolve_workspace = _fake
_fake = lambda: tmp
""") != [], "the method body runs after the outer binding, so it must flag")

    # The discriminating counter-case: if/else covers every path, so a safe
    # rebinding in BOTH branches really does supersede. Without this, the fix
    # above could be 'flag whenever any unsafe binding exists in the scope',
    # which passes all three checks above and re-breaks the false positives.
    check("if/else safe in BOTH branches is NOT flagged",
          viols("""
_fake = lambda: tmp
if cond:
    _fake = lambda *a: tmp
else:
    _fake = lambda *a: tmp
wd.resolve_workspace = _fake
""") == [], "every path rebinds safely; flagging here is the old false positive")

    check("conservative on branches: unsafe on ANY path is unsafe",
          viols("""
if cond:
    _fake = lambda: tmp
else:
    _fake = lambda *a: tmp
wd.resolve_workspace = _fake
""") != [], "control flow is unknown, so an unsafe path must still flag")

    # --- try/else: `else` runs SEQUENTIALLY after a successful body ----------
    # john-the-dev's blocker, independently reproduced by bassilkhilo-ag2 at
    # 160af1c2, one branch over from the `finally` defect above: `orelse` was
    # OR-merged as an ALTERNATIVE to the body, but Python executes it after the
    # body completes without raising. So the body's pre-`else` binding stayed
    # live in the merge and a safe `else` rebinding could not clear it.
    #
    # Same four directions as `finally`. The three FLAG cases are what stop the
    # fix from degenerating into "fold orelse into the body unconditionally":
    # on `if` the orelse really IS the alternative, and folding it there would
    # make the if/else case below regress.
    check("else rebinding to an absorbing lambda is SAFE (the repro)",
          not flags('_fake = lambda *a, **k: x\n'
                    'try:\n    _fake = lambda: x\n'
                    'except Exception:\n    _fake = lambda *a, **k: x\n'
                    'else:\n    _fake = lambda *a, **k: x\n'
                    'wd.resolve_workspace = _fake'))
    check("else leaving a BARE lambda is still flagged",
          flags('_fake = lambda *a, **k: x\n'
                'try:\n    pass\n'
                'except Exception:\n    _fake = lambda *a, **k: x\n'
                'else:\n    _fake = lambda: x\n'
                'wd.resolve_workspace = _fake'))
    check("an unsafe EXCEPT branch is still flagged despite a safe else",
          flags('_fake = lambda *a, **k: x\n'
                'try:\n    _fake = lambda *a, **k: x\n'
                'except Exception:\n    _fake = lambda: x\n'
                'else:\n    _fake = lambda *a, **k: x\n'
                'wd.resolve_workspace = _fake'))
    check("if/else orelse is STILL an alternative branch, not sequential",
          flags('if cond:\n    _fake = lambda: x\n'
                'else:\n    _fake = lambda *a, **k: x\n'
                'wd.resolve_workspace = _fake'),
          "folding orelse into the body unconditionally would silence this")

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
