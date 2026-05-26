#!/usr/bin/env python3
"""Lint: ban process.cwd() / Path.cwd() / os.getcwd() outside canonical resolvers.

Prevents re-introducing the workspace-vs-repo bug class that the 2026-05-18
audit (PRs #815, #821, #832 … #859) closed across ~28 sites.

Closes (tracks) issue #863.

Run: python3 tests/cwd-lint.test.py
Exit: 0 on pass, 1 on fail.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# Python files: Path.cwd() and os.getcwd() are only allowed in these files.
PY_ALLOWLIST = {"workspace_default.py", "util_paths.py"}
# Matches `Path.cwd()` or `os.getcwd()` as actual code.
# Comments are stripped by _code_only() before this regex runs.
_PY_CWD_RE = re.compile(r"(?:Path\.cwd\(\)|os\.getcwd\(\))")
_PY_COMMENT_RE = re.compile(r"#.*$")

# TypeScript files: process.cwd() is only allowed in these files.
TS_ALLOWLIST = {"workspace_default.ts", "util_paths.ts"}
_TS_CWD_RE = re.compile(r"process\.cwd\(\)")
_TS_LINE_COMMENT_RE = re.compile(r"//.*$")


def _code_only(line: str, comment_re: re.Pattern) -> str:
    """Strip trailing line comment, return the code portion."""
    return comment_re.sub("", line)


def _scan_python() -> list[tuple[str, int, str]]:
    """Return (relpath, lineno, line) for Python CWD violations."""
    violations: list[tuple[str, int, str]] = []
    for py_file in sorted(SRC.glob("*.py")):
        if py_file.name in PY_ALLOWLIST:
            continue
        for lineno, raw_line in enumerate(py_file.read_text(errors="replace").splitlines(), 1):
            code = _code_only(raw_line, _PY_COMMENT_RE)
            if _PY_CWD_RE.search(code):
                violations.append((f"src/{py_file.name}", lineno, raw_line.strip()))
    return violations


def _scan_typescript() -> list[tuple[str, int, str]]:
    """Return (relpath, lineno, line) for TypeScript CWD violations."""
    violations: list[tuple[str, int, str]] = []
    for ts_file in sorted(SRC.glob("*.ts")):
        if ts_file.name in TS_ALLOWLIST:
            continue
        for lineno, raw_line in enumerate(ts_file.read_text(errors="replace").splitlines(), 1):
            code = _code_only(raw_line, _TS_LINE_COMMENT_RE)
            if _TS_CWD_RE.search(code):
                violations.append((f"src/{ts_file.name}", lineno, raw_line.strip()))
    return violations


class TestCwdLintPython(unittest.TestCase):
    def test_no_path_cwd_in_python_src(self):
        violations = _scan_python()
        if violations:
            lines = "\n".join(f"  {f}:{n}: {l}" for f, n, l in violations)
            self.fail(
                f"Path.cwd() / os.getcwd() found outside allowlist "
                f"({', '.join(PY_ALLOWLIST)}):\n{lines}\n"
                "Route workspace data through resolve_workspace() instead. See issue #863."
            )

    def test_allowlist_files_exist(self):
        for name in PY_ALLOWLIST:
            self.assertTrue((SRC / name).exists(), f"Allowlisted file not found: src/{name}")

    def test_workspace_default_py_uses_cwd(self):
        text = (SRC / "workspace_default.py").read_text()
        self.assertIn("cwd", text.lower(),
                      "workspace_default.py should reference cwd (sanity check that allowlist is meaningful)")

    def test_scan_returns_list(self):
        result = _scan_python()
        self.assertIsInstance(result, list)


class TestCwdLintTypeScript(unittest.TestCase):
    def test_no_process_cwd_in_typescript_src(self):
        violations = _scan_typescript()
        if violations:
            lines = "\n".join(f"  {f}:{n}: {l}" for f, n, l in violations)
            self.fail(
                f"process.cwd() found outside allowlist "
                f"({', '.join(TS_ALLOWLIST)}):\n{lines}\n"
                "Route workspace data through resolveWorkspace() instead. See issue #863."
            )

    def test_allowlist_files_exist(self):
        for name in TS_ALLOWLIST:
            self.assertTrue((SRC / name).exists(), f"Allowlisted file not found: src/{name}")

    def test_workspace_default_ts_uses_cwd(self):
        ts_path = SRC / "workspace_default.ts"
        self.assertTrue(ts_path.exists())
        text = ts_path.read_text()
        self.assertIn("cwd", text.lower(),
                      "workspace_default.ts should reference cwd (sanity check that allowlist is meaningful)")

    def test_scan_returns_list(self):
        result = _scan_typescript()
        self.assertIsInstance(result, list)


class TestCwdLintCommentStrip(unittest.TestCase):
    def test_python_comment_stripped_before_check(self):
        # A comment containing Path.cwd() should NOT be flagged
        line = "    # resolves relative to Path.cwd() which is wrong"
        code = _code_only(line, _PY_COMMENT_RE)
        self.assertFalse(_PY_CWD_RE.search(code),
                         "Comment-only CWD reference should not be flagged")

    def test_python_real_code_not_stripped(self):
        line = "    target = Path.cwd() / 'workspace'"
        code = _code_only(line, _PY_COMMENT_RE)
        self.assertTrue(_PY_CWD_RE.search(code),
                        "Actual Path.cwd() call should be flagged")

    def test_typescript_comment_stripped_before_check(self):
        line = "    // Old code used process.cwd() here"
        code = _code_only(line, _TS_LINE_COMMENT_RE)
        self.assertFalse(_TS_CWD_RE.search(code),
                         "Comment-only CWD reference should not be flagged")

    def test_typescript_real_code_not_stripped(self):
        line = "    const ws = process.cwd();"
        code = _code_only(line, _TS_LINE_COMMENT_RE)
        self.assertTrue(_TS_CWD_RE.search(code),
                        "Actual process.cwd() call should be flagged")


if __name__ == "__main__":
    unittest.main(verbosity=2)
