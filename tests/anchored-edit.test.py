#!/usr/bin/env python3
"""An edit that changes nothing must not exit 0.

Measured 2026-09-04, three times across two agents in one session: a patch
script printed "second copy updated" against an anchor the file did not
contain; a build_log append lost its redirect and rendered to the terminal;
two task closures were narrated with no result file written. In all three the
success message was generated INDEPENDENTLY of the operation, so it was never
evidence about the operation at all.

Run: python3 tests/anchored-edit.test.py
"""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "scripts" / "anchored-edit.py"
spec = importlib.util.spec_from_file_location("ae", TOOL)
ae = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ae)


class AnchoredEdit(unittest.TestCase):
    def test_a_real_edit_applies(self):
        """Control: without this, refusing everything would pass every other test."""
        out, n = ae.apply_edit("alpha beta", "beta", "gamma")
        self.assertEqual((out, n), ("alpha gamma", 1))

    def test_a_drifted_anchor_refuses_instead_of_no_op(self):
        with self.assertRaises(ValueError) as cm:
            ae.apply_edit("alpha beta", "delta", "gamma")
        self.assertIn("absent", str(cm.exception))

    def test_an_ambiguous_anchor_refuses_unless_deliberate(self):
        with self.assertRaises(ValueError):
            ae.apply_edit("x x", "x", "y")
        self.assertEqual(ae.apply_edit("x x", "x", "y", allow_multi=True), ("y y", 2))

    def test_old_equals_new_is_a_no_op_and_refuses(self):
        with self.assertRaises(ValueError):
            ae.apply_edit("alpha", "alpha", "alpha")

    def test_an_empty_anchor_refuses(self):
        """It matches at every position, so it is never the edit anyone meant."""
        with self.assertRaises(ValueError):
            ae.apply_edit("alpha", "", "x")

    def _run(self, text, *argv):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "f.txt"
            f.write_text(text)
            p = subprocess.run([sys.executable, str(TOOL), str(f), *argv],
                               capture_output=True, text=True)
            return p.returncode, p.stdout, p.stderr, f.read_text()

    def test_the_cli_exits_2_and_writes_nothing_when_the_anchor_drifted(self):
        rc, out, err, text = self._run("alpha", "--old", "delta", "--new", "x")
        self.assertEqual(rc, 2)
        self.assertEqual(text, "alpha", "a refused edit must leave the file alone")
        self.assertIn("REFUSED", err)
        self.assertEqual(out, "", "a refusal must print no success receipt")

    def test_the_cli_receipt_is_read_back_from_the_file(self):
        rc, out, err, text = self._run("alpha beta", "--old", "beta", "--new", "gamma")
        self.assertEqual(rc, 0)
        self.assertEqual(text, "alpha gamma")
        self.assertIn("replacement present 1x", out)

    def test_count_mismatch_refuses(self):
        rc, out, err, text = self._run("x x", "--old", "x", "--new", "y",
                                       "--allow-multi", "--count", "3")
        self.assertEqual(rc, 2)
        self.assertEqual(text, "x x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
