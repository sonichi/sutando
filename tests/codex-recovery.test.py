"""Recovery exercises production observation validation, durable grants and fenced key transport."""
from datetime import datetime, timezone
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(os.environ.get("RECOVERY_TEST_REPO", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "src"))
from delivery import codex_recovery as recovery
from delivery.codex_pickup import mark, ready, refused, record_event, warning
from runtime_observation import write, validate

INCARNATION = "7:" + datetime.fromtimestamp(time.time()-100, timezone.utc).strftime("%a %b %d %H:%M:%S %Y")


def failed_record(now, kind="server-overloaded"):
    return dict(schema=1, seat="core", observer="codex-observer", observer_version="test",
                observer_id="test", observer_started_at=now-100, session="core", claude_session_id=None,
                heartbeat_at=now, changed_at=now-40, seq=1, condition_since=now-40,
                last_success_at=None, phase="failed", motion="idle", condition="abnormal",
                reason="api-error" if kind == "server-overloaded" else kind, core_incarnation=INCARNATION,
                failed_task=dict(turn_id="turn", prompt_sha256="a"*64, core_incarnation=INCARNATION,
                                 accepted_at=now-50, completed_at=now-40, error_kind=kind))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.now = time.time()
        self.record = failed_record(self.now)
        self.publish(self.record)
        self.capture = mock.patch.object(recovery, "capture", side_effect=[
            "›\ngpt-test · 100% left", "› " + recovery.BOOTSTRAP_PROMPT + "\ngpt-test · 100% left"]).start()
        self.send = mock.patch.object(recovery, "send_keys", return_value=0).start()
        self.identity = mock.patch.object(recovery, "same_incarnation", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def publish(self, record):
        self.seq = getattr(self, "seq", 0) + 1
        write({**record, "seq": self.seq}, self.ws)

    def attempt(self):
        return recovery.recover(self.ws, "core", INCARNATION, "socket", "%7", now=self.now)

    def test_short_probe_and_colored_footer_send_once_through_production_guard(self):
        footer = "  \x1b[38;5;223mgpt-test default\x1b[39m · /tmp/test…  ⚠ 2 warnings · f2 to view"
        self.assertLessEqual(len(recovery.BOOTSTRAP_PROMPT) + 2, 80)
        self.capture.side_effect = ["›\n" + footer,
                                   "\x1b[1m›\x1b[0m " + recovery.BOOTSTRAP_PROMPT + "\n\n" + footer]
        self.assertTrue(self.attempt())
        self.assertFalse(self.attempt())
        self.assertEqual([call.args[-1] for call in self.send.call_args_list],
                         [recovery.BOOTSTRAP_PROMPT, "C-m"])

    def test_ambiguous_or_extra_composer_input_spends_claim_without_enter(self):
        prompt = recovery.BOOTSTRAP_PROMPT
        footer = "  \x1b[38;5;223mgpt-test default\x1b[39m · /tmp/test…  ⚠ 2 warnings · f2 to view"
        bodies = [prompt + " extra", prompt + " ", prompt + "\t", prompt + "\r",
                  prompt + "\v", prompt + "\x1b[2J", prompt + "\x1b[2m extra\x1b[0m",
                  prompt + "\n  user text", prompt + "\n\n",
                  "unknown draft\n  › " + prompt, prompt + "\n  » " + prompt,
                  prompt + "\n" + footer, prompt + "\n  ⚠ 2 warnings · f2 to view",
                  "Sutando managed liveness check. Reply exactly READY. Do not use tools, read\n"
                  "  or write files, or send messages."]
        captures = ["› " + body + "\n\n" + footer for body in bodies]
        captures += ["› " + prompt, "› " + prompt + "\n  unknown status"]
        for capture in captures:
            with self.subTest(capture=capture), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                write(self.record, workspace)
                self.capture.side_effect = ["›\n" + footer, capture]
                self.send.reset_mock()
                self.assertFalse(recovery.recover(workspace, "core", INCARNATION, "socket", "%7", self.now))
                self.assertEqual([call.args[-1] for call in self.send.call_args_list], [prompt])
                self.assertTrue((workspace / "state/codex-recovery.json").is_file())
                self.assertFalse(recovery.recover(workspace, "core", INCARNATION, "socket", "%7", self.now))
                self.assertEqual(self.send.call_count, 1)

    def test_terminal_error_allows_only_one_probe_and_never_grants_ready(self):
        self.assertFalse(ready(self.ws, "core", INCARNATION, now=self.now))
        self.assertTrue(self.attempt())
        self.assertEqual([x.args[-1] for x in self.send.call_args_list], [recovery.BOOTSTRAP_PROMPT, "C-m"])
        self.assertFalse(self.attempt())
        self.assertFalse(ready(self.ws, "core", INCARNATION, now=self.now))
        self.assertEqual(self.send.call_count, 2)
        self.assertNotIn("a" * 64, (self.ws/"state/codex-recovery.json").read_text())

    def test_unresolved_owner_intent_never_replays_or_releases_next_task(self):
        mark(self.ws, "task-a.txt", INCARNATION, "private task")
        self.assertFalse(self.attempt())
        self.send.assert_not_called()
        self.assertTrue((self.ws/"state/codex-notifier-inflight/task-a.txt").is_file())
        self.assertFalse((self.ws/"results/task-a.txt").exists())

    def test_login_quota_generic_error_and_missing_terminal_proof_stay_closed(self):
        for kind in ["needs-login", "quota-limit", "api-error"]:
            self.record = failed_record(self.now, kind)
            self.publish(self.record)
            self.assertFalse(self.attempt())
        self.record = failed_record(self.now)
        self.record.pop("failed_task")
        self.publish(self.record)
        self.assertFalse(self.attempt())
        self.send.assert_not_called()

    def test_resumed_old_terminal_tail_cannot_grant_a_current_process_probe(self):
        self.record["failed_task"]["accepted_at"] = self.now-1000
        self.record["failed_task"]["completed_at"] = self.now-900
        self.publish(self.record)
        self.assertFalse(self.attempt())
        self.send.assert_not_called()

    def test_unparseable_process_start_cannot_grant_a_probe(self):
        for incarnation in ["missing-date", "7:not-a-date"]:
            with self.subTest(incarnation=incarnation):
                record = failed_record(self.now)
                record["core_incarnation"] = incarnation
                record["failed_task"]["core_incarnation"] = incarnation
                self.publish(record)
                self.assertIsNone(recovery.candidate(self.ws, "core", incarnation, self.now))
        self.capture.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse((self.ws / "state/codex-recovery.json").exists())

    def test_cli_probe_is_single_use_and_transport_timeout_is_non_success(self):
        argv = ["recovery", str(self.ws), "core", INCARNATION, "socket", "%7"]
        self.capture.side_effect = subprocess.TimeoutExpired("tmux", 5)
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(recovery.main(), 2)
        self.send.assert_not_called()
        self.assertFalse((self.ws / "state/codex-recovery.json").exists())
        self.capture.side_effect = ["›\ngpt-test · 100% left",
                                    "› " + recovery.BOOTSTRAP_PROMPT + "\ngpt-test · 100% left"]
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(recovery.main(), 0)
            self.assertEqual(recovery.main(), 1)
        self.assertEqual(self.send.call_count, 2)
        self.assertFalse((self.ws / "results").exists())

    def test_subprocess_cli_uses_string_workspace_and_never_reuses_a_grant(self):
        wrapper = self.ws / "recovery-cli.py"
        wrapper.write_text("import sys\nfrom pathlib import Path\n"
                           + "sys.path.insert(0, " + repr(str(REPO / "src")) + ")\n"
                           + '''from delivery import codex_recovery as recovery
root = Path(sys.argv[1])
panes = iter(["›\\ngpt-test · 100% left",
              "› " + recovery.BOOTSTRAP_PROMPT + "\\ngpt-test · 100% left"])
recovery.capture = lambda *args: next(panes)
recovery.same_incarnation = lambda incarnation: True
def send(*args):
    assert (root / "state/codex-recovery.json").is_file()
    assert args[-1] in (recovery.BOOTSTRAP_PROMPT, "C-m")
    with (root / "keys").open("a") as stream:
        stream.write("enter\\n" if args[-1] == "C-m" else "paste\\n")
    return 0
recovery.send_keys = send
raise SystemExit(recovery.main())
''')
        command = [sys.executable, str(wrapper), str(self.ws), "core", INCARNATION, "socket", "%7"]
        first = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual((self.ws / "keys").read_text(), "paste\nenter\n")
        grant = (self.ws / "state/codex-recovery.json").read_bytes()
        second = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(second.returncode, 1, second.stderr)
        self.assertEqual((self.ws / "keys").read_text(), "paste\nenter\n")
        self.assertEqual((self.ws / "state/codex-recovery.json").read_bytes(), grant)
        self.assertFalse((self.ws / "results").exists())

    def test_backoff_stale_wrong_session_and_incarnation_stay_closed(self):
        for patch in [{"session": "other"},
                      {"core_incarnation": "8:new", "failed_task": None}]:
            rec = {**failed_record(self.now), **patch}
            self.publish(rec)
            self.assertFalse(self.attempt())
        rec = failed_record(self.now)
        rec["failed_task"]["completed_at"] = self.now-1
        self.publish(rec)
        self.assertFalse(self.attempt())
        self.assertIsNone(recovery.candidate(self.ws, "core", INCARNATION, self.now+100))
        self.send.assert_not_called()

    def test_busy_unknown_unreadable_or_real_draft_never_gets_input(self):
        for pane in ["", "unknown", "› owner's draft\ngpt-test · 100% left", "›\nWorking (esc to interrupt)"]:
            self.capture.side_effect = None
            self.capture.return_value = pane
            self.assertFalse(self.attempt())
        self.send.assert_not_called()

    def test_crash_or_failed_paste_consumes_grant_before_any_key(self):
        def send(*args):
            self.assertTrue((self.ws/"state/codex-recovery.json").is_file())
            return 1
        self.send.side_effect = send
        self.assertFalse(self.attempt())
        self.assertFalse(self.attempt())
        self.assertEqual(self.send.call_count, 1)

    def test_replacement_or_new_turn_after_paste_never_gets_enter(self):
        def send(*args):
            rec = failed_record(self.now)
            rec.pop("failed_task")
            rec.update(phase="requesting", motion="moving", seq=2)
            self.publish(rec)
            return 0
        self.send.side_effect = send
        self.assertFalse(self.attempt())
        self.assertEqual(self.send.call_count, 1)

    def test_wrong_pasted_text_or_pid_replacement_never_gets_enter(self):
        self.capture.side_effect = ["›\ngpt-test · 100% left", "› other draft\ngpt-test · 100% left"]
        self.assertFalse(self.attempt())
        self.assertEqual(self.send.call_count, 1)

    def test_corrupt_grant_never_retries_and_new_success_rearms_once(self):
        path = self.ws/"state/codex-recovery.json"
        for raw in ["bad", "[]", "x"*4097, '{"incarnation":INCARNATION}']:
            path.write_text(raw)
            self.assertFalse(self.attempt())
        path.write_text(json.dumps({"incarnation":INCARNATION, "claimed_at":self.now-100, "terminal_at":self.now-110}))
        self.record["last_completed_at"] = self.now-60
        self.publish(self.record)
        self.assertTrue(self.attempt())
        self.assertFalse(self.attempt())

    def test_failed_probe_with_partial_model_output_does_not_rearm(self):
        self.assertTrue(self.attempt())
        self.record["last_success_at"] = self.now+1
        self.record["heartbeat_at"] = self.now+2
        self.publish(self.record)
        self.assertFalse(self.attempt())
        self.assertEqual(self.send.call_count, 2)

    def test_replacement_pid_is_checked_before_each_key(self):
        self.identity.side_effect = [True, False]
        self.assertFalse(self.attempt())
        self.assertEqual(self.send.call_count, 1)

    def test_typed_terminal_schema_rejects_contradictions(self):
        for patch in [{"phase":"idle"}, {"motion":"moving"}, {"condition":"healthy", "reason":None}]:
            with self.assertRaises(ValueError): validate({**self.record, **patch})
        for patch in [{"core_incarnation":"other"}, {"turn_id":""}, {"error_kind":"unrecognized"},
                      {"completed_at":self.now+5}, {"prompt_sha256":"bad"}]:
            with self.assertRaises(ValueError): validate({**self.record, "failed_task":{**self.record["failed_task"], **patch}})

    def test_refusal_warning_is_deduplicated_private_and_cleared_on_submission(self):
        for _ in range(2): refused(self.ws, "private-task.txt", "core", INCARNATION)
        path = self.ws/"state/codex-pickup.json"
        self.assertEqual(json.loads(path.read_text())["counts"]["refused"], 1)
        self.assertNotIn("private-task", path.read_text())
        self.assertIn("api-error", warning(self.ws))
        record_event(self.ws, "submitted", "private-task.txt")
        self.assertIsNone(warning(self.ws))


class TransportBoundaryTests(unittest.TestCase):
    def test_process_identity_requires_a_live_pid_and_its_exact_utc_start(self):
        incarnation = "19:Fri Oct 30 16:00:00 2026"
        answers = [subprocess.CompletedProcess([], 0, stdout="  Fri Oct 30 16:00:00 2026\n"),
                   subprocess.CompletedProcess([], 0, stdout="Fri Oct 30 16:00:01 2026\n"),
                   subprocess.CompletedProcess([], 1, stdout="")]
        with mock.patch.object(recovery.subprocess, "run", side_effect=answers) as run:
            self.assertTrue(recovery.same_incarnation(incarnation))
            self.assertFalse(recovery.same_incarnation(incarnation))
            self.assertFalse(recovery.same_incarnation(incarnation))
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args.args[0], ["ps", "-o", "lstart=", "-p", "19"])
            self.assertEqual(run.call_args.kwargs["timeout"], 5)
            self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
            self.assertEqual(run.call_args.kwargs["env"]["TZ"], "UTC")
            run.reset_mock()
            self.assertFalse(recovery.same_incarnation("not-a-pid:start"))
            run.assert_not_called()

    def test_capture_preserves_terminal_attributes_and_propagates_transport_failure(self):
        pane = "›\n\x1b[2mgpt-test · 100% left\x1b[0m\n"
        with mock.patch.object(recovery.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, stdout=pane)) as run:
            self.assertEqual(recovery.capture("isolated.sock", "%19"), pane)
            self.assertEqual(run.call_args.args[0],
                             ["tmux", "-S", "isolated.sock", "capture-pane", "-e", "-p", "-t", "%19"])
            self.assertTrue(run.call_args.kwargs["check"])
            self.assertEqual(run.call_args.kwargs["timeout"], 5)
        with mock.patch.object(recovery.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, "tmux")):
            with self.assertRaises(subprocess.CalledProcessError):
                recovery.capture("isolated.sock", "%19")

    def test_prompt_cli_is_tool_free_and_malformed_invocation_is_not_success(self):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["recovery", "prompt"]), \
                mock.patch.object(recovery, "capture") as capture, \
                mock.patch.object(recovery.subprocess, "run") as run, redirect_stdout(output):
            self.assertEqual(recovery.main(), 0)
            capture.assert_not_called()
            run.assert_not_called()
        self.assertEqual(output.getvalue(), recovery.BOOTSTRAP_PROMPT + "\n")
        with mock.patch.object(sys, "argv", ["recovery"]):
            self.assertEqual(recovery.main(), 2)


if __name__ == "__main__":
    unittest.main()
