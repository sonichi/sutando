#!/usr/bin/env python3
"""current-track-rotate.py keeps the per-pass read bounded without losing a byte."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "current-track-rotate.py"


def load():
    spec = importlib.util.spec_from_file_location("rot", SCRIPT); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def fixture(n_entries=40, size=600):
    pre = "# current track\n\nMain goal: keep the loop honest.\n\n"
    entries = [f"## 2026-09-{i%30+1:02d}T{i%24:02d}:00Z — entry {i}\n" + ("x" * size) + "\n\n" for i in range(n_entries)]
    return pre, entries


class Rotate(unittest.TestCase):
    def test_under_cap_is_a_noop(self):
        m = load(); pre, ents = fixture(3)
        text = pre + "".join(ents)
        head, archived = m.plan(text, 1 << 20)
        self.assertEqual((head, archived), (text, ""))

    def test_head_plus_archive_is_the_original(self):
        m = load(); pre, ents = fixture()
        text = pre + "".join(ents)
        head, archived = m.plan(text, 8 * 1024)
        self.assertEqual(pre + archived + head[len(pre):], text)
        self.assertTrue(head.startswith(pre))
        self.assertLessEqual(len(head.encode()), 8 * 1024 + 700)  # one entry of slack at most

    def test_newest_entries_are_the_ones_kept(self):
        m = load(); pre, ents = fixture()
        head, archived = m.plan(pre + "".join(ents), 8 * 1024)
        self.assertIn("entry 39", head); self.assertIn("entry 0", archived); self.assertNotIn("entry 0\n", head)

    def test_cli_rotates_atomically_and_twice_is_idempotent(self):
        pre, ents = fixture()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "current-track.md"; p.write_text(pre + "".join(ents))
            r = subprocess.run([sys.executable, str(SCRIPT), str(p), "--keep-bytes", "8192"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            arch = Path(d) / "current-track-archive.md"
            self.assertTrue(arch.exists())
            self.assertEqual(pre + arch.read_text() + p.read_text()[len(pre):], pre + "".join(ents))
            before = (p.read_text(), arch.read_text())
            r2 = subprocess.run([sys.executable, str(SCRIPT), str(p), "--keep-bytes", "8192"], capture_output=True, text=True)
            self.assertEqual(r2.returncode, 0); self.assertIn("nothing to do", r2.stdout)
            self.assertEqual(before, (p.read_text(), arch.read_text()))
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")], [])

    def test_unreadable_path_is_exit_1(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "/nonexistent/current-track.md"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
