#!/usr/bin/env python3
"""session_runtime is the one reading of a tmux session's SUTANDO_CORE_RUNTIME stamp.

Pins the contract (argv, parse, read) and that every reader delegates to it:
core_heartbeat._session_runtime, runtime-health._pane_blocks_dispatch and the three
health-check readers. Run: python3 tests/session-runtime.test.py
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import session_runtime  # noqa: E402
import core_heartbeat  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "src" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load("health_check", "health-check.py")
rh = _load("runtime_health", "runtime-health.py")


def _cp(rc, out=""):
    return subprocess.CompletedProcess([], rc, out, "")


class Contract(unittest.TestCase):
    def test_argv(self):
        self.assertEqual(session_runtime.argv("s"),
                         ["show-environment", "-t", "=s", "SUTANDO_CORE_RUNTIME"])

    def test_parse(self):
        p = session_runtime.parse
        self.assertEqual(p(0, "SUTANDO_CORE_RUNTIME=codex\n"), "codex")
        self.assertEqual(p(0, b"SUTANDO_CORE_RUNTIME= claude \n"), "claude")
        self.assertIsNone(p(0, "-SUTANDO_CORE_RUNTIME\n"))   # unset in the session
        self.assertIsNone(p(0, "SUTANDO_CORE_RUNTIME=\n"))   # empty value
        self.assertIsNone(p(0, "OTHER=codex\n"))
        self.assertIsNone(p(0, None))
        self.assertIsNone(p(1, "SUTANDO_CORE_RUNTIME=codex\n"))
        self.assertIsNone(p(None, ""))                       # tmux could not run

    def test_read_uses_the_callers_runner(self):
        seen = []
        def run(*args):
            seen.append(args)
            return _cp(0, "SUTANDO_CORE_RUNTIME=codex\n")
        self.assertEqual(session_runtime.read("core", run), "codex")
        self.assertEqual(seen, [tuple(session_runtime.argv("core"))])
        self.assertIsNone(session_runtime.read("core", lambda *a: None))


class Delegation(unittest.TestCase):
    """Each reader asks session_runtime, through its own tmux runner."""

    def _spy(self, answer):
        calls = []
        def read(session, run):
            calls.append((session, run))
            return answer(session) if callable(answer) else answer
        return calls, read

    def test_core_heartbeat(self):
        calls, read = self._spy("codex")
        with mock.patch.object(core_heartbeat.session_runtime, "read", read), \
             mock.patch.object(core_heartbeat, "_tmux", return_value="R") as tmux:
            self.assertEqual(core_heartbeat._session_runtime("/sock", "sess"), "codex")
            self.assertEqual(calls[0][0], "sess")
            self.assertEqual(calls[0][1]("a", "b"), "R")
            tmux.assert_called_once_with("/sock", "a", "b")

    def test_runtime_health(self):
        argvs, parsed = [], []
        def run(argv):
            argvs.append(argv)
            return 0, "SUTANDO_CORE_RUNTIME=codex\n"
        def parse(rc, out):
            parsed.append((rc, out))
            return "codex"
        with mock.patch.object(rh, "_run", run), \
             mock.patch.object(rh, "_tmux_socket", return_value="/sock"), \
             mock.patch.object(rh.session_runtime, "parse", parse), \
             mock.patch.object(rh.cli_wedge, "core_target", return_value=None):
            self.assertIsNone(rh._pane_blocks_dispatch("/ws"))
        self.assertEqual(argvs, [["tmux", "-S", "/sock", *session_runtime.argv(rh.SESSION)]])
        self.assertEqual(parsed, [(0, "SUTANDO_CORE_RUNTIME=codex\n")])

    def _hc_runner(self, calls, tmux):
        self.assertEqual(calls[-1][1]("x"), "T")
        self.assertEqual(tmux.call_args.args[1:], ("x",))
        return tmux.call_args.args[0]

    def test_health_check_codex_target(self):
        target = {"socket": "/sock", "session": "core"}
        for answer, expect in (("codex", target), ("claude", None), (None, None)):
            calls, read = self._spy(answer)
            with mock.patch.object(hc.session_runtime, "read", read), \
                 mock.patch.object(hc, "_run_tmux", return_value="T") as tmux:
                tmux.return_value = _cp(0)  # has-session
                got = hc._local_codex_core_target(dict(target))
                self.assertEqual(got, expect)
                self.assertEqual(calls[-1][0], "core")
                tmux.return_value = "T"
                self.assertEqual(self._hc_runner(calls, tmux), "/sock")

    def test_health_check_claude_notifier_repair(self):
        target = {"socket": "/sock", "session": "core"}
        for answer, shown in (("codex", "(codex)"), (None, "(unreadable)")):
            calls, read = self._spy(answer)
            with mock.patch.object(hc.session_runtime, "read", read), \
                 mock.patch.object(hc, "_claude_runtime_selected", return_value=True), \
                 mock.patch.object(hc, "_fresh_local_core_record", return_value={"pid": 1}), \
                 mock.patch.object(hc, "_local_claude_notifier_target", return_value=target), \
                 mock.patch.object(hc, "_run_tmux", return_value="T") as tmux:
                verdict = hc.fix_claude_task_notifier()
                self.assertIn(f"records a different runtime {shown}", verdict)
                self.assertEqual(calls[-1][0], "core")
                self.assertEqual(self._hc_runner(calls, tmux), "/sock")

    def test_health_check_live_core_runtime(self):
        stamps = {"a": "codex", "b": None, "c": "codex"}
        calls, read = self._spy(stamps.get)
        with mock.patch.object(hc.session_runtime, "read", read), \
             mock.patch.object(hc, "_run_tmux", return_value="T") as tmux:
            self.assertEqual(hc._live_core_runtime("/sock", ["a", "b", "c"]), "codex")
            self.assertEqual([c[0] for c in calls], ["a", "b", "c"])
            self.assertEqual(self._hc_runner(calls, tmux), "/sock")
        stamps["b"] = "claude"
        with mock.patch.object(hc.session_runtime, "read", read):
            self.assertIsNone(hc._live_core_runtime("/sock", ["a", "b"]))  # conflicting


if __name__ == "__main__":
    unittest.main()
