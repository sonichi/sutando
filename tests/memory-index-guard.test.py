#!/usr/bin/env python3
"""PreToolUse memory-index-guard: an Edit or Write to MEMORY.md, run via any
caller, is gated on memory-index-budget.py regardless of whether the calling
skill remembers to chain step 7.5 itself (hooks/memory-index-guard.py).

Run:  python3 tests/memory-index-guard.test.py
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "memory-index-guard.py"
_spec = importlib.util.spec_from_file_location("mig", HOOK)
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)


def _stub(tmpdir, rc, stdout=""):
    p = Path(tmpdir) / "budget.py"
    p.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"print({stdout!r})\n"
        f"sys.exit({rc})\n"
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


class IsMemoryIndex(unittest.TestCase):
    def test_matches_memory_md_by_basename(self):
        self.assertTrue(G._is_memory_index("/a/b/MEMORY.md"))
        self.assertTrue(G._is_memory_index("MEMORY.md"))

    def test_does_not_match_other_memory_files(self):
        self.assertFalse(G._is_memory_index("/a/b/MEMORY-archive.md"))
        self.assertFalse(G._is_memory_index("/a/b/feedback_something.md"))
        self.assertFalse(G._is_memory_index(""))
        self.assertFalse(G._is_memory_index(None))


class AdditionForEdit(unittest.TestCase):
    def test_a_growing_edit_is_the_addition(self):
        addition = G._addition_for("Edit", {
            "file_path": "MEMORY.md", "old_string": "x", "new_string": "x\n- new row"})
        self.assertEqual(addition, "x\n- new row")

    def test_a_shrink_is_not_checked(self):
        addition = G._addition_for("Edit", {
            "file_path": "MEMORY.md", "old_string": "a long old string here", "new_string": "short"})
        self.assertIsNone(addition)

    def test_a_non_memory_file_is_not_checked(self):
        addition = G._addition_for("Edit", {
            "file_path": "feedback_something.md", "old_string": "x", "new_string": "x\n- new row"})
        self.assertIsNone(addition)

    def test_equal_length_is_not_a_growth(self):
        addition = G._addition_for("Edit", {
            "file_path": "MEMORY.md", "old_string": "abcde", "new_string": "fghij"})
        self.assertIsNone(addition)


class AdditionForWrite(unittest.TestCase):
    def test_a_fresh_file_treats_the_whole_content_as_the_addition(self):
        with tempfile.TemporaryDirectory() as td:
            target = str(Path(td) / "MEMORY.md")  # does not exist yet
            addition = G._addition_for("Write", {"file_path": target, "content": "- brand new row"})
            self.assertEqual(addition, "- brand new row")

    def test_an_existing_file_diffs_old_vs_new(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "MEMORY.md"
            target.write_text("- row one\n- row two\n")
            addition = G._addition_for("Write", {
                "file_path": str(target), "content": "- row one\n- row two\n- row three\n"})
            self.assertEqual(addition, "- row three")

    def test_a_pure_reorder_with_no_new_lines_is_not_checked(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "MEMORY.md"
            target.write_text("- row one\n- row two\n")
            addition = G._addition_for("Write", {
                "file_path": str(target), "content": "- row two\n- row one\n"})
            self.assertIsNone(addition)

    def test_empty_content_is_not_checked(self):
        addition = G._addition_for("Write", {"file_path": "MEMORY.md", "content": ""})
        self.assertIsNone(addition)


class CheckMemoryWrite(unittest.TestCase):
    def test_safe_allows(self):
        with tempfile.TemporaryDirectory() as td:
            G.BUDGET_CHECK = _stub(td, 0, "safe")
            found = G.check_memory_write("Edit", {
                "file_path": "MEMORY.md", "old_string": "x", "new_string": "x\n- new row"})
            self.assertIsNone(found)

    def test_a_real_refuse_denies_with_a_reason(self):
        with tempfile.TemporaryDirectory() as td:
            G.BUDGET_CHECK = _stub(td, 1, "REFUSE -- drops feedback_x.md")
            found = G.check_memory_write("Edit", {
                "file_path": "MEMORY.md", "old_string": "x", "new_string": "x\n- new row"})
            self.assertIsNotNone(found)
            self.assertIn("drops feedback_x.md", found)

    def test_cannot_answer_fails_open(self):
        with tempfile.TemporaryDirectory() as td:
            G.BUDGET_CHECK = _stub(td, 2, "cannot answer")
            found = G.check_memory_write("Edit", {
                "file_path": "MEMORY.md", "old_string": "x", "new_string": "x\n- new row"})
            self.assertIsNone(found)

    def test_a_shrink_never_reaches_the_script(self):
        with tempfile.TemporaryDirectory() as td:
            G.BUDGET_CHECK = _stub(td, 1, "would refuse if called")
            found = G.check_memory_write("Edit", {
                "file_path": "MEMORY.md", "old_string": "a long old string", "new_string": "short"})
            self.assertIsNone(found)


class EndToEnd(unittest.TestCase):
    def test_a_non_edit_write_tool_is_ignored(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}),
            capture_output=True, text=True,
        )
        self.assertNotIn('"permissionDecision"', r.stdout)

    def test_a_non_memory_edit_is_ignored(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Edit", "tool_input": {
                "file_path": "notes/whatever.md", "old_string": "x", "new_string": "x" * 5000}}),
            capture_output=True, text=True,
        )
        self.assertNotIn('"permissionDecision"', r.stdout)

    def test_the_override_env_var_bypasses_everything(self):
        env = dict(os.environ)
        env["SUTANDO_ALLOW_UNGATED_MEMORY_WRITE"] = "1"
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Edit", "tool_input": {
                "file_path": "/whatever/MEMORY.md", "old_string": "x", "new_string": "x" * 50000}}),
            capture_output=True, text=True, env=env,
        )
        self.assertNotIn('"permissionDecision"', r.stdout)

    def test_live_against_the_real_memory_index_budget_script(self):
        """One real end-to-end pass against the actual memory-index-budget.py
        (not a stub) confirming the wiring works, not just the mocked unit
        tests. Uses a throwaway MEMORY.md so this never touches the real one."""
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td) / "MEMORY.md"
            mem.write_text("# Memory Index\n- [x](feedback_x.md) — a row\n")
            r = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps({"tool_name": "Write", "tool_input": {
                    "file_path": str(mem),
                    "content": "# Memory Index\n- [x](feedback_x.md) — a row\n- [y](feedback_y.md) — another\n",
                }}),
                capture_output=True, text=True,
            )
            # A small, well-under-budget addition to a tiny index must not deny.
            self.assertNotIn('"permissionDecision": "deny"', r.stdout)


if __name__ == "__main__":
    unittest.main()
