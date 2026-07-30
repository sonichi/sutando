#!/usr/bin/env python3
"""Tests for scripts/lint-hermetic-bridge-tests.py.

Exercises the classifier and every main() mode IN-PROCESS (import + call, never a
subprocess) so the diff-coverage gate actually sees the lines — a subprocess run
executes the code in a child interpreter coverage.py is not tracing
([[reference_coverage_gate_needs_inprocess_tests]]).

The behaviours pinned here are the ones that were wrong in an earlier draft of the lint:
  * a COMMENT naming CLAUDE_CONFIG_DIR must not count as isolation (the bug that let
    tests/bridge-audit-wiring.test.py look hermetic while it read live config)
  * post-import `mod.ACCESS_FILE = <temp>` is MITIGATED, never a hard failure
  * a stale KNOWN_UNISOLATED entry is a NOTE, not exit 1 (a fatal check would redden
    main the moment a PR fixes a listed file)

Run: python3 tests/lint-hermetic-bridge-tests.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "lint_hermetic", REPO / "scripts" / "lint-hermetic-bridge-tests.py"
)
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + (("" if cond else " — " + detail)))
    if not cond:
        failures.append(name)


def write(tmp: Path, body: str) -> Path:
    p = tmp / "sample.test.py"
    p.write_text(body)
    return p


IMPORTS_BRIDGE = """
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location("db", "src/discord-bridge.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
"""

tmpdir = Path(tempfile.mkdtemp(prefix="lint-hermetic-test-"))

# --- classify() ------------------------------------------------------------
check(
    "out of scope: no exec_module -> None",
    lint.classify(write(tmpdir, "import os\nx = 1\n")) is None,
)
check(
    "out of scope: exec_module but no bridge -> None",
    lint.classify(write(tmpdir, "spec.loader.exec_module(m)  # src/health-check.py\n")) is None,
)
check(
    "violation: imports bridge, no isolation",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE)) == lint.VIOLATION,
)
check(
    "clean: os.environ[CLAUDE_CONFIG_DIR] assignment",
    lint.classify(
        write(tmpdir, 'import os\nos.environ["CLAUDE_CONFIG_DIR"] = "/tmp/x"\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
)
check(
    "clean: os.environ.setdefault(CLAUDE_CONFIG_DIR, ...)",
    lint.classify(
        write(tmpdir, 'import os\nos.environ.setdefault("CLAUDE_CONFIG_DIR", "/tmp/x")\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
)
check(
    "clean: CLAUDE_HOME counts too",
    lint.classify(
        write(tmpdir, 'import os\nos.environ["CLAUDE_HOME"] = "/tmp/x"\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
)
check(
    "clean: alt isolation via HOME",
    lint.classify(write(tmpdir, 'import os\nos.environ["HOME"] = "/tmp/x"\n' + IMPORTS_BRIDGE))
    == lint.CLEAN,
)
# --- the two adversarial bypasses (qingyun, #2429 P1) ----------------------
# Both defeated the original regex predicate and are why detection is AST-based.
check(
    "violation: a COMMENT naming CLAUDE_CONFIG_DIR is NOT isolation",
    lint.classify(
        write(tmpdir, "# Hermetic: CLAUDE_CONFIG_DIR is handled, honest.\n" + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
)
check(
    "BYPASS 1: an assignment-SHAPED comment is NOT isolation",
    lint.classify(
        write(
            tmpdir,
            '# os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n' + IMPORTS_BRIDGE,
        )
    )
    == lint.VIOLATION,
    "regex matched comment text; AST never sees comments",
)
check(
    "BYPASS 2: a REAL assignment AFTER exec_module is NOT isolation",
    lint.classify(
        write(
            tmpdir,
            IMPORTS_BRIDGE + '\nimport os, tempfile\nos.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n',
        )
    )
    == lint.VIOLATION,
    "isolation after the import leaves module-level resolution already done",
)
check(
    "ordering: the SAME assignment placed BEFORE exec_module is clean",
    lint.classify(
        write(
            tmpdir,
            'import os, tempfile\nos.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n' + IMPORTS_BRIDGE,
        )
    )
    == lint.CLEAN,
)
check(
    "unparseable file is a VIOLATION, never a silent pass",
    lint.classify(write(tmpdir, "def broken(:\n" + IMPORTS_BRIDGE)) == lint.VIOLATION,
)
check(
    "late HOME assignment is also rejected",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE + '\nimport os\nos.environ["HOME"] = "/tmp/x"\n'))
    == lint.VIOLATION,
)
# BYPASS 3 (qingyun, #2429 second review): the old ALT_ISOLATES accepted any `util_paths.`
# substring, so a COMMENT promising isolation counted as isolation — the very hole this lint
# exists to close. The AST rewrite dropped that predicate; these pin it shut.
check(
    "BYPASS 3: a comment mentioning util_paths.channel_access_path is NOT isolation",
    lint.classify(
        write(
            tmpdir,
            "# util_paths.channel_access_path isolates this later\n" + IMPORTS_BRIDGE,
        )
    )
    == lint.VIOLATION,
)
check(
    "BYPASS 3b: same comment placed after the import is still NOT isolation",
    lint.classify(
        write(
            tmpdir,
            IMPORTS_BRIDGE + "\n# util_paths.channel_access_path isolates this later\n",
        )
    )
    == lint.VIOLATION,
)
check(
    "a REAL patch of channel_access_path before the import IS isolation",
    lint.classify(
        write(
            tmpdir,
            'from unittest.mock import patch\nwith patch("util_paths.channel_access_path"):\n    pass\n'
            + IMPORTS_BRIDGE,
        )
    )
    == lint.CLEAN,
)
check(
    "mitigated: post-import ACCESS_FILE rebind",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE + '\nm.ACCESS_FILE = "/tmp/a.json"\n'))
    == lint.MITIGATED,
)
check(
    "mitigated: post-import channels_env rebind",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE + '\nm.channels_env = "/tmp/e"\n'))
    == lint.MITIGATED,
)
check(
    "slack and telegram bridges are in scope too",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE.replace("discord", "slack"))) == lint.VIOLATION
    and lint.classify(write(tmpdir, IMPORTS_BRIDGE.replace("discord", "telegram"))) == lint.VIOLATION,
)
check("unreadable path -> None", lint.classify(tmpdir / "does-not-exist.py") is None)

# --- scan() ----------------------------------------------------------------
scanned = lint.scan(["tests/lint-hermetic-bridge-tests.test.py"])
check("scan(): skips out-of-scope files", scanned == {}, str(scanned))

# --- repo-wide invariants --------------------------------------------------
tracked = lint.tracked_tests()
check("tracked_tests(): finds the tests dir", len(tracked) > 20, f"got {len(tracked)}")
check(
    "KNOWN_UNISOLATED entries all exist on disk",
    all((lint.REPO / p).exists() for p in lint.KNOWN_UNISOLATED),
    str([p for p in lint.KNOWN_UNISOLATED if not (lint.REPO / p).exists()]),
)

# --- main() modes ----------------------------------------------------------
def run_main(argv: list[str]) -> tuple[int, str]:
    old = sys.argv
    sys.argv = argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = lint.main()
    finally:
        sys.argv = old
    return rc, buf.getvalue()


rc, out = run_main(["lint", "--list"])
check("--list exits 0", rc == 0, f"rc={rc}")
check("--list prints a verdict column", any(v in out for v in (lint.VIOLATION, lint.CLEAN)), out[:120])

rc, out = run_main(["lint"])
check("whole-tree run is green on the current tree", rc == 0, out[-400:])
check("whole-tree run reports the scan summary", "bridge-importing tests scanned" in out, out[-200:])

rc, out = run_main(["lint", "--diff"])
check("--diff exits 0 when nothing relevant changed or all changed files are clean", rc == 0, out[-300:])

# --- failure branches ------------------------------------------------------
# Exercise the paths that only fire on a red tree, by swapping the grandfather list.
_real_known = lint.KNOWN_UNISOLATED
try:
    # Nothing grandfathered -> every existing offender counts as a NEW violation.
    lint.KNOWN_UNISOLATED = frozenset()
    rc, out = run_main(["lint"])
    check("empty grandfather list -> exit 1", rc == 1, f"rc={rc}")
    check("failure output names the FAIL reason", "FAIL — test imports a bridge" in out, out[:200])
    check("failure output lists offending files", "tests/" in out.split("FAIL")[-1], out[-300:])
    check(
        "failure output explains the fix",
        "Set CLAUDE_CONFIG_DIR to a temp dir" in out,
        out[-300:],
    )

    # A listed file that does not exist is stale -> NOTE, and must NOT fail the run.
    lint.KNOWN_UNISOLATED = frozenset(_real_known | {"tests/_definitely-not-here.test.py"})
    rc, out = run_main(["lint"])
    check("stale grandfather entry does NOT fail the run", rc == 0, f"rc={rc}")
    check("stale entry is reported as a NOTE", "NOTE — KNOWN_UNISOLATED entries" in out, out[-400:])
    check(
        "stale entry names the file",
        "tests/_definitely-not-here.test.py" in out,
        out[-400:],
    )
finally:
    lint.KNOWN_UNISOLATED = _real_known

check("grandfather list restored after patching", lint.KNOWN_UNISOLATED is _real_known)

print("\n" + ("FAIL — " + ", ".join(failures) if failures else "PASS — lint-hermetic-bridge-tests"))
sys.exit(1 if failures else 0)
