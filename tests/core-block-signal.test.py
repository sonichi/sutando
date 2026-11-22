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
import shutil
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


class TestClearIsScopedToTheInteraction(unittest.TestCase):
    """Only the call that blocked, or a turn boundary, clears a held dialog."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def ev(self, payload, pid=7):
        cbs.record_event(self.state, SOCK, "%1", pid, payload, "claude", "sutando-core")
        return cbs.read(self.state, SOCK, "%1")

    BASH_RM = {"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}

    def block(self, **extra):
        return self.ev({"hook_event_name": "PermissionRequest", **self.BASH_RM, **extra})

    def test_an_unrelated_tool_call_leaves_it_blocked(self):
        self.block(tool_use_id="t1")
        for ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            rec = self.ev({"hook_event_name": ev, "tool_use_id": "t2", "tool_name": "Read",
                           "tool_input": {"file_path": "x"}})
            self.assertEqual(rec["state"], "blocked", ev)

    def test_another_call_of_the_same_tool_leaves_it_blocked(self):
        self.block(tool_use_id="t1")
        rec = self.ev({"hook_event_name": "PostToolUse", "tool_use_id": "t2", **self.BASH_RM})
        self.assertEqual(rec["state"], "blocked")

    def test_the_blocking_call_finishing_clears_it(self):
        self.block(tool_use_id="t1")
        self.assertEqual(self.ev({"hook_event_name": "PostToolUse", "tool_use_id": "t1", **self.BASH_RM})["state"],
                         "clear")

    def test_without_ids_the_same_tool_and_input_clears_it_and_another_input_does_not(self):
        self.block()
        other = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
        self.assertEqual(self.ev(other)["state"], "blocked")
        self.assertEqual(self.ev({"hook_event_name": "PostToolUse", **self.BASH_RM})["state"], "clear")

    def test_turn_boundaries_clear_it(self):
        for ev in ("UserPromptSubmit", "Stop"):
            self.block(tool_use_id="t1")
            self.assertEqual(self.ev({"hook_event_name": ev})["state"], "clear", ev)

    def test_a_dialog_with_no_tool_is_cleared_only_by_a_turn_boundary(self):
        self.ev({"hook_event_name": "Notification", "notification_type": "elicitation_dialog"})
        self.assertEqual(self.ev({"hook_event_name": "PreToolUse", "tool_use_id": "t9", "tool_name": "Read",
                                  "tool_input": {}})["state"], "blocked")
        self.assertEqual(self.ev({"hook_event_name": "Stop"})["state"], "clear")

    def test_a_notification_after_the_request_keeps_its_interaction(self):
        self.block(tool_use_id="t1")
        rec = self.ev({"hook_event_name": "Notification", "notification_type": "permission_prompt"})
        self.assertEqual(rec["interaction"]["tool_use_id"], "t1")
        self.assertEqual(self.ev({"hook_event_name": "PostToolUse", "tool_use_id": "t1", **self.BASH_RM})["state"],
                         "clear")

    def test_a_record_from_the_previous_cli_does_not_hold(self):
        self.block(tool_use_id="t1")
        rec = self.ev({"hook_event_name": "PreToolUse", "tool_use_id": "t2", "tool_name": "Read",
                       "tool_input": {}}, pid=8)
        self.assertEqual((rec["state"], rec["pane_pid"]), ("clear", 8))

    def test_tool_progress_with_nothing_held_writes_clear(self):
        self.assertEqual(self.ev({"hook_event_name": "PreToolUse", "tool_use_id": "t1", "tool_name": "Read",
                                  "tool_input": {}})["state"], "clear")


FAKE_APP_SERVER = os.path.join(_HERE, "fixtures", "fake-codex-app-server.py")


class TestCodexArgs(unittest.TestCase):
    def test_each_event_is_a_valid_toml_override_and_trust_is_never_bypassed(self):
        repo = "/tmp/a repo/with 'quote\" and \\slash"
        args = cbs.codex_args(repo)
        self.assertNotIn("--dangerously-bypass-hook-trust", args)
        self.assertEqual(args[0::2], ["-c"] * len(cbs.CODEX_EVENTS))
        seen = []
        for value in args[1::2]:
            key, _, toml_value = value.partition("=")
            seen.append(key.split(".", 1)[1])
            handler = tomllib.loads(f"v = {toml_value}")["v"][0]["hooks"][0]
            self.assertEqual(handler["type"], "command")
            self.assertEqual(handler["command"], cbs.codex_hook_command(repo))
            self.assertIn("with 'quote\" and \\slash", handler["command"].replace("'\"'\"'", "'"))
        self.assertEqual(tuple(seen), cbs.CODEX_EVENTS)


class TestSeedCodexTrust(unittest.TestCase):
    """Trust is recorded through Codex's own app-server, for exactly our hooks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.mkdir(self.home)
        self.codex = os.path.join(self.tmp.name, "codex")
        with open(self.codex, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} {FAKE_APP_SERVER} \"$@\"\n")
        os.chmod(self.codex, 0o755)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("FAKE_MODE", "FAKE_FOREIGN"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def seed(self):
        return cbs.seed_codex_trust("/tmp/r", self.home, self.tmp.name, codex_bin=self.codex, timeout=10)

    def writes(self):
        try:
            return [json.loads(x) for x in Path(self.home, "writes.jsonl").read_text().splitlines()]
        except OSError:
            return []

    def test_first_seed_trusts_all_five_then_is_idempotent(self):
        self.assertEqual(self.seed(), "trusted")
        w = self.writes()
        self.assertEqual(len(w), 1)
        self.assertEqual((w[0]["keyPath"], w[0]["mergeStrategy"]), ("hooks.state", "upsert"))
        self.assertEqual(len(w[0]["value"]), len(cbs.CODEX_EVENTS))
        self.assertEqual(self.seed(), "already-trusted")
        self.assertEqual(len(self.writes()), 1)

    def test_a_foreign_hook_is_never_trusted(self):
        os.environ["FAKE_FOREIGN"] = "1"
        self.assertEqual(self.seed(), "trusted")
        self.assertNotIn("/u/hooks.json:stop:0:0", self.writes()[0]["value"])

    def test_a_changed_command_is_trusted_again(self):
        self.seed()
        trust = json.loads(Path(self.home, "fake-trust.json").read_text())
        for v in trust.values():
            v["trusted_hash"] = "sha256:old"
        Path(self.home, "fake-trust.json").write_text(json.dumps(trust))
        self.assertEqual(self.seed(), "trusted")

    def test_failures_raise(self):
        os.environ["FAKE_MODE"] = "exit"
        with self.assertRaises(RuntimeError):
            self.seed()
        os.environ["FAKE_MODE"] = "stuck"
        with self.assertRaises(RuntimeError):
            self.seed()

    def test_the_cli_prints_args_only_once_trusted(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(cbs, "seed_codex_trust", return_value="trusted"):
            self.assertEqual(cbs.main(["codex-args", "/tmp/r", "--codex-home", self.home, "--cwd", "/"]), 0)
        self.assertEqual(out.getvalue().split("\0")[:-1], cbs.codex_args("/tmp/r"))
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(cbs, "seed_codex_trust", side_effect=RuntimeError("no")):
            self.assertEqual(cbs.main(["codex-args", "/tmp/r", "--codex-home", self.home, "--cwd", "/"]), 1)
        self.assertEqual(out.getvalue(), "")

    @unittest.skipUnless(shutil.which("codex"), "no codex CLI on PATH")
    def test_against_the_real_codex_in_a_scratch_home(self):
        # Codex's own hooks/list and config writer, never the user's Codex home.
        first = cbs.seed_codex_trust("/tmp/r", self.home, self.tmp.name, timeout=30)
        self.assertEqual(first, "trusted")
        self.assertEqual(cbs.seed_codex_trust("/tmp/r", self.home, self.tmp.name, timeout=30), "already-trusted")
        text = Path(self.home, "config.toml").read_text()
        self.assertEqual(text.count("trusted_hash"), len(cbs.CODEX_EVENTS))
        self.assertIn('"/<session-flags>/config.toml:stop:0:0"', text)


if __name__ == "__main__":
    unittest.main()
