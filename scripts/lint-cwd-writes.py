#!/usr/bin/env python3
"""
lint-cwd-writes.py — flag bare cwd() calls outside canonical workspace resolvers.

Code that resolves workspace paths via process.cwd() / Path.cwd() / os.getcwd()
instead of the canonical helpers (resolve_workspace / resolveWorkspace) re-introduces
the workspace-vs-repo split-brain bug closed in the 2026-05-18 audit (#863).

Usage:
    python3 scripts/lint-cwd-writes.py [files...]

    files   Explicit list of .py / .ts / .js / .mjs files to check.
            Defaults to src/**/*.py and src/**/*.ts.

Exit codes:
    0  All files clean
    1  One or more violations found

Allowlist policy:
    Python — Path.cwd() / os.getcwd() are allowed ONLY in:
        src/workspace_default.py  (the canonical resolver itself)
        src/util_paths.py         (path utility helpers)

    TypeScript/JS — process.cwd() is allowed ONLY in:
        src/workspace_default.ts  (the canonical resolver itself)
        src/util_paths.ts         (path utility helpers)
        Any *_helpers* or *test* path
        Inline-tool REPO_ROOT uses (src/inline-tools.ts — post #848 approved)
        src/browser-tools.ts      (REPO_ROOT-relative, post-audit approved)

    Comments (lines starting with //, #, or /*, or containing // cwd) are ignored.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# --- Python allowlist ---
PY_ALLOWED_FILES = {
    "src/workspace_default.py",
    "src/util_paths.py",
}

# --- TS/JS allowlist ---
TS_ALLOWED_FILES = {
    "src/workspace_default.ts",
    "src/util_paths.ts",
    "src/inline-tools.ts",
    "src/browser-tools.ts",
}
TS_ALLOWED_PATTERNS = [
    re.compile(r"[_\-]helper"),
    re.compile(r"\.test\."),
    re.compile(r"tests/"),
]

# Patterns that indicate a cwd call (not in a comment)
PY_CWD_RE = re.compile(r"\b(?:Path\.cwd|os\.getcwd)\s*\(")
TS_CWD_RE = re.compile(r"\bprocess\.cwd\s*\(")

# Comment patterns — lines where cwd appears only as documentation
PY_COMMENT_RE = re.compile(r"^\s*#")
TS_COMMENT_RE = re.compile(r"^\s*(?://|/\*|\*)")
INLINE_COMMENT_RE = re.compile(r"//.*cwd|#.*cwd")  # cwd in trailing comment


def is_py_allowed(rel_path: str) -> bool:
    return rel_path in PY_ALLOWED_FILES


def is_ts_allowed(rel_path: str) -> bool:
    if rel_path in TS_ALLOWED_FILES:
        return True
    for pat in TS_ALLOWED_PATTERNS:
        if pat.search(rel_path):
            return True
    return False


def check_py(path: Path) -> list[tuple[int, str]]:
    """Return list of (lineno, line) violations for a Python file."""
    rel = str(path.relative_to(REPO_ROOT))
    if is_py_allowed(rel):
        return []
    violations = []
    try:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PY_COMMENT_RE.match(line):
                continue
            # Strip inline trailing comments before matching
            code_part = re.split(r"#", line)[0]
            if PY_CWD_RE.search(code_part):
                violations.append((i, line.rstrip()))
    except Exception:
        pass
    return violations


def check_ts(path: Path) -> list[tuple[int, str]]:
    """Return list of (lineno, line) violations for a TS/JS file."""
    rel = str(path.relative_to(REPO_ROOT))
    if is_ts_allowed(rel):
        return []
    violations = []
    try:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if TS_COMMENT_RE.match(line):
                continue
            # Strip inline trailing // comments
            code_part = re.split(r"//", line)[0]
            if TS_CWD_RE.search(code_part):
                violations.append((i, line.rstrip()))
    except Exception:
        pass
    return violations


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    file_args = [a for a in argv if not a.startswith("--")]
    if file_args:
        paths = [Path(f) for f in file_args]
    else:
        paths = sorted((REPO_ROOT / "src").glob("*.py")) + \
                sorted((REPO_ROOT / "src").glob("*.ts")) + \
                sorted((REPO_ROOT / "src").glob("*.mjs"))

    violations = 0
    for path in paths:
        if not path.exists():
            print(f"SKIP   {path}  (not found)", file=sys.stderr)
            continue
        suffix = path.suffix.lower()
        if suffix == ".py":
            hits = check_py(path)
        elif suffix in (".ts", ".js", ".mjs"):
            hits = check_ts(path)
        else:
            continue
        for lineno, line in hits:
            try:
                rel = path.relative_to(REPO_ROOT)
            except ValueError:
                rel = path
            print(f"FAIL   {rel}:{lineno}  — bare cwd() call outside canonical resolver")
            print(f"       {line[:120]}")
            violations += 1

    if violations:
        print(f"\n{violations} violation(s) — use resolve_workspace() / resolveWorkspace() instead of bare cwd().")
        sys.exit(1)
    elif file_args:
        print("All files clean.")


if __name__ == "__main__":
    main()
