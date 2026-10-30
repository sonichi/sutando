"""Recovery exercises production observation validation, durable grants and fenced key transport."""
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


def failed_record(now, kind="server-overloaded"):
    return dict(schema=1, seat="core", observer="codex-observer", observer_version="test",
                observer_id="test", observer_started_at=now-100, session="core", claude_session_id=None,
                heartbeat_at=now, changed_at=now-40, seq=1, condition_since=now-40,
                last_success_at=None, phase="failed", motion="idle", condition="abnormal",
                reason="api-error" if kind == "server-overloaded" else kind, core_incarnation="7:start",
                failed_task=dict(turn_id="turn", prompt_sha256="a"*64, core_incarnation="7:start",
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
        return recovery.recover(self.ws, "core", "7:start", "socket", "%7", now=self.now)

    def test_terminal_error_allows_only_one_probe_and_never_grants_ready(self):
        self.assertFalse(ready(self.ws, "core", "7:start", now=self.now))
        self.assertTrue(self.attempt())
        self.assertEqual([x.args[-1] for x in self.send.call_args_list], [recovery.BOOTSTRAP_PROMPT, "C-m"])
        self.assertFalse(self.attempt())
        self.assertFalse(ready(self.ws, "core", "7:start", now=self.now))
        self.assertEqual(self.send.call_count, 2)
        self.assertNotIn("a" * 64, (self.ws/"state/codex-recovery.json").read_text())

    def test_unresolved_owner_intent_never_replays_or_releases_next_task(self):
        mark(self.ws, "task-a.txt", "7:start", "private task")
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
        self.assertIsNone(recovery.candidate(self.ws, "core", "7:start", self.now+100))
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
        for raw in ["bad", "[]", "x"*4097, '{"incarnation":"7:start"}']:
            path.write_text(raw)
            self.assertFalse(self.attempt())
        path.write_text(json.dumps({"incarnation":"7:start", "claimed_at":self.now-100, "terminal_at":self.now-110}))
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
        for _ in range(2): refused(self.ws, "private-task.txt", "core", "7:start")
        path = self.ws/"state/codex-pickup.json"
        self.assertEqual(json.loads(path.read_text())["counts"]["refused"], 1)
        self.assertNotIn("private-task", path.read_text())
        self.assertIn("api-error", warning(self.ws))
        record_event(self.ws, "submitted", "private-task.txt")
        self.assertIsNone(warning(self.ws))


if __name__ == "__main__":
    unittest.main()
