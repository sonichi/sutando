#!/usr/bin/env python3
"""Tests for the `hosts/<host>/` per-host read probe in personal_path (H4 fix).

The per-host relocation (#1717) moves per-host files into
`<workspace>/hosts/<hostname>/`. personal_path() must probe that location
FIRST so relocated files are found; when absent, resolution must be
byte-for-byte identical to the pre-#1717 order (purely additive, no regression).

Run: python3 tests/util-paths-hosts-resolution.test.py
Exit: 0 on pass, 1 on fail.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from util_paths import _host_label, personal_path  # noqa: E402


def clear_env():
    for k in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR", "SUTANDO_HOST_LABEL"):
        os.environ.pop(k, None)


class HostsResolutionTests(unittest.TestCase):
    def setUp(self):
        clear_env()
        os.environ["SUTANDO_HOST_LABEL"] = "test-host"
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        clear_env()

    def test_hosts_file_found_first(self):
        # A file relocated to hosts/<host>/ is returned, not the root copy.
        host_dir = self.ws / "hosts" / "test-host"
        host_dir.mkdir(parents=True)
        (host_dir / "stand-identity.json").write_text("{}")
        (self.ws / "stand-identity.json").write_text("{}")  # stale root copy
        with redirect_stderr(io.StringIO()):
            p = personal_path("stand-identity.json", workspace=self.ws)
        self.assertEqual(p, host_dir / "stand-identity.json")

    def test_absent_hosts_falls_back_to_workspace_root(self):
        # No hosts/ file → identical to pre-#1717 behavior (root fallback).
        (self.ws / "pending-questions.md").write_text("q")
        with redirect_stderr(io.StringIO()):
            p = personal_path("pending-questions.md", workspace=self.ws)
        self.assertEqual(p, self.ws / "pending-questions.md")

    def test_nothing_exists_preferred_return_unchanged(self):
        # No memory dir, nothing on disk → preferred return is workspace root
        # (NOT hosts/) — the fix is read-side only; write target is untouched.
        with redirect_stderr(io.StringIO()):
            p = personal_path("never-created.json", workspace=self.ws)
        self.assertEqual(p, self.ws / "never-created.json")

    def test_hosts_beats_legacy_machine_dir(self):
        # Both hosts/ (new) and machine-<host>/ (legacy memory-dir) have the
        # file → hosts/ wins (it is probed first).
        mem = Path(self._tmp.name) / "memrepo"
        (mem / "machine-test-host").mkdir(parents=True)
        (mem / "machine-test-host" / "f.json").write_text("legacy")
        host_dir = self.ws / "hosts" / "test-host"
        host_dir.mkdir(parents=True)
        (host_dir / "f.json").write_text("new")
        os.environ["SUTANDO_MEMORY_DIR"] = str(mem)
        with redirect_stderr(io.StringIO()):
            p = personal_path("f.json", workspace=self.ws)
        self.assertEqual(p, host_dir / "f.json")

    def test_legacy_machine_dir_still_found_when_no_hosts(self):
        # hosts/ absent but legacy machine-<host>/ has it → legacy still works.
        mem = Path(self._tmp.name) / "memrepo2"
        (mem / "machine-test-host").mkdir(parents=True)
        (mem / "machine-test-host" / "g.json").write_text("legacy")
        os.environ["SUTANDO_MEMORY_DIR"] = str(mem)
        with redirect_stderr(io.StringIO()):
            p = personal_path("g.json", workspace=self.ws)
        self.assertEqual(p, mem / "machine-test-host" / "g.json")

    def test_host_label_drives_hosts_segment(self):
        self.assertEqual(_host_label(), "test-host")

    def test_dotted_host_label_used_raw(self):
        # An explicit label is an override → used verbatim, NOT split on '.'.
        # Parity guard with TS hostLabel() (Mini #1718 review note 1).
        os.environ["SUTANDO_HOST_LABEL"] = "a.b"
        self.assertEqual(_host_label(), "a.b")


if __name__ == "__main__":
    unittest.main()
