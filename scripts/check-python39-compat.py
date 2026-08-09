#!/usr/bin/env python3
"""check-python39-compat.py — fail if src/ stops parsing on the interpreter
production actually runs.

WHY THIS EXISTS
---------------
The live discord-bridge on the 24/7 node runs the CommandLineTools system
interpreter:

    /Library/Developer/CommandLineTools/.../Versions/3.9/.../Python  -> 3.9.6

CI does not. `ci.yml` runs on "the runner's stock python3" with no
`setup-python` step, and the only version pins anywhere in .github/workflows
are 3.11 and 3.12. So a module that uses 3.10+ *syntax* passes every gate and
then fails at import on the box that serves the owner — visible only as a
bridge that will not come back after a restart.

`src/progress_stream.py` already documents the constraint in its docstring
("Python 3.9 compatible (workspace runs system python3 = 3.9.6)"), and 47
modules rely on `from __future__ import annotations` to satisfy it. Nothing
enforced it. This script is that enforcement: the documented rule gets a
caller.

WHAT IT DOES AND DOES NOT CATCH
-------------------------------
This is a *syntax* gate — it compiles, it does not import or execute.

  caught      3.10+ grammar that 3.9 genuinely rejects — `match`/`case` is
              the verified example.
  NOT caught  (a) runtime-only 3.10+ features that parse fine on 3.9:
              `datetime.UTC`, `tomllib`, `itertools.batched`, `typing.Self`,
              `StrEnum`, and PEP-604 annotations in modules WITHOUT
              `from __future__ import annotations`.
              (b) constructs DOCUMENTED as 3.10 that CPython 3.9's PEG parser
              accepts anyway — parenthesized context managers are the one
              measured here. Do not assume the version a feature shipped in
              equals the version whose parser rejects it; that assumption
              cost a control that fired on the floor itself.

That limit is deliberate and stated rather than discovered later: importing
all of src/ would require the full dependency set in CI, which is a much
heavier gate for a much smaller marginal catch. A syntax floor is the cheap
90%.

SELF-TEST
---------
`--self-test` asserts the detector can actually go positive. A checker whose
probe is broken reports "0 failures" and looks identical to a healthy tree —
the exact failure this script exists to prevent, one level up. CI runs the
self-test before the real scan, so a silently-broken gate fails loudly.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: Directories scanned. src/ is where the bridge-imported modules live.
DEFAULT_TARGETS = ("src",)

#: Constructs that MUST fail to parse on 3.9. If any of these compiles, the
#: interpreter running this script is newer than the floor and the scan below
#: would be vacuous.
#: `match` is the reliable discriminator. A parenthesized-context-manager
#: control was tried and REMOVED: it is documented as 3.10 but CPython 3.9's
#: PEG parser already accepts it, so it fired on the floor itself and would
#: have made this gate fail closed on every run. Kept as a note because the
#: obvious next addition to this list is exactly that construct.
CONTROL_MUST_FAIL = (
    ("match statement", "match 1:\n    case 1: pass\n"),
)

#: Must always parse — guards against a control that fails for the wrong
#: reason (e.g. a broken harness rejecting everything).
CONTROL_MUST_PASS = (("plain assignment", "x = 1\n"),)


def compiles(source: str, name: str) -> bool:
    """True if `source` parses on the running interpreter."""
    try:
        compile(source, name, "exec")
        return True
    except SyntaxError:
        return False


def self_test() -> int:
    """Prove the detector discriminates. Returns an exit code."""
    bad = []
    for label, src in CONTROL_MUST_FAIL:
        if compiles(src, "<control>"):
            bad.append(
                "control %r COMPILED but must not — this interpreter (%s) is "
                "newer than the 3.9 floor, so the scan would pass vacuously"
                % (label, ".".join(str(v) for v in sys.version_info[:3]))
            )
    for label, src in CONTROL_MUST_PASS:
        if not compiles(src, "<control>"):
            bad.append("control %r FAILED but must pass — harness is broken" % label)
    for line in bad:
        print("self-test: %s" % line, file=sys.stderr)
    if bad:
        print("self-test: FAIL", file=sys.stderr)
        return 1
    print("self-test: ok — detector rejects 3.10+ syntax and accepts 3.9 syntax")
    return 0


def scan(targets: "tuple[str, ...]", repo: Path) -> "list[tuple[Path, str]]":
    failures = []
    for target in targets:
        root = repo / target
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except SyntaxError as exc:
                failures.append((path, "%s (line %s)" % (exc.msg, exc.lineno)))
            except UnicodeDecodeError as exc:
                failures.append((path, "undecodable: %s" % exc))
    return failures


def run(targets: "tuple[str, ...]", repo: Path) -> int:
    """Controls + scan + reporting. Separated from `main` so the failure
    branch is testable against a temp tree instead of requiring a broken file
    to be planted in the real src/."""
    # The controls run before every real scan too: a vacuous pass is worse
    # than a red, because it is indistinguishable from a healthy tree.
    if self_test() != 0:
        return 1

    # Same principle applied to the TARGETS. A mistyped --target, or a target
    # that exists but holds no .py, previously printed "0 file(s) parse
    # cleanly" and exited 0 — a green that means "scanned nothing", which is
    # indistinguishable from "scanned everything and it was fine". That is the
    # failure this whole script exists to prevent, so it is an error here too.
    missing = [t for t in targets if not (repo / t).is_dir()]
    if missing:
        print("python39-compat: target(s) do not exist under %s: %s"
              % (repo, ", ".join(sorted(missing))), file=sys.stderr)
        print("Refusing to report a clean scan over a directory that is not "
              "there — a mistyped --target must not look like a pass.",
              file=sys.stderr)
        return 1

    failures = scan(targets, repo)
    scanned = sum(len(list((repo / t).rglob("*.py"))) for t in targets
                  if (repo / t).is_dir())
    if scanned == 0:
        print("python39-compat: target(s) %s contain no .py files — nothing "
              "was scanned" % ", ".join(sorted(targets)), file=sys.stderr)
        print("Refusing to report a clean scan over zero files.",
              file=sys.stderr)
        return 1

    if failures:
        print("\npython39-compat: %d file(s) do NOT parse on %s"
              % (len(failures), ".".join(str(v) for v in sys.version_info[:3])),
              file=sys.stderr)
        for path, msg in failures:
            print("  %s: %s" % (path.relative_to(repo), msg), file=sys.stderr)
        print("\nThe live bridge runs system python 3.9.6. A module that only "
              "parses on a newer interpreter will fail at import after a "
              "restart. Add 'from __future__ import annotations' if this is an "
              "annotation, or use 3.9-compatible syntax.", file=sys.stderr)
        return 1

    print("python39-compat: %d file(s) parse cleanly on %s"
          % (scanned, ".".join(str(v) for v in sys.version_info[:3])))
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true",
                    help="verify the detector can go positive, then exit")
    ap.add_argument("--target", action="append", default=None,
                    help="directory to scan (repeatable; default: src)")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    repo = Path(__file__).resolve().parent.parent
    targets = tuple(args.target) if args.target else DEFAULT_TARGETS
    return run(targets, repo)


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover
