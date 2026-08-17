#!/usr/bin/env python3
"""The deprecated `scripts/pr_flag.py` path must keep working, or fail loudly.

Grounded in the review on #3005: a registered cron is a prompt SNAPSHOT, so a
host whose job was registered against `python3 scripts/pr_flag.py` keeps
invoking that path until someone re-runs /schedule-crons. Deleting it outright
makes the hourly digest vanish with no error anyone reads.

The two properties that matter are opposites, and only testing both shows the
shim is a bridge rather than a silencer:
  - skill present -> forwards, and the child's exit code and stdout survive
  - skill absent  -> non-zero and says what to do, never a quiet success

Run: python3 tests/pr-flag-shim.test.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHIM = REPO / "scripts" / "pr_flag.py"


class Shim(unittest.TestCase):
    def _run(self, cfg_dir, args=(), plant=None):
        """Run the shim with CLAUDE_CONFIG_DIR=cfg_dir, optionally planting a fake skill."""
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg_dir))
        if plant is not None:
            tgt = Path(cfg_dir) / "skills/pr-triage/scripts/pr_flag.py"
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text(plant)
        # The shim only ever runs as a child process, so without this the diff
        # gate measures it at 0% while every test here passes. Same seam as
        # tests/voice-lock.test.py.
        cmd = [sys.executable]
        if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
            cmd += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]
        return subprocess.run([*cmd, str(SHIM), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_forwards_to_the_skill_when_it_is_installed(self):
        fake = textwrap.dedent("""\
            import sys
            print("SKILL_RAN " + " ".join(sys.argv[1:]))
            sys.exit(0)
        """)
        with tempfile.TemporaryDirectory() as d:
            r = self._run(d, ["--emit", "--repo", "o/r"], plant=fake)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SKILL_RAN --emit --repo o/r", r.stdout,
                      "argv did not survive the forward")

    def test_the_child_exit_code_survives(self):
        # A shim that swallows a non-zero exit turns a failed digest into a
        # silent one, which is the failure this whole path exists to avoid.
        with tempfile.TemporaryDirectory() as d:
            r = self._run(d, plant="import sys; sys.exit(7)")
        self.assertEqual(r.returncode, 7)

    def test_absent_skill_is_loud_and_non_zero(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run(d)                      # nothing planted
        self.assertNotEqual(r.returncode, 0, "a missing skill exited 0")
        self.assertIn("not installed", r.stderr)
        self.assertIn("/schedule-crons", r.stderr, "the remedy is not stated")

    def test_it_always_says_it_is_deprecated(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run(d, plant="import sys; sys.exit(0)")
        self.assertIn("DEPRECATED", r.stderr)

    def test_target_prefers_the_config_dir_over_the_repo_fallback(self):
        # In-process, so the resolution order is asserted directly rather than
        # inferred from which child happened to run. Every subprocess test ends
        # in os.execv, which is why this path is otherwise unobservable.
        spec = importlib.util.spec_from_file_location("_prflag_shim", SHIM)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as d:
            tgt = Path(d) / "skills/pr-triage/scripts/pr_flag.py"
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text("import sys; sys.exit(0)")
            os.environ["CLAUDE_CONFIG_DIR"] = d
            try:
                self.assertEqual(mod._target(), tgt)
            finally:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertIsNone(mod._target(), "no skill anywhere must resolve to None")

    def test_the_shim_carries_no_logic(self):
        # The vendored copy drifted because it held the implementation. This
        # file may only forward; a size ceiling is the cheap way to pin that.
        self.assertLess(len(SHIM.read_text().splitlines()), 80,
                        "the shim has grown logic — it must only forward")


if __name__ == "__main__":
    unittest.main(verbosity=2)
