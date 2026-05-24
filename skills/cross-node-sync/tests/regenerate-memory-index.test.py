#!/usr/bin/env python3
"""
Tests for skills/cross-node-sync/scripts/regenerate-memory-index.py

Coverage:
  parse_frontmatter  — no frontmatter, valid YAML block, multiple keys,
                       missing key falls back to stem
  collect_entries    — empty dir, MEMORY.md excluded, normal file included,
                       file without frontmatter uses stem as name
  render             — empty list, sorted alphabetically, long desc truncated,
                       short desc passthrough, header line format
  main               — dir not found (exit 0), dry-run output, writes file,
                       clobber guard (0 entries + non-trivial MEMORY.md)

Run: python3 skills/cross-node-sync/tests/regenerate-memory-index.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "regenerate_memory_index",
    REPO / "skills" / "cross-node-sync" / "scripts" / "regenerate-memory-index.py",
)
rmi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rmi)

TMPDIR = Path(tempfile.mkdtemp())


def _write_memory_file(dir_: Path, name: str, content: str) -> Path:
    p = dir_ / name
    p.write_text(content)
    return p


def _with_frontmatter(fm_dict: dict, body: str = "") -> str:
    fm = "\n".join(f"{k}: {v}" for k, v in fm_dict.items())
    return f"---\n{fm}\n---\n{body}"


# ── parse_frontmatter ─────────────────────────────────────────────────────────

class TestParseFrontmatter(unittest.TestCase):
    def test_no_frontmatter_returns_empty(self):
        result = rmi.parse_frontmatter("just text here")
        self.assertEqual(result, {})

    def test_valid_frontmatter_parsed(self):
        text = _with_frontmatter({"name": "my-memory", "description": "A test memory"})
        result = rmi.parse_frontmatter(text)
        self.assertEqual(result["name"], "my-memory")
        self.assertEqual(result["description"], "A test memory")

    def test_multiple_keys(self):
        text = _with_frontmatter({
            "name": "test",
            "description": "desc",
            "type": "user",
        })
        result = rmi.parse_frontmatter(text)
        self.assertEqual(result["type"], "user")
        self.assertEqual(len(result), 3)

    def test_colon_in_value_preserved(self):
        text = "---\ndescription: key: value here\n---\n"
        result = rmi.parse_frontmatter(text)
        self.assertIn("key: value here", result["description"])

    def test_empty_frontmatter_block(self):
        result = rmi.parse_frontmatter("---\n---\n")
        self.assertEqual(result, {})


# ── collect_entries ───────────────────────────────────────────────────────────

class TestCollectEntries(unittest.TestCase):
    def setUp(self):
        self.memdir = Path(tempfile.mkdtemp())

    def test_empty_dir_returns_empty(self):
        result = rmi.collect_entries(self.memdir)
        self.assertEqual(result, [])

    def test_memory_md_excluded(self):
        _write_memory_file(self.memdir, "MEMORY.md", "# Memory index\n")
        result = rmi.collect_entries(self.memdir)
        self.assertEqual(result, [])

    def test_index_md_excluded(self):
        _write_memory_file(self.memdir, "INDEX.md", "# Index\n")
        result = rmi.collect_entries(self.memdir)
        self.assertEqual(result, [])

    def test_normal_file_included(self):
        content = _with_frontmatter({"name": "user-profile", "description": "Who the user is"})
        _write_memory_file(self.memdir, "user_profile.md", content)
        result = rmi.collect_entries(self.memdir)
        self.assertEqual(len(result), 1)
        name, fname, desc = result[0]
        self.assertEqual(name, "user-profile")
        self.assertEqual(fname, "user_profile.md")
        self.assertEqual(desc, "Who the user is")

    def test_file_without_frontmatter_uses_stem(self):
        _write_memory_file(self.memdir, "my_note.md", "just some text")
        result = rmi.collect_entries(self.memdir)
        self.assertEqual(len(result), 1)
        name, fname, desc = result[0]
        self.assertEqual(name, "my_note")
        self.assertEqual(desc, "")

    def test_multiple_files_all_included(self):
        for i in range(3):
            content = _with_frontmatter({"name": f"entry-{i}", "description": f"desc {i}"})
            _write_memory_file(self.memdir, f"entry_{i}.md", content)
        result = rmi.collect_entries(self.memdir)
        self.assertEqual(len(result), 3)


# ── render ────────────────────────────────────────────────────────────────────

class TestRender(unittest.TestCase):
    def test_empty_list_renders_newline(self):
        result = rmi.render([])
        self.assertEqual(result, "\n")

    def test_single_entry_format(self):
        result = rmi.render([("my-memory", "my_memory.md", "A short description")])
        self.assertIn("- [my-memory](my_memory.md) — A short description", result)

    def test_entries_sorted_alphabetically(self):
        entries = [
            ("zebra", "zebra.md", "z desc"),
            ("apple", "apple.md", "a desc"),
        ]
        result = rmi.render(entries)
        lines = [l for l in result.strip().splitlines() if l]
        self.assertTrue(lines[0].startswith("- [apple]"))
        self.assertTrue(lines[1].startswith("- [zebra]"))

    def test_long_desc_truncated_with_ellipsis(self):
        long_desc = "x" * 300
        result = rmi.render([("test", "test.md", long_desc)])
        line = result.strip()
        self.assertLessEqual(len(line), rmi.MAX_LINE)
        self.assertTrue(line.endswith("..."))

    def test_exact_length_desc_not_truncated(self):
        prefix = "- [n](n.md) — "
        budget = rmi.MAX_LINE - len(prefix)
        desc = "x" * budget
        result = rmi.render([("n", "n.md", desc)])
        line = result.strip()
        self.assertFalse(line.endswith("..."))

    def test_case_insensitive_sort(self):
        entries = [
            ("Beta", "beta.md", ""),
            ("alpha", "alpha.md", ""),
        ]
        result = rmi.render(entries)
        lines = [l for l in result.strip().splitlines() if l]
        self.assertTrue(lines[0].startswith("- [alpha]"))


# ── main ──────────────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):
    def _run_main(self, extra_args=None):
        """Run main() with given CLI args, return (exit_code, stdout, stderr)."""
        args = extra_args or []
        with patch("sys.stdout", new_callable=StringIO) as out, \
             patch("sys.stderr", new_callable=StringIO) as err:
            try:
                with patch("sys.argv", ["rmi"] + args):
                    rc = rmi.main()
            except SystemExit as e:
                rc = e.code
            return rc, out.getvalue(), err.getvalue()

    def test_missing_dir_exits_0(self):
        rc, out, err = self._run_main(["--memory-dir", str(TMPDIR / "nonexistent")])
        self.assertEqual(rc, 0)
        self.assertIn("not found", err)

    def test_dry_run_does_not_write_file(self):
        memdir = Path(tempfile.mkdtemp())
        content = _with_frontmatter({"name": "test", "description": "desc"})
        _write_memory_file(memdir, "test.md", content)
        target = memdir / "MEMORY.md"
        rc, out, err = self._run_main(["--memory-dir", str(memdir), "--dry-run"])
        self.assertFalse(target.exists())
        self.assertIn("would write", out)

    def test_normal_run_writes_memory_md(self):
        memdir = Path(tempfile.mkdtemp())
        content = _with_frontmatter({"name": "my-entry", "description": "A memory"})
        _write_memory_file(memdir, "entry.md", content)
        rc, out, err = self._run_main(["--memory-dir", str(memdir)])
        self.assertEqual(rc, 0)
        target = memdir / "MEMORY.md"
        self.assertTrue(target.exists())
        mem_text = target.read_text()
        self.assertIn("my-entry", mem_text)

    def test_clobber_guard_0_entries_existing_content(self):
        memdir = Path(tempfile.mkdtemp())
        target = memdir / "MEMORY.md"
        # Write a non-trivial MEMORY.md (with list items)
        target.write_text("# Memory\n- [entry](entry.md) — some description\n")
        # No .md files with frontmatter → 0 entries found
        rc, out, err = self._run_main(["--memory-dir", str(memdir)])
        self.assertEqual(rc, 1)
        self.assertIn("refusing to clobber", err)
        # File must not be overwritten
        self.assertIn("entry.md", target.read_text())

    def test_clobber_guard_dry_run_returns_1(self):
        memdir = Path(tempfile.mkdtemp())
        target = memdir / "MEMORY.md"
        target.write_text("# Memory\n- [entry](entry.md) — some description\n")
        rc, out, err = self._run_main(["--memory-dir", str(memdir), "--dry-run"])
        self.assertEqual(rc, 1)

    def test_normal_run_entry_count_in_output(self):
        memdir = Path(tempfile.mkdtemp())
        for i in range(3):
            content = _with_frontmatter({"name": f"e{i}", "description": f"desc {i}"})
            _write_memory_file(memdir, f"e{i}.md", content)
        rc, out, err = self._run_main(["--memory-dir", str(memdir)])
        self.assertIn("3", out)


if __name__ == "__main__":
    unittest.main()
