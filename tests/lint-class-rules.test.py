"""Tests for scripts/lint-class-rules.py — layer 3 of #1543.

Structural assertions that the lint script:
1. Exists and is executable as a standalone script.
2. Exits 0 when sutando-migrate.sh is absent (no-op guard).
3. Dynamically scans Python/TS source for personal_path callers.
4. Fails correctly when a personal_path file is classified rehome-state.
5. Passes when personal_path files have root-keeping classifications.

Uses synthetic CLASS_RULES strings so the test doesn't depend on
sutando-migrate.sh being present on this branch.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LINT_SCRIPT = REPO / "scripts" / "lint-class-rules.py"
SRC = LINT_SCRIPT.read_text()


# ---------------------------------------------------------------------------
# Structural checks — script exists and has the key functions
# ---------------------------------------------------------------------------

class TestLintClassRulesStructure(unittest.TestCase):

    def test_script_exists(self):
        self.assertTrue(LINT_SCRIPT.exists(), "scripts/lint-class-rules.py must exist")

    def test_parse_class_rules_function_defined(self):
        self.assertIn("def parse_class_rules(", SRC,
                      "parse_class_rules() function must be defined")

    def test_extract_personal_path_args_py_defined(self):
        self.assertIn("def extract_personal_path_args_py(", SRC,
                      "extract_personal_path_args_py() must be defined")

    def test_extract_personal_path_args_ts_defined(self):
        self.assertIn("def extract_personal_path_args_ts(", SRC,
                      "extract_personal_path_args_ts() must be defined")

    def test_run_lint_function_defined(self):
        self.assertIn("def run_lint(", SRC, "run_lint() must be defined")

    def test_rehome_to_state_classes_constant_defined(self):
        self.assertIn("REHOME_TO_STATE_CLASSES", SRC,
                      "REHOME_TO_STATE_CLASSES constant must be defined")
        self.assertIn('"rehome-state"', SRC,
                      "rehome-state must be in REHOME_TO_STATE_CLASSES")

    def test_no_op_when_migrate_sh_absent(self):
        self.assertIn("not found", SRC,
                      "must emit a 'not found' message when migrate.sh is absent")
        self.assertIn("return 0", SRC,
                      "must return 0 (pass) when migrate.sh is absent")

    def test_ci_step_added_to_workflow(self):
        ci_yml = (REPO / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("lint-class-rules.py", ci_yml,
                      "CI workflow must include lint-class-rules.py step")


# ---------------------------------------------------------------------------
# Functional tests using the script's internal functions directly
# ---------------------------------------------------------------------------

# Import the lint script's functions by exec-ing it in a namespace
# Provide __file__ so the REPO Path(...) at module level works correctly.
_ns: dict = {"__file__": str(LINT_SCRIPT)}
exec(compile(SRC, str(LINT_SCRIPT), "exec"), _ns)  # noqa: S102
_parse_class_rules = _ns["parse_class_rules"]
_classify_file = _ns["classify_file"]
_extract_personal_path_args_py = _ns["extract_personal_path_args_py"]
_extract_personal_path_args_ts = _ns["extract_personal_path_args_ts"]


class TestParseClassRules(unittest.TestCase):

    def _make_migrate_sh(self, rules: list[str]) -> Path:
        body = "\n".join(f'    "{r}"' for r in rules)
        content = f"""#!/bin/bash
