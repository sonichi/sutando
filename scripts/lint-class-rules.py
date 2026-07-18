#!/usr/bin/env python3
"""Layer 3 (#1543): CI lint for migration CLASS_RULES vs reader-chain symmetry.

## What this checks

`scripts/sutando-migrate.sh` encodes destination paths for every workspace file
as bash strings in `CLASS_RULES`. The reader functions that consume those files
(`personal_path`, `personalPath`, `status_read_path`) have fixed resolution
chains. This lint verifies that no file that is READ via `personal_path()` in
source code would be CLASSIFIED as `rehome-state` (→ `state/`) by the migration
script — exactly the bug class from incident #1540.

## How it differs from the layer-1 test

`tests/migrate-reader-contract.test.py` (layer 1) checks a *hardcoded* set of
known personal-path files. This script instead *dynamically discovers* all
`personal_path("filename")` / `personalPath("filename")` call sites in Python
and TypeScript source, then validates those filenames against CLASS_RULES.

If a developer adds `personal_path("new-thing.json")` to the codebase and
accidentally classifies `new-thing.json|rehome-state` in CLASS_RULES, layer 1
misses it. Layer 3 catches it.

## No-op when sutando-migrate.sh is absent

The migration script ships on the staging-workspace-revamp branch (#1454) but
is not yet on main. This lint exits 0 (pass) when the file is absent, so CI
stays green on current main. Once #1454 lands, this lint becomes live.

Usage:
    python3 scripts/lint-class-rules.py          # scan whole tree
    python3 scripts/lint-class-rules.py --check  # same (alias for CI)
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

REPO = Path(subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    capture_output=True, text=True, check=True
).stdout.strip())
MIGRATE_SH = REPO / "scripts" / "sutando-migrate.sh"


# ---------------------------------------------------------------------------
# CLASS_RULES parser (mirrors tests/migrate-reader-contract.test.py)
# ---------------------------------------------------------------------------

def parse_class_rules(script: Path) -> list[tuple[str, str]]:
    """Extract (glob, class) pairs from CLASS_RULES=( ... ) in the migrate script."""
    text = script.read_text()
    m = re.search(r'CLASS_RULES=\(\s*(.*?)\n\)', text, re.DOTALL)
    if not m:
        print(
            f"WARN: Could not find CLASS_RULES=(...) in {script.relative_to(REPO)} "
            "— parser may need updating if the array syntax changed."
        )
        return []
    body = m.group(1)
    rules: list[tuple[str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m2 = re.match(r'"([^|"]+)\|([^"]+)"', line)
        if m2:
            rules.append((m2.group(1).strip(), m2.group(2).strip()))
    return rules


def classify_file(filename: str, rules: list[tuple[str, str]]) -> str | None:
    """Return the class for `filename` (first-match wins), or None."""
    for glob, cls in rules:
        if fnmatch(filename, glob):
            return cls
    return None


# ---------------------------------------------------------------------------
# Discover personal_path() callers in Python source
# ---------------------------------------------------------------------------

def extract_personal_path_args_py(src_dir: Path) -> dict[str, list[str]]:
    """Walk *.py under src_dir; extract literal string args to personal_path().

    Returns: {filename_arg: [caller_file, ...]}
    """
    results: dict[str, list[str]] = {}

    for pyfile in sorted(src_dir.rglob("*.py")):
        try:
            tree = ast.parse(pyfile.read_text(errors="replace"))
        except SyntaxError:
            continue
        try:
            rel = str(pyfile.relative_to(REPO))
        except ValueError:
            rel = str(pyfile)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name not in ("personal_path", "shared_personal_path"):
                continue
            if not node.args:
                continue
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                arg = first_arg.value
                results.setdefault(arg, []).append(rel)

    return results


# ---------------------------------------------------------------------------
# Discover personalPath() callers in TypeScript source
# ---------------------------------------------------------------------------

def extract_personal_path_args_ts(src_dir: Path) -> dict[str, list[str]]:
    """Walk *.ts under src_dir; extract literal string args to personalPath().

    Uses regex — TS AST parsing would need a third-party dep.
    Returns: {filename_arg: [caller_file, ...]}
    """
    results: dict[str, list[str]] = {}
    # Matches personalPath('file') or personalPath("file")
    pattern = re.compile(r'\bpersonalPath\(\s*[\'"]([^\'"]+)[\'"]')

    for tsfile in sorted(src_dir.rglob("*.ts")):
        if "node_modules" in str(tsfile):
            continue
        try:
            rel = str(tsfile.relative_to(REPO))
        except ValueError:
            rel = str(tsfile)
        try:
            text = tsfile.read_text(errors="replace")
        except OSError:
            continue
        for m in pattern.finditer(text):
            arg = m.group(1)
            results.setdefault(arg, []).append(rel)

    return results


# ---------------------------------------------------------------------------
# Main lint
# ---------------------------------------------------------------------------

REHOME_TO_STATE_CLASSES = {"rehome-state"}


def run_lint() -> int:
    """Return 0 on pass, 1 on failure."""
    if not MIGRATE_SH.exists():
        print(
            f"SKIP: {MIGRATE_SH.relative_to(REPO)} not found — "
            "lint activates when scripts/sutando-migrate.sh is present (post-#1454)."
        )
        return 0

    rules = parse_class_rules(MIGRATE_SH)
    if not rules:
        print("SKIP: CLASS_RULES is empty or unparseable.")
        return 0

    # Discover callers
    src_dir = REPO / "src"
    skills_dir = REPO / "skills"

    py_callers: dict[str, list[str]] = {}
    for search_dir in [src_dir, skills_dir]:
        if search_dir.exists():
            for fname, files in extract_personal_path_args_py(search_dir).items():
                py_callers.setdefault(fname, []).extend(files)

    ts_callers: dict[str, list[str]] = {}
    for search_dir in [src_dir, skills_dir]:
        if search_dir.exists():
            for fname, files in extract_personal_path_args_ts(search_dir).items():
                ts_callers.setdefault(fname, []).extend(files)

    all_callers = {**py_callers}
    for fname, files in ts_callers.items():
        all_callers.setdefault(fname, []).extend(files)

    if not all_callers:
        print("INFO: No personal_path / personalPath callers found — nothing to check.")
        return 0

    failures: list[str] = []

    for filename, caller_files in sorted(all_callers.items()):
        cls = classify_file(filename, rules)
        if cls in REHOME_TO_STATE_CLASSES:
            failures.append(
                f"  {filename!r} → class={cls!r} (→ state/) "
                f"but personal_path() never looks in state/.\n"
                f"  Callers: {', '.join(sorted(set(caller_files)))}"
            )
        elif cls is None:
            # Not in CLASS_RULES — will hit the catchall. Not a reader-contract
            # violation, but worth noting so new personal-path files get explicit rules.
            print(f"  NOTE: {filename!r} has no explicit CLASS_RULES entry (hits catchall). "
                  f"Callers: {', '.join(sorted(set(caller_files)))}")

    if failures:
        print("FAIL: personal_path files classified into unreachable destination:")
        for f in failures:
            print(f)
        print(
            "\nFix: change the CLASS_RULES entry to 'newest-mtime' or another class "
            "that keeps the file at the workspace root, so personal_path() can resolve it."
        )
        return 1

    n = len(all_callers)
    print(f"PASS: {n} personal_path file(s) all have compatible CLASS_RULES classifications.")
    return 0


if __name__ == "__main__":
    sys.exit(run_lint())
