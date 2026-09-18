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


if __name__ == "__main__":
    unittest.main(verbosity=2)
