#!/usr/bin/env python3
"""Lint: ban process.cwd() / Path.cwd() / os.getcwd() outside canonical resolvers.

## Why this test exists

PR family #815-#859 fixed ~28 sites where workspace data was resolved via
process.cwd() / Path.cwd() / os.getcwd() instead of the canonical
resolveWorkspace() helper.  Each future caller that hands-rolls a cwd-relative
workspace path re-introduces the split-brain bug where SUTANDO_WORKSPACE is
set but the write lands in the repo root.  Issue #863.

## What is checked

TypeScript (src/ + scripts/):
  - Fail on any non-comment line containing `process.cwd()`
  - Allowlist: none needed (workspace_default.ts / util_paths.ts mention it
    only in docblocks; the canonical resolver uses $env + homedir(), not cwd)

Python (src/ + scripts/):
  - Fail on any non-comment line containing `Path.cwd()` or `os.getcwd()`
  - Allowlist: src/workspace_default.py (uses Path.cwd() for relative-path
    anchor in _expand_tilde, which is correct and intentional)

Read-only static analysis; no fixtures, no networking.  Runs in <300 ms.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC  = REPO / "src"
SCRIPTS = REPO / "scripts"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _non_comment_lines(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, line) pairs whose content is not purely a comment."""
    suffix = path.suffix
    lines = path.read_text(errors="replace").splitlines()
    out = []
    for i, raw in enumerate(lines, 1):
        stripped = raw.lstrip()
        if suffix in (".ts", ".tsx", ".js", ".mjs"):
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
        elif suffix == ".py":
            if stripped.startswith("#"):
                continue
        out.append((i, raw))
    return out


def _scan_files(roots: list[Path], extensions: tuple[str, ...]) -> list[Path]:
    files = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix in extensions:
                files.append(p)
    return sorted(files)


# ---------------------------------------------------------------------------
# TS/JS check — ban process.cwd()
# ---------------------------------------------------------------------------

TS_CWD_RE = re.compile(r'\bprocess\.cwd\(\)')
TS_ALLOWLIST: set[str] = set()  # no files need allowlisting today

def check_ts_cwd() -> list[str]:
    failures = []
    ts_files = _scan_files([SRC, SCRIPTS], (".ts", ".tsx", ".js", ".mjs"))
    for path in ts_files:
        rel = path.relative_to(REPO)
        if str(rel) in TS_ALLOWLIST:
            continue
        for lineno, line in _non_comment_lines(path):
            if TS_CWD_RE.search(line):
                failures.append(f"{rel}:{lineno}: process.cwd() outside canonical resolver — use resolveWorkspace() from workspace_default.ts")
    return failures


# ---------------------------------------------------------------------------
# Python check — ban Path.cwd() and os.getcwd()
# ---------------------------------------------------------------------------

PY_CWD_RE = re.compile(r'\bPath\.cwd\(\)|\bos\.getcwd\(\)')
PY_ALLOWLIST = {
    "src/workspace_default.py",  # uses Path.cwd() to anchor relative env-var paths
}

def check_py_cwd() -> list[str]:
    failures = []
    py_files = _scan_files([SRC, SCRIPTS], (".py",))
    for path in py_files:
        rel = path.relative_to(REPO)
        if str(rel) in PY_ALLOWLIST:
            continue
        for lineno, line in _non_comment_lines(path):
            if PY_CWD_RE.search(line):
                failures.append(f"{rel}:{lineno}: Path.cwd()/os.getcwd() outside canonical resolver — use resolve_workspace() from workspace_default.py")
    return failures


# ---------------------------------------------------------------------------
# Sanity assertions
# ---------------------------------------------------------------------------

def sanity_checks() -> list[str]:
    errs = []
    # Allowlisted Python files must exist
    for rel in PY_ALLOWLIST:
        if not (REPO / rel).exists():
            errs.append(f"PY_ALLOWLIST entry '{rel}' does not exist on disk")
    # workspace_default modules exist
    for f in ("src/workspace_default.ts", "src/workspace_default.py"):
        if not (REPO / f).exists():
            errs.append(f"canonical resolver '{f}' is missing")
    return errs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    all_failures: list[str] = []

    sanity = sanity_checks()
    if sanity:
        print("SANITY CHECK FAILURES:")
        for s in sanity:
            print(f"  {s}")
        sys.exit(1)

    ts_failures = check_ts_cwd()
    py_failures = check_py_cwd()
    all_failures = ts_failures + py_failures

    if all_failures:
        print(f"cwd-lint: {len(all_failures)} violation(s):\n")
        for f in all_failures:
            print(f"  {f}")
        print(
            "\nFix: replace process.cwd()/Path.cwd()/os.getcwd() with the canonical "
            "resolver (resolveWorkspace() / resolve_workspace()).\n"
            "If this is a legitimate use inside the resolver itself, add to the "
            "allowlist in tests/cwd-lint.test.py."
        )
        sys.exit(1)

    ts_count = len(_scan_files([SRC, SCRIPTS], (".ts", ".tsx", ".js", ".mjs")))
    py_count = len(_scan_files([SRC, SCRIPTS], (".py",)))
    print(f"cwd-lint: OK — {ts_count} TS files, {py_count} Python files, 0 violations")
