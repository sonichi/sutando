#!/usr/bin/env python3
"""terminal_open pure core: detection from env + plan/applescript building.

open_instance (the actual spawner) is pragma-excluded; everything decision-
shaped is these pure helpers, driven for every supported terminal.

Run: python3 tests/runtime-cli-terminal-open.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-cli"))

import terminal_open as to  # noqa: E402


class DetectTerminal(unittest.TestCase):
    def _with(self, env):
        base = {"TERM_PROGRAM": "", "WEZTERM_PANE": None, "KITTY_WINDOW_ID": None}
        clean = {k: v for k, v in {**base, **env}.items() if v is not None}
        drop = [k for k, v in {**base, **env}.items() if v is None]
        with mock.patch.dict("os.environ", clean, clear=False):
            for k in drop:
                mock.patch.dict("os.environ", {}, clear=False)
            import os
            for k in drop:
                os.environ.pop(k, None)
            return to.detect_terminal()

    def test_each_supported_terminal_detected(self):
        self.assertEqual(self._with({"TERM_PROGRAM": "Apple_Terminal"}),
                         "apple_terminal")
        self.assertEqual(self._with({"TERM_PROGRAM": "iTerm.app"}), "iterm2")
        self.assertEqual(self._with({"WEZTERM_PANE": "1"}), "wezterm")
        self.assertEqual(self._with({"KITTY_WINDOW_ID": "1"}), "kitty")

    def test_ghostty_detected(self):
        self.assertEqual(self._with({"TERM_PROGRAM": "ghostty"}), "ghostty")

    def test_wezterm_and_kitty_argv_when_binary_present(self):
        with mock.patch.object(to.shutil, "which", lambda _b: "/usr/bin/fake"):
            wz = to.build_open_plan("@a:x", "wezterm")
            self.assertEqual(wz["method"], "exec")
            self.assertIn("wezterm", wz["argv"][0])
            kt = to.build_open_plan("@a:x", "kitty")
            self.assertEqual(kt["method"], "exec")
            self.assertIn("kitty", kt["argv"][0])

    def test_unknown_terminal_falls_back(self):
        out = self._with({"TERM_PROGRAM": "MysteryTerm"})
        self.assertIsInstance(out, str)
        self.assertNotIn(out, ("apple_terminal", "iterm2", "wezterm", "kitty"))


class PlanBuilding(unittest.TestCase):
    def test_plan_for_every_terminal_names_the_attach_command(self):
        for term in ("apple_terminal", "iterm2", "wezterm", "kitty", "unknown"):
            plan = to.build_open_plan("@a:x", term)
            self.assertIn("@a:x", str(plan), term)

    def test_non_default_instance_lands_in_the_attach_command(self):
        plan = to.build_open_plan("@a:x", "unknown", instance="work")
        self.assertEqual(plan["command"], "sutando attach @a:x --instance work")
        # the default instance keeps the bare command (single-instance world)
        plan = to.build_open_plan("@a:x", "unknown", instance="default")
        self.assertEqual(plan["command"], "sutando attach @a:x")

    def test_applescript_tab_vs_window(self):
        tab = to.applescript_for("echo hi", window=False)
        win = to.applescript_for("echo hi", window=True)
        self.assertIn("echo hi", tab)
        self.assertIn("echo hi", win)
        self.assertNotEqual(tab, win)


# The registry preserves an identity verbatim and instance_key accepts shell
# metacharacters, so everything below arrives from stored data, not a literal.
INJECT = "agent;echo${IFS}OPEN_INJECTED"


class IdentityIsDataNotCode(unittest.TestCase):
    def _plan(self, terminal, agent=INJECT, **kw):
        with mock.patch.object(to.shutil, "which", lambda _b: "/usr/bin/fake"):
            return to.build_open_plan(agent, terminal, **kw)

    def test_no_terminal_wraps_the_attach_in_a_shell(self):
        for term in ("wezterm", "kitty"):
            argv = self._plan(term)["argv"]
            self.assertNotIn("sh", argv, term)
            self.assertNotIn("-c", argv, term)

    def test_the_identity_stays_one_argv_element(self):
        for term in ("wezterm", "kitty"):
            argv = self._plan(term)["argv"]
            self.assertIn(INJECT, argv, term)
            self.assertEqual(argv[-1], INJECT, term)

    def test_instance_is_its_own_argv_element_too(self):
        argv = self._plan("wezterm", agent="@a:x", instance=INJECT)["argv"]
        self.assertEqual(argv[-2:], ["--instance", INJECT])

    def test_the_shell_form_survives_a_real_shell(self):
        # `do script` takes a command STRING, so this form is quoted, not argv.
        # A fake `sutando` on PATH reports the argv a real shell handed it.
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "sutando"
            fake.write_text('#!/bin/sh\nprintf "ARGV:%s\\n" "$@"\n')
            fake.chmod(0o755)
            command = self._plan("apple_terminal")["command"]
            env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}"}
            got = subprocess.run(["sh", "-c", command], capture_output=True,
                                 text=True, env=env, timeout=20)
        # Exact dump: the id arrives as ONE argument and nothing else runs.
        # Unquoted, `echo` prints a bare OPEN_INJECTED line of its own.
        self.assertEqual(got.stdout, f"ARGV:attach\nARGV:{INJECT}\n")

    def test_a_quote_cannot_end_the_applescript_literal(self):
        plan = self._plan("apple_terminal", agent='a"x')
        script = plan["script"]
        body = script.split('do script "', 1)[1].rsplit('"', 1)[0]
        # Every quote inside the literal is escaped, so none can terminate it.
        self.assertNotIn('"', body.replace('\\"', ""))
        self.assertIn('\\"', body)

    def test_a_backslash_and_newline_are_escaped_too(self):
        script = self._plan("apple_terminal", agent="a\\b\nc")["script"]
        body = script.split('do script "', 1)[1].rsplit('"', 1)[0]
        self.assertNotIn("\n", body)
        self.assertIn("\\\\", body)

    def test_control_an_ordinary_id_is_left_alone(self):
        # Without this the assertions above would pass on a filter that
        # mangled every id, which would break `open` for everyone.
        plan = self._plan("wezterm", agent="@a:x")
        self.assertEqual(plan["argv"][-1], "@a:x")
        self.assertEqual(plan["command"], "sutando attach @a:x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
