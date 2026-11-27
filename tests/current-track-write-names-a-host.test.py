#!/usr/bin/env python3
"""The current-track writer must refuse a target that names no host.

`hosts/$H/current-track.md` with an unset $H collapses to `hosts/current-track.md`.
The write then SUCCEEDS onto a path the vault's carrier rules do not cover
(`!hosts/*/**` needs the directory level), so the entry is ignored and never
backed up, and the only downstream symptom is a carrier probe blaming the
exclude file. Observed live: a 373-byte anchor plus its lock, unbacked.

Run: python3 tests/current-track-write-names-a-host.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "current-track-write.py"
ROTATE = ROOT / "scripts" / "current-track-rotate.py"

sys.path.insert(0, str(ROOT / "src"))
import current_track as ct  # noqa: E402


def run(target: Path, text: str = "- entry\n"):
    return subprocess.run([sys.executable, str(SCRIPT), "append", str(target)],
                          input=text, capture_output=True, text=True)


class TargetMustNameAHost(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "hosts" / "MyHost").mkdir(parents=True)

    def test_a_target_under_hosts_label_is_written(self):
        target = self.ws / "hosts" / "MyHost" / "current-track.md"
        r = run(target)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("- entry", target.read_text())

    def test_the_collapsed_path_is_refused_and_writes_nothing(self):
        target = self.ws / "hosts" / "current-track.md"
        r = run(target)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("hosts/<label>/", r.stderr)
        self.assertFalse(target.exists(), "the refused write still created the file")
        self.assertFalse(target.with_name(target.name + ".lock").exists(),
                         "the refused write still created the lock beside it")

    def test_an_empty_label_produces_exactly_that_collapsed_path(self):
        label = ""
        self.assertEqual((self.ws / "hosts" / label / "current-track.md").name, "current-track.md")
        self.assertEqual((self.ws / "hosts" / label / "current-track.md").parent.name, "hosts",
                         "an unset label is what puts the file directly in hosts/")

    def test_a_target_outside_hosts_is_refused(self):
        r = run(self.ws / "current-track.md")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertFalse((self.ws / "current-track.md").exists())

    def test_the_refusal_is_measured_in_process_not_only_by_subprocess(self):
        """The subprocess cases above pin the exit code a caller sees, but no
        coverage tracer follows a subprocess, so the refusal branch reads unhit."""
        spec = importlib.util.spec_from_file_location("ctw", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        err = io.StringIO()
        saved = sys.argv[:]
        try:
            sys.argv = ["current-track-write.py", "append", str(self.ws / "hosts" / "current-track.md")]
            with contextlib.redirect_stderr(err):
                rc = mod.main()
        finally:
            sys.argv = saved
        self.assertEqual(rc, 2)
        self.assertIn("hosts/<label>/", err.getvalue())
        self.assertFalse((self.ws / "hosts" / "current-track.md").exists())


class TheRuleIsTheResolvedDestination(unittest.TestCase):
    """Both sides of the boundary, because a spelling test gets each one wrong.

    `hosts` is a legal hostname, so reserving the literal string refuses a real
    host; and `hosts/../x` reads as if it were under hosts/ while naming the
    workspace root. Only the resolved path separates them.
    """

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "hosts" / "hosts").mkdir(parents=True)

    def test_a_host_actually_labelled_hosts_is_written(self):
        target = self.ws / "hosts" / "hosts" / "current-track.md"
        r = run(target)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("- entry", target.read_text())

    def test_a_target_escaping_hosts_with_dotdot_is_refused(self):
        r = run(self.ws / "hosts" / ".." / "current-track.md")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(sorted(p.name for p in self.ws.glob("current-track.md*")), [],
                         "the refused write left an anchor or a lock at the workspace root")


class TheSharedWriterEnforcesIt(unittest.TestCase):
    """The CLI is not the boundary — rotation and direct calls reach the same files.

    A guard living only in scripts/current-track-write.py leaves
    current-track-rotate.py and any in-process append()/replace() free to create
    the collapsed anchor the guard exists to prevent.
    """

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "hosts").mkdir(parents=True)
        self.collapsed = self.ws / "hosts" / "current-track.md"

    def test_append_refuses_and_creates_nothing(self):
        with self.assertRaises(ct.NotAHostAnchor):
            ct.append(self.collapsed, "- entry\n")
        self.assertFalse(self.collapsed.exists())
        self.assertFalse(ct.lock_path(self.collapsed).exists())

    def test_replace_refuses_and_creates_nothing(self):
        with self.assertRaises(ct.NotAHostAnchor):
            ct.replace(self.collapsed, "# head\n")
        self.assertFalse(self.collapsed.exists())
        self.assertFalse(ct.lock_path(self.collapsed).exists())

    def test_rotate_refuses_an_existing_collapsed_anchor(self):
        """Written behind the guard's back, rotation must still refuse to touch it."""
        self.collapsed.write_text("## 2026-09-01T00:00Z a\n" + "x" * 900
                                  + "\n## 2026-09-02T00:00Z b\n" + "y" * 900 + "\n")
        before = self.collapsed.read_text()
        with self.assertRaises(ct.NotAHostAnchor):
            ct.rotate(self.collapsed, keep_bytes=64)
        self.assertEqual(self.collapsed.read_text(), before, "rotation rewrote a refused anchor")
        self.assertFalse((self.ws / "hosts" / "current-track-archive.md").exists(),
                         "rotation wrote an archive beside a refused anchor")

    def test_the_rotate_cli_reports_the_refusal_as_exit_2(self):
        self.collapsed.write_text("## a\ntext\n## b\ntext\n")
        r = subprocess.run([sys.executable, str(ROTATE), str(self.collapsed), "--keep-bytes", "8"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("hosts/<label>/", r.stderr)

    def test_a_valid_host_anchor_still_rotates(self):
        """The positive control: without it every assertion above passes on a
        writer that refuses everything."""
        good = self.ws / "hosts" / "MyHost" / "current-track.md"
        good.parent.mkdir(parents=True)
        # Stamped headings: rotation keeps BOTH ends when it cannot order them,
        # so an unstamped fixture archives nothing and the control proves nothing.
        good.write_text("## 2026-09-01T00:00Z a\n" + "x" * 900
                        + "\n## 2026-09-02T00:00Z b\n" + "y" * 900 + "\n")
        r = ct.rotate(good, keep_bytes=1024)
        self.assertTrue(r.archived, "nothing was archived, so this control proves nothing")
        self.assertTrue((self.ws / "hosts" / "MyHost" / "current-track-archive.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