# test script
CLASS_RULES=(
{body}
)
"""
        p = Path(tempfile.mktemp(suffix=".sh"))
        p.write_text(content)
        return p

    def test_parses_simple_rules(self):
        f = self._make_migrate_sh([
            "stand-identity.json|newest-mtime",
            "state/*.json|structural",
            "*|quarantine-unknown",
        ])
        try:
            rules = _parse_class_rules(f)
            self.assertEqual(rules, [
                ("stand-identity.json", "newest-mtime"),
                ("state/*.json", "structural"),
                ("*", "quarantine-unknown"),
            ])
        finally:
            f.unlink(missing_ok=True)

    def test_ignores_comment_lines(self):
        f = self._make_migrate_sh([
            "# this is a comment",
            "stand-identity.json|newest-mtime",
        ])
        try:
            rules = _parse_class_rules(f)
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0], ("stand-identity.json", "newest-mtime"))
        finally:
            f.unlink(missing_ok=True)

    def test_classify_first_match_wins(self):
        rules = [
            ("stand-identity.json", "newest-mtime"),
            ("stand-identity.json", "rehome-state"),
            ("*", "quarantine-unknown"),
        ]
        cls = _classify_file("stand-identity.json", rules)
        self.assertEqual(cls, "newest-mtime")

    def test_classify_rehome_state_detected(self):
        rules = [
            ("stand-identity.json", "rehome-state"),
            ("*", "quarantine-unknown"),
        ]
        cls = _classify_file("stand-identity.json", rules)
        self.assertEqual(cls, "rehome-state",
                         "should detect rehome-state classification for personal_path file")


class TestExtractCallers(unittest.TestCase):

    def test_py_extracts_string_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "test.py"
            f.write_text(
                "from util_paths import personal_path\n"
                'si = personal_path("stand-identity.json")\n'
                'pq = personal_path("pending-questions.md")\n'
            )
            result = _extract_personal_path_args_py(Path(tmp))
            self.assertIn("stand-identity.json", result)
            self.assertIn("pending-questions.md", result)

    def test_py_ignores_variable_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "test.py"
            f.write_text(
                'personal_path(some_variable)\n'
                'personal_path("literal.json")\n'
            )
            result = _extract_personal_path_args_py(Path(tmp))
            self.assertIn("literal.json", result)
            self.assertNotIn("some_variable", result)

    def test_ts_extracts_string_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "test.ts"
            f.write_text(
                "import { personalPath } from './util_paths.js';\n"
                "const si = personalPath('stand-identity.json');\n"
            )
            result = _extract_personal_path_args_ts(Path(tmp))
            self.assertIn("stand-identity.json", result)

    def test_py_attribute_call_extracts_arg(self):
        """Attribute-style call (util.personal_path("x")) is extracted — line 111-112."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "test.py"
            f.write_text('util.personal_path("attr-style.json")\n')
            result = _extract_personal_path_args_py(Path(tmp))
            self.assertIn("attr-style.json", result)

    def test_py_skips_non_personal_path_calls(self):
        """Non-matching function names skip the arg extraction — line 114."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "test.py"
            f.write_text('some_other_func("irrelevant.json")\n')
            result = _extract_personal_path_args_py(Path(tmp))
            self.assertNotIn("irrelevant.json", result)

    def test_py_skips_call_with_no_args(self):
        """personal_path() with no arguments does not crash — line 116."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "test.py"
            f.write_text('personal_path()\n')
            result = _extract_personal_path_args_py(Path(tmp))
            self.assertEqual(result, {})

    def test_py_skips_syntax_error_file(self):
        """SyntaxError in a source file is swallowed — lines 98-99."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "broken.py"
            f.write_text('def oops(\n')  # unclosed paren → SyntaxError
            result = _extract_personal_path_args_py(Path(tmp))
            self.assertEqual(result, {})

    def test_ts_skips_node_modules(self):
        """TS files inside node_modules are skipped — line 141."""
        with tempfile.TemporaryDirectory() as tmp:
            nm = Path(tmp) / "node_modules" / "pkg"
            nm.mkdir(parents=True)
            f = nm / "util.ts"
            f.write_text("const x = personalPath('in-node-modules.json');\n")
            result = _extract_personal_path_args_ts(Path(tmp))
            self.assertNotIn("in-node-modules.json", result)

    def test_ts_skips_unreadable_file(self):
        """OSError reading a TS file is swallowed — lines 148-149."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "locked.ts"
            f.write_text("const x = personalPath('locked.json');\n")
            f.chmod(0o000)
            try:
                result = _extract_personal_path_args_ts(Path(tmp))
                self.assertNotIn("locked.json", result)
            finally:
                f.chmod(0o644)  # restore so tmpdir cleanup works

    def test_parse_class_rules_warns_on_missing_array(self):
        """parse_class_rules prints WARN and returns [] when no CLASS_RULES block — lines 59,63."""
        import io
        from contextlib import redirect_stdout
        orig_repo = _ns["REPO"]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            p = tmp_path / "migrate.sh"
            p.write_text("#!/bin/bash\n# no CLASS_RULES here\necho hello\n")
            _ns["REPO"] = tmp_path
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    rules = _parse_class_rules(p)
                self.assertEqual(rules, [])
                self.assertIn("WARN", buf.getvalue())
            finally:
                _ns["REPO"] = orig_repo

    def test_parse_class_rules_skips_blank_line_in_rules(self):
        """Blank lines inside CLASS_RULES body are skipped — line 69 (continue)."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "migrate.sh"
            f.write_text(
                "#!/bin/bash\n"
                "CLASS_RULES=(\n"
                "\n"  # blank line inside the array
                '    "stand-identity.json|newest-mtime"\n'
                ")\n"
            )
            rules = _parse_class_rules(f)
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0], ("stand-identity.json", "newest-mtime"))


class TestRunLintNoOp(unittest.TestCase):

    def test_exits_zero_on_clean_checkout(self):
        """The script must exit 0 on a clean checkout (PASS or SKIP — never 1)."""
        result = subprocess.run(
            [sys.executable, str(LINT_SCRIPT)],
            capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0,
                         f"lint script must exit 0 on main:\n{result.stdout}\n{result.stderr}")
        passed_or_skipped = "PASS" in result.stdout or "SKIP" in result.stdout
        self.assertTrue(passed_or_skipped,
                        f"output must contain PASS or SKIP on main:\n{result.stdout}")


# ---------------------------------------------------------------------------
# Direct run_lint() coverage — calls the function with patched module globals
# so every branch in run_lint()'s body is instrumented by coverage.py.
#
# Strategy: patch _ns["MIGRATE_SH"] and _ns["REPO"] to point at temporary
# directories with synthetic content, then call _ns["run_lint"]() directly.
# ---------------------------------------------------------------------------

_run_lint = _ns["run_lint"]


class TestRunLintDirect(unittest.TestCase):
    """Direct run_lint() coverage using patched module globals.

    All tests build a fake REPO inside a tempdir so that
    MIGRATE_SH.relative_to(REPO) (used in run_lint's print statements) works.
    """

    def _make_fake_repo(self, rules: list[str] | None,
                        src_py_files: dict[str, str] | None = None,
                        src_ts_files: dict[str, str] | None = None) -> tuple[Path, Path]:
        """Return (tmp_repo, migrate_sh_path). If rules is None, migrate_sh is absent."""
        tmp_repo = Path(tempfile.mkdtemp(prefix="lint-test-repo-"))
        scripts_dir = tmp_repo / "scripts"
        scripts_dir.mkdir()
        src_dir = tmp_repo / "src"
        src_dir.mkdir()
        (tmp_repo / "skills").mkdir()
        for name, content in (src_py_files or {}).items():
            (src_dir / name).write_text(content)
        for name, content in (src_ts_files or {}).items():
            (src_dir / name).write_text(content)
        migrate_sh = tmp_repo / "scripts" / "sutando-migrate.sh"
        if rules is not None:
            body = "\n".join(f'    "{r}"' for r in rules)
            migrate_sh.write_text(f"#!/bin/bash\nCLASS_RULES=(\n{body}\n)\n")
        return tmp_repo, migrate_sh

    def _patch_and_run(self, tmp_repo: Path, migrate_sh: Path) -> tuple[int, str]:
        """Patch MIGRATE_SH and REPO, call run_lint(), return (returncode, stdout)."""
        import io
        from contextlib import redirect_stdout
        orig_migrate = _ns["MIGRATE_SH"]
        orig_repo = _ns["REPO"]
        buf = io.StringIO()
        try:
            _ns["MIGRATE_SH"] = migrate_sh
            _ns["REPO"] = tmp_repo
            with redirect_stdout(buf):
                rc = _run_lint()
        finally:
            _ns["MIGRATE_SH"] = orig_migrate
            _ns["REPO"] = orig_repo
        return rc, buf.getvalue()

    def test_skip_when_migrate_sh_absent(self):
        """run_lint returns 0 and prints SKIP when sutando-migrate.sh is missing."""
        tmp_repo, migrate_sh = self._make_fake_repo(None)  # rules=None → no file written
        rc, out = self._patch_and_run(tmp_repo, migrate_sh)
        self.assertEqual(rc, 0)
        self.assertIn("SKIP", out)

    def test_skip_when_class_rules_empty(self):
        """run_lint returns 0 when CLASS_RULES is empty."""
        tmp_repo, migrate_sh = self._make_fake_repo([])
        rc, out = self._patch_and_run(tmp_repo, migrate_sh)
        self.assertEqual(rc, 0)
        self.assertIn("SKIP", out)

    def test_pass_when_no_personal_path_callers(self):
        """run_lint returns 0 when src/ has no personal_path calls."""
        tmp_repo, migrate_sh = self._make_fake_repo(
            ["stand-identity.json|newest-mtime"],
            src_py_files={"noop.py": "x = 1\n"}
        )
        rc, _ = self._patch_and_run(tmp_repo, migrate_sh)
        self.assertEqual(rc, 0)

    def test_pass_when_caller_has_compatible_classification(self):
        """run_lint returns 0 when personal_path file is newest-mtime (workspace root)."""
        tmp_repo, migrate_sh = self._make_fake_repo(
            ["stand-identity.json|newest-mtime", "*|quarantine-unknown"],
            src_py_files={"reader.py": 'personal_path("stand-identity.json")\n'}
        )
        rc, out = self._patch_and_run(tmp_repo, migrate_sh)
        self.assertEqual(rc, 0)
        self.assertIn("PASS", out)

    def test_fail_when_caller_file_classified_rehome_state(self):
        """run_lint returns 1 when a personal_path file is classified rehome-state."""
        tmp_repo, migrate_sh = self._make_fake_repo(
            ["stand-identity.json|rehome-state", "*|quarantine-unknown"],
            src_py_files={"reader.py": 'personal_path("stand-identity.json")\n'}
        )
        rc, out = self._patch_and_run(tmp_repo, migrate_sh)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)

    def test_note_when_caller_file_not_in_class_rules(self):
        """run_lint returns 0 and emits NOTE when file has no matching rule (cls is None)."""
        tmp_repo, migrate_sh = self._make_fake_repo(
            # Only a specific rule that does NOT match "unlisted-thing.json"
            ["stand-identity.json|newest-mtime"],
            src_py_files={"reader.py": 'personal_path("unlisted-thing.json")\n'}
        )
        rc, out = self._patch_and_run(tmp_repo, migrate_sh)
        self.assertEqual(rc, 0)
        self.assertIn("NOTE", out)

    def test_ts_caller_contributes_to_pass(self):
        """run_lint handles TypeScript callers (personalPath) in addition to Python."""
        tmp_repo, migrate_sh = self._make_fake_repo(
            ["ts-state.json|newest-mtime", "*|quarantine-unknown"],
            src_ts_files={"reader.ts": "const x = personalPath('ts-state.json');\n"}
        )
        rc, out = self._patch_and_run(tmp_repo, migrate_sh)
        self.assertEqual(rc, 0)
        self.assertIn("PASS", out)


if __name__ == "__main__":
    unittest.main()
