#!/usr/bin/env python3
"""src/core_block_signal.py: hook events -> one record per core pane -> a verdict the
supervisor can trust, or None so it reads the screen as before.

Run: python3 tests/core-block-signal.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import io
import json
import os
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
import core_block_signal as cbs  # noqa: E402

SOCK = "/tmp/test-sutando.sock"


class TestEventState(unittest.TestCase):
    def test_a_permission_request_is_blocked(self):
        self.assertEqual(cbs.event_state({"hook_event_name": "PermissionRequest"}), ("blocked", "permission"))

    def test_dialog_notifications_are_blocked(self):
        for ntype, reason in (("permission_prompt", "permission"), ("elicitation_dialog", "question")):
            self.assertEqual(cbs.event_state({"hook_event_name": "Notification", "notification_type": ntype}),
                             ("blocked", reason))

    def test_idle_prompt_says_nothing(self):
        # The empty composer at rest: not a dialog, and not evidence that none is up either.
        for ntype in ("idle_prompt", "auth_success", None):
            self.assertIsNone(cbs.event_state({"hook_event_name": "Notification", "notification_type": ntype}))

    def test_turn_progress_is_clear(self):
        for ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "Stop"):
            self.assertEqual(cbs.event_state({"hook_event_name": ev}), ("clear", None))

    def test_other_events_say_nothing(self):
        for ev in ("SessionStart", "SubagentStop", "PreCompact", "", None):
            self.assertIsNone(cbs.event_state({"hook_event_name": ev}))
        self.assertIsNone(cbs.event_state("not a dict"))

    def test_both_runtimes_register_only_events_that_speak(self):
        for ev in cbs.CLAUDE_EVENTS + cbs.CODEX_EVENTS:
            payload = {"hook_event_name": ev, "notification_type": "permission_prompt"}
            self.assertIsNotNone(cbs.event_state(payload), ev)
        self.assertNotIn("Notification", cbs.CODEX_EVENTS)


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_and_no_temp_left(self):
        p = cbs.write(self.state, SOCK, "%3", 4242, "blocked", "permission", "PermissionRequest",
                      "claude", "sutando-core", now=100.0)
        rec = cbs.read(self.state, SOCK, "%3")
        self.assertEqual(rec["state"], "blocked")
        self.assertEqual(rec["reason"], "permission")
        self.assertEqual(rec["pane_pid"], 4242)
        self.assertEqual(rec["source"], "hook")
        self.assertEqual(rec["ts"], 100.0)
        self.assertEqual([x.name for x in p.parent.iterdir()], [p.name])

    def test_a_clear_record_carries_no_reason(self):
        cbs.write(self.state, SOCK, "%3", 1, "clear", "permission", "Stop", "claude", "s")
        self.assertIsNone(cbs.read(self.state, SOCK, "%3")["reason"])

    def test_an_invalid_state_is_refused(self):
        with self.assertRaises(ValueError):
            cbs.write(self.state, SOCK, "%3", 1, "maybe", None, "Stop", "claude", "s")

    def test_panes_of_two_servers_do_not_share_a_record(self):
        cbs.write(self.state, SOCK, "%0", 1, "blocked", "permission", "PermissionRequest", "claude", "s")
        self.assertIsNone(cbs.read(self.state, "/tmp/other.sock", "%0"))

    def test_garbage_reads_as_no_record(self):
        p = cbs.record_path(self.state, SOCK, "%3")
        p.parent.mkdir(parents=True)
        p.write_text("{not json")
        self.assertIsNone(cbs.read(self.state, SOCK, "%3"))
        p.write_text(json.dumps({"schema": 99, "state": "clear"}))
        self.assertIsNone(cbs.read(self.state, SOCK, "%3"))


class TestVerdict(unittest.TestCase):
    def rec(self, state, ts, pid=7, reason=None):
        return {"schema": 1, "state": state, "reason": reason, "pane_pid": pid, "ts": ts}

    def test_no_record_or_no_pid_is_no_verdict(self):
        self.assertIsNone(cbs.verdict(None, 7, 1000.0))
        self.assertIsNone(cbs.verdict(self.rec("clear", 1000.0), None, 1000.0))

    def test_another_process_record_is_no_verdict(self):
        self.assertIsNone(cbs.verdict(self.rec("clear", 1000.0, pid=8), 7, 1000.0))
        self.assertIsNone(cbs.verdict(self.rec("blocked", 1000.0, pid=8), 7, 1000.0))

    def test_a_fresh_clear_vouches(self):
        self.assertEqual(cbs.verdict(self.rec("clear", 1000.0), 7, 1000.0 + cbs.CLEAR_TTL_S), ("clear", None))

    def test_a_stale_clear_does_not(self):
        self.assertIsNone(cbs.verdict(self.rec("clear", 1000.0), 7, 1000.0 + cbs.CLEAR_TTL_S + 1))

    def test_a_clear_from_the_future_does_not(self):
        self.assertIsNone(cbs.verdict(self.rec("clear", 2000.0), 7, 1000.0))

    def test_a_blocked_record_stands_while_its_process_does(self):
        self.assertEqual(cbs.verdict(self.rec("blocked", 0.0, reason="question"), 7, 10 ** 6),
                         ("blocked", "question"))


class TestHookEntry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, payload, env=None, ident=("sutando-core", "%5", 999)):
        env = {"TMUX": f"{SOCK},123,0", "TMUX_PANE": "%5"} if env is None else env
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(cbs, "pane_identity", return_value=ident), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
            for k in ("TMUX", "TMUX_PANE"):
                if k not in env:
                    os.environ.pop(k, None)
            rc = cbs.main(["hook", "--runtime", "codex", "--state-dir", self.state])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "", "hook stdout is read by the CLI as hook output")
        return cbs.read(self.state, SOCK, "%5")

    def test_a_permission_request_writes_blocked(self):
        rec = self.run_hook({"hook_event_name": "PermissionRequest", "session_id": "x"})
        self.assertEqual((rec["state"], rec["runtime"], rec["session"], rec["pane_pid"]),
                         ("blocked", "codex", "sutando-core", 999))

    def test_idle_prompt_writes_nothing(self):
        self.assertIsNone(self.run_hook({"hook_event_name": "Notification", "notification_type": "idle_prompt"}))

    def test_outside_tmux_writes_nothing(self):
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop"}, env={}))

    def test_an_unreadable_pane_writes_nothing(self):
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop"}, ident=None))

    def test_bad_stdin_and_write_failures_fail_open(self):
        with mock.patch.object(sys, "stdin", io.StringIO("{oops")):
            self.assertEqual(cbs.main(["hook", "--runtime", "claude", "--state-dir", self.state]), 0)
        with mock.patch.object(cbs, "write", side_effect=OSError("disk")):
            self.assertIsNone(self.run_hook({"hook_event_name": "Stop"}))


class TestCodexArgs(unittest.TestCase):
    def test_each_event_is_a_valid_toml_override_and_trust_is_bypassed(self):
        repo = "/tmp/a repo/with 'quote\" and \\slash"
        args = cbs.codex_args(repo)
        self.assertEqual(args[-1], "--dangerously-bypass-hook-trust")
        pairs = args[:-1]
        self.assertEqual(pairs[0::2], ["-c"] * len(cbs.CODEX_EVENTS))
        seen = []
        for value in pairs[1::2]:
            key, _, toml_value = value.partition("=")
            ev = key.split(".", 1)[1]
            seen.append(ev)
            groups = tomllib.loads(f"v = {toml_value}")["v"]
            handler = groups[0]["hooks"][0]
            self.assertEqual(handler["type"], "command")
            self.assertTrue(handler["command"].startswith("bash "))
            self.assertTrue(handler["command"].endswith(" codex"))
            self.assertIn("core-block-hook.sh", handler["command"])
            self.assertIn("with 'quote\" and \\slash", handler["command"].replace("'\"'\"'", "'"))
        self.assertEqual(tuple(seen), cbs.CODEX_EVENTS)

    def test_the_cli_prints_nul_separated_args(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cbs.main(["codex-args", "/tmp/r"])
        self.assertEqual(out.getvalue().split("\0")[:-1], cbs.codex_args("/tmp/r"))


if __name__ == "__main__":
    unittest.main()
