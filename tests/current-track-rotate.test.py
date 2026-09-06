#!/usr/bin/env python3
"""current_track.py is the one writer: append and rotate share a lock, nothing is lost, an
oversized newest entry is refused loudly instead of reported as nothing to do."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROTATE = REPO / "scripts" / "current-track-rotate.py"
APPEND = REPO / "scripts" / "current-track-append.py"
sys.path.insert(0, str(REPO / "src"))
import current_track as ct  # noqa: E402


def load(path):
    """Import a hyphen-named script so its main() runs in-process (coverage sees it)."""
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class Cli:
    def __init__(self, mod, stdin=None):
        self.mod, self.stdin = mod, stdin

    def __call__(self, *argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        old_in = sys.stdin
        try:
            if self.stdin is not None:
                sys.stdin = io.StringIO(self.stdin)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    rc = self.mod.main([str(a) for a in argv])
                except SystemExit as e:
                    rc = e.code
        finally:
            sys.stdin = old_in
        return type("R", (), {"returncode": rc, "stdout": out.getvalue(), "stderr": err.getvalue()})()


def fixture(n_entries=40, size=600):
    pre = "# current track\n\nMain goal: keep the loop honest.\n\n"
    entries = [f"## 2026-09-{i%30+1:02d}T{i%24:02d}:00Z — entry {i}\n" + ("x" * size) + "\n\n" for i in range(n_entries)]
    return pre, entries


def run(*argv, stdin=None):
    return subprocess.run([sys.executable, *map(str, argv)], input=stdin, capture_output=True, text=True)


class Plan(unittest.TestCase):
    def test_under_cap_is_a_noop(self):
        pre, ents = fixture(3); text = pre + "".join(ents)
        r = ct.plan(text, 1 << 20)
        self.assertEqual((r.head, r.archived, r.oversized), (text, "", False))

    def test_head_plus_archive_is_the_original_and_newest_kept(self):
        pre, ents = fixture(); text = pre + "".join(ents)
        r = ct.plan(text, 8 * 1024)
        self.assertEqual(pre + r.archived + r.head[len(pre):], text)
        self.assertTrue(r.head.startswith(pre))
        self.assertIn("entry 39", r.head); self.assertNotIn("entry 0\n", r.head)
        self.assertLessEqual(len(r.head.encode()), 8 * 1024)
        self.assertFalse(r.oversized)

    def test_no_headings_means_nothing_to_archive_but_over_budget(self):
        r = ct.plan("plain prose " * 1000, 100)
        self.assertEqual(r.archived, ""); self.assertTrue(r.oversized)

    def test_oversized_newest_entry_is_kept_whole_and_flagged(self):
        pre, ents = fixture(5, 600)
        big = "## 2026-09-06T02:00Z — the giant\n" + ("y" * 40_000) + "\n"
        text = pre + "".join(ents) + big
        r = ct.plan(text, 32 * 1024)
        self.assertTrue(r.oversized)
        self.assertEqual(r.head, pre + big)              # kept whole, never cut
        self.assertEqual(r.archived, "".join(ents))      # everything older still leaves


class RotateAndAppend(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.p = Path(self.d.name) / "current-track.md"
        pre, ents = fixture(); self.pre = pre; self.p.write_text(pre + "".join(ents))
        self.archive = self.p.with_name("current-track-archive.md")

    def tearDown(self):
        self.d.cleanup()

    def test_rotate_writes_archive_first_then_replaces_head(self):
        before = self.p.read_text()
        r = ct.rotate(self.p, 8 * 1024)
        self.assertEqual(self.p.read_text(), r.head)
        self.assertEqual(self.pre + self.archive.read_text() + r.head[len(self.pre):], before)

    def test_dry_run_touches_nothing(self):
        before = self.p.read_text()
        r = ct.rotate(self.p, 8 * 1024, dry_run=True)
        self.assertTrue(r.archived); self.assertEqual(self.p.read_text(), before); self.assertFalse(self.archive.exists())

    def test_concurrent_append_during_rotation_survives(self):
        """The reviewer's race: an append between rotate's read and its replace must land in the head."""
        entry = "## 2026-09-06T02:10Z — landed mid-rotation\nkeep me\n"
        done = threading.Event()

        def appender():
            ct.append(self.p, entry); done.set()

        def seam():
            threading.Thread(target=appender, daemon=True).start()
            self.assertFalse(done.wait(0.5))   # blocked on the writer lock while rotate holds it

        ct.rotate(self.p, 8 * 1024, _between_read_and_replace=seam)
        self.assertTrue(done.wait(5))
        self.assertIn(entry, self.p.read_text())
        self.assertNotIn(entry, self.archive.read_text())

    def test_append_cli_and_rotate_cli_share_the_lock_across_processes(self):
        entry = "## 2026-09-06T02:20Z — from the CLI\nvia stdin\n"
        procs = [subprocess.Popen([sys.executable, str(APPEND), str(self.p)], stdin=subprocess.PIPE, text=True) for _ in range(3)]
        rot = subprocess.Popen([sys.executable, str(ROTATE), str(self.p), "--keep-bytes", "8192"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i, pr in enumerate(procs):
            pr.communicate(entry.replace("CLI", f"CLI {i}"))
        rot.communicate()
        self.assertEqual(rot.returncode, 0)
        text = self.p.read_text() + self.archive.read_text()
        for i in range(3):
            self.assertEqual(text.count(f"from the CLI {i}"), 1)

    def test_cli_exit_codes_in_process(self):
        rot = Cli(load(ROTATE))
        r = rot(self.p, "--keep-bytes", "8192", "--dry-run"); self.assertEqual(r.returncode, 0); self.assertIn("would move", r.stdout)
        r = rot(self.p, "--keep-bytes", "8192"); self.assertEqual(r.returncode, 0); self.assertIn("moved", r.stdout)
        r = rot(self.p, "--keep-bytes", "8192"); self.assertEqual(r.returncode, 0); self.assertIn("nothing to do", r.stdout)
        r = rot(self.p, "--keep-bytes", "0"); self.assertEqual(r.returncode, 2)
        r = rot(self.p.with_name("absent.md")); self.assertEqual(r.returncode, 1); self.assertIn("cannot read", r.stderr)
        self.p.write_text(self.pre + "## 2026-09-06T02:00Z — the giant\n" + "y" * 40_000 + "\n")
        r = rot(self.p, "--keep-bytes", "32768"); self.assertEqual(r.returncode, 3)
        self.assertIn("REFUSING", r.stderr); self.assertIn("the giant", r.stderr)
        r = rot(self.p, "--keep-bytes", "32768", "--dry-run"); self.assertEqual(r.returncode, 3); self.assertIn("would keep", r.stderr)
        self.p.write_text("plain prose with no heading " * 2000)
        r = rot(self.p, "--keep-bytes", "1024"); self.assertEqual(r.returncode, 3); self.assertIn("the preamble", r.stderr)
        app = load(APPEND)
        r = Cli(app, stdin="   \n")(self.p); self.assertEqual(r.returncode, 1)
        r = Cli(app, stdin="x")(); self.assertEqual(r.returncode, 2)
        r = Cli(app, stdin="## 2026-09-06T02:30Z — no trailing newline")(self.p); self.assertEqual(r.returncode, 0)
        self.assertTrue(self.p.read_text().endswith("no trailing newline\n"))

if __name__ == "__main__":
    unittest.main()
