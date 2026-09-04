#!/usr/bin/env python3
"""The exec shims at the old scripts/ paths hand off to skills/pr-review/scripts/ with argv intact."""
import os
import runpy
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ShimTests(unittest.TestCase):
    def _run_shim(self, name, argv):
        calls = []

        def fake_execv(exe, args):
            calls.append((exe, args))
            raise SystemExit(0)

        with mock.patch.object(os, "execv", fake_execv), mock.patch.object(sys, "argv", [name] + argv):
            with self.assertRaises(SystemExit):
                runpy.run_path(os.path.join(ROOT, "scripts", name), run_name="__main__")
        self.assertEqual(len(calls), 1, "the shim must exec exactly once")
        return calls[0]

    def test_ci_triage_shim_execs_the_moved_script(self):
        exe, args = self._run_shim("ci-triage.py", ["--pr", "42"])
        self.assertEqual(exe, sys.executable)
        self.assertEqual(os.path.normpath(args[1]), os.path.join(ROOT, "skills", "pr-review", "scripts", "ci-triage.py"))
        self.assertEqual(args[2:], ["--pr", "42"])
        self.assertTrue(os.path.exists(args[1]), "the exec target must exist")

    def test_review_preflight_shim_execs_the_moved_script(self):
        exe, args = self._run_shim("review-preflight.py", ["3902"])
        self.assertEqual(exe, sys.executable)
        self.assertEqual(os.path.normpath(args[1]), os.path.join(ROOT, "skills", "pr-review", "scripts", "review-preflight.py"))
        self.assertEqual(args[2:], ["3902"])
        self.assertTrue(os.path.exists(args[1]), "the exec target must exist")


if __name__ == "__main__":
    unittest.main()
