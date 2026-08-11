#!/usr/bin/env python3
"""Behavioral test: a failed archive must not destroy the task it failed to archive.

Root cause (2026-08-11): `archive_file()` caught every exception from the move
and then ran `src.unlink(missing_ok=True)` under the comment "so we don't leave
stale files". That deletes the task file whose PRESERVATION is the entire reason
archiving exists — and it does so on the one path where something already went
wrong, printing a line nobody reads. A task could vanish with no result written
and no trace beyond that print.

`archive_path()` is inside the same try, so an unwritable or clobbered archive
directory — a `mkdir` that raises — reaches the delete. That is the failure this
test drives, because it needs no mocking of `shutil`.

The fix leaves `src` in place and reports failure, which raises a second
question the delete had been masking: the caller clears the delivery sentinel
straight afterwards. A surviving result file re-enters the poll loop, and the
sentinel is the only thing standing between it and a duplicate send — so the
clear is now gated on the archive having actually succeeded. Both call sites
already CLAIMED that precondition in their comments ("Delivery succeeded +
archived") without checking it.

Extracts the pure functions rather than importing the bridge, matching the
convention of the other bridge tests.
"""
from pathlib import Path
import ast
import os
import tempfile
import unittest

# Hermetic-bridge-test lint: an explicit config root plus the canonical
# access.json seeded underneath it, rooted at the name in the environ.
_CFG = tempfile.mkdtemp(prefix="archive-test-cfg-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
_ACCESS = Path(_CFG) / "channels" / "discord" / "access.json"
_ACCESS.parent.mkdir(parents=True, exist_ok=True)
_ACCESS.write_text('{"allowFrom": []}')

SRC = Path(__file__).resolve().parent.parent / "src" / "discord-bridge.py"


def _load(tasks_archive, results_archive):
    """Exec archive_path + archive_file against temp archive roots."""
    tree = ast.parse(SRC.read_text())
    wanted = {"archive_path", "archive_file"}
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in wanted]
    missing = wanted - {n.name for n in body}
    if missing:
        raise AssertionError(f"not found in {SRC}: {sorted(missing)}")
    ns = {"Path": Path,
          "ARCHIVE_TASKS_DIR": tasks_archive,
          "ARCHIVE_RESULTS_DIR": results_archive}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SRC), "exec"), ns)
    return ns["archive_file"]


class ArchiveFailureIsNotDeletion(unittest.TestCase):
    def test_the_happy_path_still_moves_the_file(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            src = d / "task-1.txt"
            src.write_text("id: task-1\ntask: do the thing\n")
            fn = _load(d / "arch-tasks", d / "arch-results")
            self.assertTrue(fn(src, "tasks", "task-1"))
            self.assertFalse(src.exists(), "archived file should be gone from source")
            moved = list((d / "arch-tasks").rglob("task-1.txt"))
            self.assertEqual(len(moved), 1, "file should be in the archive")
            self.assertIn("do the thing", moved[0].read_text())

    def test_a_failed_archive_LEAVES_the_task(self):
        """The regression. Pre-fix this asserts False — the file was unlinked."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            src = d / "task-2.txt"
            src.write_text("id: task-2\ntask: irreplaceable\n")
            # Clobber the archive root with a FILE so mkdir(parents=True) raises
            # inside archive_path, i.e. a real unwritable-archive failure.
            broken = d / "arch-tasks"
            broken.write_text("not a directory")
            fn = _load(broken, d / "arch-results")

            self.assertFalse(fn(src, "tasks", "task-2"), "must report failure")
            self.assertTrue(src.exists(),
                            "a failed archive must NOT delete the task file")
            self.assertIn("irreplaceable", src.read_text(),
                          "the surviving file must still hold its content")

    def test_absent_source_reports_success(self):
        """Nothing to archive is not a failure — it must not hold the sentinel."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fn = _load(d / "arch-tasks", d / "arch-results")
            self.assertTrue(fn(d / "nope.txt", "tasks", "task-3"))

    def test_results_kind_routes_to_the_results_archive(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            src = d / "task-4.txt"
            src.write_text("done")
            fn = _load(d / "arch-tasks", d / "arch-results")
            self.assertTrue(fn(src, "results", "task-4"))
            self.assertEqual(len(list((d / "arch-results").rglob("task-4.txt"))), 1)
            self.assertEqual(len(list((d / "arch-tasks").rglob("*.txt"))), 0)


class SentinelStaysWhileTheResultDoes(unittest.TestCase):
    """Without this, leaving the file trades data loss for a duplicate send."""

    def test_every_clear_delivered_is_gated_on_the_archive(self):
        """AST, not text: a comment mentioning the call is not a call, and the
        guard sits further back than any fixed character window reaches."""
        tree = ast.parse(SRC.read_text())

        def clear_calls(node):
            return [n for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "_clear_delivered"]

        all_calls = clear_calls(tree)
        gated = [c for n in ast.walk(tree)
                 if isinstance(n, ast.If)
                 and isinstance(n.test, ast.Name) and n.test.id == "_archived"
                 for c in clear_calls(ast.Module(body=n.body, type_ignores=[]))]

        self.assertGreaterEqual(len(all_calls), 2,
                                "expected the two poll-loop clear sites")
        self.assertEqual(len(gated), len(all_calls),
                         f"{len(all_calls) - len(gated)} _clear_delivered "
                         "call(s) not behind `if _archived:` — an ungated clear "
                         "lets a surviving result file be delivered a second time")

    def test_the_gate_reads_the_RESULT_archive(self):
        """`if _archived:` is only meaningful if _archived came from archiving
        the result file — the task file's fate does not drive redelivery."""
        tree = ast.parse(SRC.read_text())
        assigns = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "_archived"
                           for t in n.targets)
                   and isinstance(n.value, ast.Call)
                   and isinstance(n.value.func, ast.Name)
                   and n.value.func.id == "archive_file"
                   and n.value.args
                   and isinstance(n.value.args[0], ast.Name)
                   and n.value.args[0].id == "result_file"]
        self.assertGreaterEqual(len(assigns), 2,
                                "each gated site needs its own "
                                "`_archived = archive_file(result_file, ...)`")

    def test_archive_file_no_longer_unlinks(self):
        text = SRC.read_text()
        start = text.index("def archive_file(")
        end = text.index("\ndef ", start + 1)
        self.assertNotIn("unlink", text[start:end],
                         "archive_file must never delete its source")


if __name__ == "__main__":
    unittest.main(verbosity=2)
