#!/usr/bin/env python3
"""Tests for src/obsidian-mirror.py and skills/obsidian-vault/scripts/dream.py"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import importlib.util

def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

mirror = _load("obsidian_mirror", REPO / "src" / "obsidian-mirror.py")

PASS = 0
FAIL = 0

def ok(label):
    global PASS
    PASS += 1
    print(f"  ✓ {label}")

def fail(label, reason=""):
    global FAIL
    FAIL += 1
    print(f"  ✗ {label}{' — ' + reason if reason else ''}")


class TestOptInGate(unittest.TestCase):
    def test_force_bypasses_gate(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(mirror._opt_in_gate(force=True))

    def test_env_var_enables(self):
        with patch.dict(os.environ, {"SUTANDO_OBSIDIAN_MIRROR": "1"}):
            self.assertTrue(mirror._opt_in_gate(force=False))

    def test_missing_env_blocks(self):
        env = {k: v for k, v in os.environ.items() if k != "SUTANDO_OBSIDIAN_MIRROR"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(mirror._opt_in_gate(force=False))

    def test_env_zero_blocks(self):
        with patch.dict(os.environ, {"SUTANDO_OBSIDIAN_MIRROR": "0"}):
            self.assertFalse(mirror._opt_in_gate(force=False))


class TestParseSince(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(mirror._parse_since(None))

    def test_hours(self):
        result = mirror._parse_since("2h")
        self.assertIsInstance(result, datetime)
        diff = datetime.now(tz=timezone.utc) - result
        self.assertAlmostEqual(diff.total_seconds(), 7200, delta=5)

    def test_minutes(self):
        result = mirror._parse_since("30m")
        diff = datetime.now(tz=timezone.utc) - result
        self.assertAlmostEqual(diff.total_seconds(), 1800, delta=5)

    def test_days(self):
        result = mirror._parse_since("1d")
        diff = datetime.now(tz=timezone.utc) - result
        self.assertAlmostEqual(diff.total_seconds(), 86400, delta=5)

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            mirror._parse_since("invalid")

    def test_seconds(self):
        result = mirror._parse_since("60s")
        diff = datetime.now(tz=timezone.utc) - result
        self.assertAlmostEqual(diff.total_seconds(), 60, delta=5)


class TestNewerThan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp_path = Path(self.tmp.name)
        self.tmp.close()

    def tearDown(self):
        self.tmp_path.unlink(missing_ok=True)

    def test_none_cutoff_always_true(self):
        self.assertTrue(mirror._newer_than(self.tmp_path, None))

    def test_old_file_fails_cutoff(self):
        cutoff = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        self.assertFalse(mirror._newer_than(self.tmp_path, cutoff))

    def test_fresh_file_passes_cutoff(self):
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        self.assertTrue(mirror._newer_than(self.tmp_path, cutoff))


class TestEnsureVault(unittest.TestCase):
    def test_creates_required_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            mirror._ensure_vault(vault)
            self.assertTrue((vault / ".obsidian").is_dir())
            self.assertTrue((vault / "Sutando" / "Notes").is_dir())
            self.assertTrue((vault / "Sutando" / "Agent" / "Tasks").is_dir())
            self.assertTrue((vault / "Sutando" / "Tasks.md").is_file())
            self.assertTrue((vault / "Sutando" / "Agent" / "Asks.md").is_file())

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            mirror._ensure_vault(vault)
            mirror._ensure_vault(vault)  # second call should not raise


class TestParseTaskFile(unittest.TestCase):
    def test_extracts_id_from_filename(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", prefix="task-1234567890.", delete=False, mode="w") as f:
            f.write("do the thing")
            p = Path(f.name)
        try:
            task = mirror._parse_task_file(p)
            self.assertEqual(task["id"], "1234567890")
            self.assertEqual(task["text"], "do the thing")
        finally:
            p.unlink()


class TestWriteTaskMirror(unittest.TestCase):
    def test_writes_task_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            mirror._ensure_vault(vault)
            task = {"id": "999", "path": Path("task-999.txt"), "text": "test task body"}
            dest = mirror._write_task_mirror(vault, task)
            self.assertTrue(dest.exists())
            content = dest.read_text()
            self.assertIn("Task 999", content)
            self.assertIn("test task body", content)

    def test_preserves_result_block_on_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            mirror._ensure_vault(vault)
            task_dir = vault / "Sutando" / "Agent" / "Tasks"
            existing = task_dir / "task-888.md"
            existing.write_text("# Task 888\n\nold body\n\n## Result\nmy result here\n")
            task = {"id": "888", "path": Path("task-888.txt"), "text": "new body"}
            mirror._write_task_mirror(vault, task)
            content = existing.read_text()
            self.assertIn("## Result", content)
            self.assertIn("my result here", content)


class TestWriteResultMirror(unittest.TestCase):
    def test_appends_result_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            mirror._ensure_vault(vault)
            task_file = vault / "Sutando" / "Agent" / "Tasks" / "task-777.md"
            task_file.write_text("# Task 777\n\nbody\n")
            result_path = Path("results") / "task-777.txt"
            ok = mirror._write_result_mirror(vault, result_path, "the result text")
            self.assertTrue(ok)
            content = task_file.read_text()
            self.assertIn("## Result", content)
            self.assertIn("the result text", content)

    def test_replaces_existing_result_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            mirror._ensure_vault(vault)
            task_file = vault / "Sutando" / "Agent" / "Tasks" / "task-666.md"
            task_file.write_text("# Task 666\n\nbody\n\n## Result\nold result\n")
            result_path = Path("results") / "task-666.txt"
            mirror._write_result_mirror(vault, result_path, "new result")
            content = task_file.read_text()
            self.assertIn("new result", content)
            self.assertNotIn("old result", content)

    def test_missing_task_note_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            mirror._ensure_vault(vault)
            result_path = Path("results") / "task-555.txt"
            ok = mirror._write_result_mirror(vault, result_path, "result")
            self.assertFalse(ok)


class TestMirrorNotes(unittest.TestCase):
    def test_copies_md_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            vault = Path(tmp) / "vault"
            notes_dir = workspace / "notes"
            notes_dir.mkdir(parents=True)
            (notes_dir / "my-note.md").write_text("# My note\n\ncontent")
            mirror._ensure_vault(vault)
            count = mirror._mirror_notes(workspace, vault, cutoff=None, quiet=True)
            self.assertEqual(count, 1)
            dest = vault / "Sutando" / "Notes" / "my-note.md"
            self.assertTrue(dest.exists())
            self.assertIn("content", dest.read_text())


class TestMirrorPendingQuestions(unittest.TestCase):
    def test_copies_pending_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            vault = Path(tmp) / "vault"
            mirror._ensure_vault(vault)
            pq = workspace / "pending-questions.md"
            pq.write_text("## Q1\nWhy?\n")
            mirror._mirror_pending_questions(workspace, vault, quiet=True)
            dest = vault / "Sutando" / "Agent" / "Asks.md"
            self.assertIn("Why?", dest.read_text())


# ---------------------------------------------------------------------------
# dream.py tests (import separately — needs anthropic optional)
# ---------------------------------------------------------------------------

dream_path = REPO / "skills" / "obsidian-vault" / "scripts" / "dream.py"

class TestDreamModule(unittest.TestCase):
    def setUp(self):
        self.dream = _load("dream", dream_path)

    def test_proper_nouns_extracted(self):
        text = "Anthropic released Claude Sonnet for general use."
        nouns = self.dream._proper_nouns(text)
        self.assertIn("Anthropic", nouns)
        self.assertIn("Claude", nouns)
        self.assertIn("Sonnet", nouns)

    def test_short_words_excluded(self):
        text = "An Act For The Big One"
        nouns = self.dream._proper_nouns(text)
        # Words shorter than 6 chars after first letter → excluded by {5,}
        self.assertNotIn("An", nouns)
        self.assertNotIn("Act", nouns)
        self.assertNotIn("For", nouns)
        self.assertNotIn("The", nouns)
        self.assertNotIn("Big", nouns)

    def test_opt_in_gate_force(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(self.dream._opt_in_gate(force=True))

    def test_opt_in_gate_env(self):
        with patch.dict(os.environ, {"SUTANDO_OBSIDIAN_MIRROR": "1"}):
            self.assertTrue(self.dream._opt_in_gate(force=False))

    def test_opt_in_gate_blocked(self):
        env = {k: v for k, v in os.environ.items() if k != "SUTANDO_OBSIDIAN_MIRROR"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(self.dream._opt_in_gate(force=False))

    def test_already_linked_detects_wikilink(self):
        body_a = "Some text with [[NoteB]] link."
        self.assertTrue(self.dream._already_linked(body_a, "NoteB", "body b", "NoteA"))

    def test_already_linked_false_when_no_link(self):
        self.assertFalse(self.dream._already_linked("body a", "NoteB", "body b", "NoteA"))

    def test_apply_inline_ref_appends_to_last_paragraph(self):
        body = "# Header\n\nParagraph one.\n\nParagraph two with content."
        result = self.dream.apply_inline_ref(body, "(cf. [[OtherNote]])")
        self.assertIn("(cf. [[OtherNote]])", result)
        self.assertTrue(result.endswith("(cf. [[OtherNote]])\n") or "(cf. [[OtherNote]])" in result.split("\n\n")[-1])

    def test_update_dream_footer_adds_block(self):
        body = "# Note\n\nSome content.\n"
        result = self.dream._update_dream_footer(body, ["LinkA", "LinkB"])
        self.assertIn(self.dream.SENTINEL_START, result)
        self.assertIn("[[LinkA]]", result)
        self.assertIn("[[LinkB]]", result)

    def test_update_dream_footer_idempotent(self):
        body = "# Note\n\nContent.\n"
        body = self.dream._update_dream_footer(body, ["LinkA"])
        body = self.dream._update_dream_footer(body, ["LinkA"])  # second run same stem
        self.assertEqual(body.count("[[LinkA]]"), 1)

    def test_existing_dream_stems_parsed(self):
        body = (
            f"{self.dream.SENTINEL_START}\n"
            "> **See also:** [[NoteA]]  [[NoteB]]\n"
            f"{self.dream.SENTINEL_END}\n"
        )
        stems = self.dream._existing_dream_stems(body)
        self.assertIn("NoteA", stems)
        self.assertIn("NoteB", stems)

    def test_pairs_to_check_filters_by_shared_noun(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "Anthropic.md"
            p2 = Path(tmp) / "Claude-Release.md"
            p3 = Path(tmp) / "Unrelated.md"
            p1.write_text("Anthropic released Claude Sonnet.")
            p2.write_text("Claude Sonnet is built by Anthropic.")
            p3.write_text("Bananas are yellow.")
            pairs = self.dream._pairs_to_check([p1, p2, p3])
            pair_names = {(a.stem, b.stem) for a, b in pairs}
            # p1 and p2 share "Anthropic", "Claude", "Sonnet"
            self.assertIn(("Anthropic", "Claude-Release"), pair_names)
            # p3 shares nothing
            for a, b in pairs:
                self.assertNotIn("Unrelated", (a.stem, b.stem))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestOptInGate, TestParseSince, TestNewerThan, TestEnsureVault,
        TestParseTaskFile, TestWriteTaskMirror, TestWriteResultMirror,
        TestMirrorNotes, TestMirrorPendingQuestions, TestDreamModule,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
