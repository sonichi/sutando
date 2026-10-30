#!/usr/bin/env python3
"""Run the production notifier scheduler with a simulated terminal and real completion receipts."""
# Regression: a completed parent turn must release the inbox while its worker result is pending.
# Run the scheduler case with PICKUP_TEST_NOTIFIER pointing to the base notifier to reproduce the failure.
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(os.environ.get("PICKUP_TEST_REPO", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "src"))
from delivery import codex_pickup as pickup
from delivery.codex_pickup import mark, paths, yielded, record_event, warning, ready
from runtime_observation import write


def idle_record(incarnation="7:start", session="core"):
    now = time.time()
    return dict(schema=1, observer="codex-observer", observer_version="test", observer_id="test",
                observer_started_at=now-100, seat="core", session=session, claude_session_id=None,
                seq=2, changed_at=now, condition_since=None, last_success_at=now, heartbeat_at=now,
                phase="idle", motion="idle", condition="healthy", reason=None,
                core_incarnation=incarnation)


def observation(ws, filename, incarnation="7:start", digest=None):
    marker = paths(ws)[0] / filename
    rec = idle_record(incarnation)
    rec["heartbeat_at"] += .01
    rec["completed_task"] = dict(prompt_sha256=digest or marker.read_text().splitlines()[1],
                                 accepted_at=marker.stat().st_mtime + .001,
                                 completed_at=rec["heartbeat_at"], core_incarnation=incarnation)
    write(rec, ws)
    return rec


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        mark(self.ws, "task-a.txt", "7:start", "private prompt")

    def test_missing_observation_refuses_first_submission(self):
        self.assertFalse(ready(self.ws, "core", "7:start"))

    def test_exact_completed_turn_is_durable_when_observer_moves_on(self):
        observation(self.ws, "task-a.txt")
        self.assertTrue(yielded(self.ws, "task-a.txt", "7:start", "core"))
        (self.ws / "state/runtime-observations/core.json").unlink()
        self.assertTrue(yielded(self.ws, "task-a.txt", "7:start", "core"))
        self.assertFalse(yielded(self.ws, "task-a.txt", "8:new", "core"))

    def test_wrong_prompt_stale_observer_and_wrong_incarnation_do_not_yield(self):
        observation(self.ws, "task-a.txt", digest="0" * 64)
        self.assertFalse(yielded(self.ws, "task-a.txt", "7:start", "core"))
        observation(self.ws, "task-a.txt", incarnation="8:new")
        self.assertFalse(yielded(self.ws, "task-a.txt", "7:start", "core"))
        observation(self.ws, "task-a.txt")
        self.assertFalse(yielded(self.ws, "task-a.txt", "7:start", "core", now=time.time()+100))
        self.assertFalse(yielded(self.ws, "task-a.txt", "", "core"))

    def test_local_evidence_contains_no_prompt_or_filename(self):
        record_event(self.ws, "startup")
        for _ in range(70):
            record_event(self.ws, "send_failed", "private-account-task.txt", 1)
        raw = (self.ws / "state/codex-pickup.json").read_text()
        self.assertNotIn("private", raw)
        self.assertEqual(len(json.loads(raw)["recent"]), 64)
        self.assertEqual(json.loads(raw)["counts"]["send_failed"], 70)

    def test_uncertain_deadline_warns_once_and_restart_rebuilds_warning(self):
        self.assertFalse(yielded(self.ws, "task-a.txt", "7:start", "core", timeout=0))
        self.assertIsNotNone(warning(self.ws))
        self.assertFalse(yielded(self.ws, "task-a.txt", "7:start", "core", timeout=0))
        record = json.loads((self.ws / "state/codex-pickup.json").read_text())
        self.assertEqual(record["counts"]["held"], 1)
        record_event(self.ws, "startup")
        self.assertIsNone(warning(self.ws))
        self.assertTrue((paths(self.ws)[0] / "task-a.txt").exists())
        for malformed in [[], {"blocked": []}, {"blocked": True}]:
            (self.ws / "state/codex-pickup.json").write_text(json.dumps(malformed))
            self.assertIsNone(warning(self.ws))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    def invoke(self, command, *args, stdin=""):
        with mock.patch.object(sys, "argv", ["pickup", command, str(self.ws), *args]), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            return pickup.main()

    def test_cli_marks_and_releases_only_with_matching_runtime_and_completion(self):
        self.assertEqual(self.invoke("ready", "core", "7:start"), 1)
        self.assertEqual(self.invoke("mark", "task-a.txt", "7:start", stdin="private task text"), 0)
        marker = paths(self.ws)[0] / "task-a.txt"
        self.assertEqual(marker.read_text().splitlines()[0], "7:start")
        self.assertNotIn("private task text", marker.read_text())
        write(idle_record(), self.ws)
        self.assertEqual(self.invoke("ready", "core", "7:start"), 0)
        self.assertEqual(self.invoke("yielded", "task-a.txt", "7:start", "core"), 1)
        observation(self.ws, "task-a.txt")
        self.assertEqual(self.invoke("yielded", "task-a.txt", "7:start", "core"), 0)
        self.assertTrue((paths(self.ws)[1] / "task-a.txt").is_file())
        self.assertTrue(marker.is_file())

    def test_cli_refusal_diagnostics_distinguish_missing_identity_and_observer(self):
        self.assertEqual(self.invoke("refused", "private-task.txt", "core", "7:start"), 0)
        self.assertIn("observer-missing", warning(self.ws))
        write(idle_record(), self.ws)
        self.assertEqual(self.invoke("refused", "private-task.txt", "core", "8:replacement"), 0)
        self.assertIn("observer-incarnation-mismatch", warning(self.ws))
        record = idle_record()
        record.update(seq=3, observer="another-runtime")
        write(record, self.ws)
        self.assertEqual(self.invoke("refused", "private-task.txt", "core", "7:start"), 0)
        self.assertIn("observer-mismatch", warning(self.ws))
        evidence = (self.ws / "state/codex-pickup.json").read_text()
        self.assertNotIn("private-task.txt", evidence)
        self.assertNotIn("8:replacement", evidence)
        self.assertEqual(json.loads(evidence)["counts"]["refused"], 3)
        self.assertFalse(paths(self.ws)[0].exists())

    def test_cli_recovery_delegates_and_missing_observation_cannot_create_a_grant(self):
        self.assertEqual(self.invoke("recover", "core", "7:start", "socket", "%7"), 1)
        self.assertFalse((self.ws / "state/codex-recovery.json").exists())
        with mock.patch("delivery.codex_recovery.recover", return_value=True) as recover:
            self.assertEqual(self.invoke("recover", "core", "7:start", "socket", "%7"), 0)
            recover.assert_called_once_with(str(self.ws), "core", "7:start", "socket", "%7")

    def test_cli_refuses_oversized_prompt_invalid_request_and_write_failure(self):
        self.assertEqual(self.invoke("mark", "task-a.txt", "7:start", stdin="x" * (1024 * 1024 + 1)), 2)
        self.assertFalse(paths(self.ws)[0].exists())
        self.assertEqual(self.invoke("mark", "../escape", "7:start", stdin="task"), 2)
        self.assertEqual(self.invoke("unknown"), 2)
        self.assertEqual(self.invoke("ready", "core"), 2)
        self.assertEqual(self.invoke("record", "unknown-event"), 2)
        with mock.patch.object(pickup, "write_status", side_effect=OSError("disk unavailable")):
            self.assertEqual(self.invoke("record", "submitted", "task-a.txt"), 2)

    def test_cli_timing_and_hold_resolution_are_bounded_metadata(self):
        with mock.patch.object(pickup.time, "time", return_value=100):
            self.assertEqual(self.invoke("record", "enqueued", "task-a.txt"), 0)
        with mock.patch.object(pickup.time, "time", return_value=102):
            self.assertEqual(self.invoke("record", "submitted", "task-a.txt"), 0)
        path = self.ws / "state/codex-pickup.json"
        self.assertEqual(json.loads(path.read_text())["recent"][-1]["wait_ms"], 2000)
        self.assertEqual(self.invoke("record", "held", "task-a.txt"), 0)
        self.assertIsNotNone(warning(self.ws))
        self.assertEqual(self.invoke("record", "completed", "task-a.txt"), 0)
        self.assertIsNone(warning(self.ws))
        self.assertNotIn("task-a.txt", path.read_text())

    def test_corrupt_and_oversized_metadata_recovers_without_false_warning(self):
        path = self.ws / "state/codex-pickup.json"
        path.parent.mkdir(parents=True)
        for raw in ["[]", "{invalid", "x" * 65537]:
            with self.subTest(raw=raw[:8]):
                path.write_text(raw)
                self.assertIsNone(warning(self.ws))
                self.assertEqual(self.invoke("record", "enqueued", "task-a.txt"), 0)
                self.assertEqual(json.loads(path.read_text())["counts"]["enqueued"], 1)
                self.assertLess(path.stat().st_size, 65536)

    def test_oversized_receipt_cannot_release_a_task(self):
        self.assertEqual(self.invoke("mark", "task-a.txt", "7:start", stdin="task"), 0)
        observation(self.ws, "task-a.txt")
        (paths(self.ws)[0] / "task-a.txt").write_text("7:start\n" + "a" * 1024)
        self.assertEqual(self.invoke("yielded", "task-a.txt", "7:start", "core"), 1)
        self.assertFalse((paths(self.ws)[1] / "task-a.txt").exists())

    def test_send_cli_pins_pid_preserves_keys_and_propagates_guard_failure(self):
        with mock.patch.object(pickup.subprocess, "run", return_value=subprocess.CompletedProcess([], 75)) as run:
            self.assertEqual(self.invoke("send", "%7", "7:start", "-l", "--", "literal ; task"), 75)
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--expected-pid") + 1], "7")
            self.assertEqual(argv[argv.index("-t") + 1], "%7")
            self.assertEqual(argv[-3:], ["-l", "--", "literal ; task"])
            run.reset_mock()
            self.assertEqual(self.invoke("send", "core:0", "7:start", "C-m"), 2)
            self.assertEqual(self.invoke("send", "%7", "unknown", "C-m"), 2)
            run.assert_not_called()


class SchedulerTests(unittest.TestCase):
    def run_scheduler(self, scenario, runtime=None):
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp)
            for d in ["tasks", "results", "queue", "payload", "logs", "context"]:
                (ws/d).mkdir()
            for name, tier in [("task-a.txt", "low"), ("task-b.txt", "normal")]:
                (ws/"tasks"/name).write_text("task: test\n")
                (ws/"payload"/name).write_text(str(ws/"tasks"/name))
            (ws/"queue/task-a.txt").write_text("low")
            write(idle_record() if runtime is None else runtime, ws)
            script = Path(os.environ.get("PICKUP_TEST_NOTIFIER", REPO/"src/agent/codex/cli/task-notifier.sh")).read_text()
            names = ["has_result", "task_payload", "log_notifier", "queue_head", "submit_task",
                     "pickup_event", "finish_responsive_task", "reconcile_finished_pickups", "process_responsive_queue", "process_announced_queue"]
            functions = []
            for name in names:
                match = re.search(rf"^{name}\(\) \{{.*?^\}}", script, re.M|re.S)
                if match:
                    functions.append(match.group())
            proof = ws/"proof.py"
            proof.write_text('import sys\nsys.path.insert(0, '+repr(str(REPO/"tests"))+')\n'
                             +'import runpy\nm=runpy.run_path('+repr(str(Path(__file__).resolve()))+')\n'
                             +'m["observation"](__import__("pathlib").Path(sys.argv[1]), "task-a.txt")\n')
            harness = '''set -euo pipefail
core_incarnation() { printf '7:start'; }
core_is_idle() { [ ! -f "$WORKSPACE_DIR/busy" ]; }
wait_for_core_idle() { core_is_idle; }
clear_automation_leftover() { touch "$WORKSPACE_DIR/cleared"; return 1; }
mark_worker_stage() { return 0; }
prepare_workstream_context() { return 0; }
clear_workstream_context() { workstream_context_file=""; }
tmux() { return 0; }
deliver_prompt() {
  printf '%s\\n' "$1" >> "$WORKSPACE_DIR/sent"
  if [ -f "$PICKUP_PY" ]; then printf '%s' "$2" | "$NOTIFIER_PY" "$PICKUP_PY" mark "$WORKSPACE_DIR" "$1" '7:start'; fi
}
''' + '\n'.join(functions) + '\n' + scenario
            env = {"PATH":os.environ["PATH"], "HOME":temp, "WORKSPACE_DIR":temp,
                   "TASKS_DIR":str(ws/"tasks"), "RESULTS_DIR":str(ws/"results"), "queue_dir":str(ws/"queue"),
                   "PAYLOAD_DIR":str(ws/"payload"), "CONTEXT_DIR":str(ws/"context"),
                   "INFLIGHT_DIR":str(paths(ws)[0]), "YIELDED_DIR":str(paths(ws)[1]),
                   "PICKUP_PY":str(REPO/"src/delivery/codex_pickup.py"),
                   "DISPATCH_PY":str(REPO/"src/delivery/task_dispatch.py"), "NOTIFIER_PY":sys.executable,
                   "RESPONSIVE":"1", "SESSION":"core", "TMUX_SOCKET":str(ws/"socket"),
                   "COMPLETION_TIMEOUT":"120", "POLL_INTERVAL":".01", "workstream_context_file":"",
                   "PROOF":str(proof)}
            try:
                result = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, timeout=6)
            except subprocess.TimeoutExpired:
                raise AssertionError("notifier still waits for delegated result instead of accepting the next idle turn") from None
            self.assertEqual(result.returncode, 0, result.stderr)
            return (ws/"sent").read_text().splitlines() if (ws/"sent").exists() else []

    def test_restart_with_idle_looking_pane_requires_fresh_matching_idle_observer(self):
        for patch in [dict(phase="unknown", motion="unknown"), dict(phase="tool", motion="moving"),
                      dict(heartbeat_at=time.time()-100), dict(core_incarnation="8:other"),
                      dict(session="other"), dict(observer="other"), dict(core_incarnation=None),
                      dict(condition="unknown"), dict(condition="abnormal", reason="quota-limit")]:
            with self.subTest(patch=patch):
                record = idle_record()
                record.update(patch)
                self.assertEqual(self.run_scheduler("process_announced_queue", runtime=record), [])
        self.assertEqual(self.run_scheduler("process_announced_queue"), ["task-a.txt"])

    def test_completed_parent_turn_releases_next_task_without_worker_result(self):
        sent = self.run_scheduler('''process_announced_queue
printf normal > "$queue_dir/task-b.txt"
"$NOTIFIER_PY" "$PROOF" "$WORKSPACE_DIR"
process_announced_queue
process_announced_queue
[ -f "$INFLIGHT_DIR/task-a.txt" ]
[ -f "$YIELDED_DIR/task-a.txt" ]
[ ! -e "$RESULTS_DIR/task-a.txt" ]
''')
        self.assertEqual(sent, ["task-a.txt", "task-b.txt"])

    def test_empty_composer_without_acceptance_does_not_admit_higher_priority_task(self):
        sent = self.run_scheduler('''process_announced_queue
printf urgent > "$queue_dir/task-b.txt"
process_announced_queue
[ "$(wc -l < "$WORKSPACE_DIR/sent" | tr -d ' ')" = 1 ]
touch "$WORKSPACE_DIR/busy"
process_announced_queue
process_announced_queue
[ ! -e "$WORKSPACE_DIR/cleared" ]
''')
        self.assertEqual(sent, ["task-a.txt"])

    def test_busy_composer_with_completed_receipt_still_holds_next_task(self):
        sent = self.run_scheduler('''process_announced_queue
printf normal > "$queue_dir/task-b.txt"
"$NOTIFIER_PY" "$PROOF" "$WORKSPACE_DIR"
touch "$WORKSPACE_DIR/busy"
process_announced_queue
''')
        self.assertEqual(sent, ["task-a.txt"])

    def test_restart_reconciles_retained_context_without_watcher_announcement(self):
        sent = self.run_scheduler('''process_announced_queue
printf retained > "$CONTEXT_DIR/task-a.txt"
rm "$queue_dir/task-a.txt"
TASKS_DIR="$WORKSPACE_DIR/sentinels"
SUTANDO_INBOX_RESOLVER=enabled
reconcile_finished_pickups
[ -f "$INFLIGHT_DIR/task-a.txt" ]
[ -f "$CONTEXT_DIR/task-a.txt" ]
printf OK > "$RESULTS_DIR/task-a.txt"
reconcile_finished_pickups
[ ! -e "$INFLIGHT_DIR/task-a.txt" ]
[ ! -e "$CONTEXT_DIR/task-a.txt" ]
''')
        self.assertEqual(sent, ["task-a.txt"])

    def test_actual_result_and_removed_payload_release_and_clear_receipts(self):
        for action in ['printf OK > "$RESULTS_DIR/task-a.txt"', 'rm "$TASKS_DIR/task-a.txt"']:
            sent = self.run_scheduler('''process_announced_queue
printf normal > "$queue_dir/task-b.txt"
''' + action + '''
process_announced_queue
[ ! -f "$INFLIGHT_DIR/task-a.txt" ]
''')
            self.assertEqual(sent, ["task-a.txt", "task-b.txt"])


class TransportTests(unittest.TestCase):
    def run_transport(self, replacement=False, fail_paste=False):
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp)
            shim = ws / "pickup.py"
            shim.write_text("""import os, sys
from pathlib import Path
sys.path.insert(0, os.environ['REPO'] + '/src')
from delivery.codex_pickup import mark
root = Path(os.environ['WORKSPACE_DIR'])
if sys.argv[1] == 'mark':
    mark(sys.argv[2], sys.argv[3], sys.argv[4], sys.stdin.read())
    if os.environ.get('REPLACEMENT') == '1': (root/'replacement').touch()
elif sys.argv[1] == 'send':
    if sys.argv[-1] == 'C-m':
        assert (root/'state/codex-notifier-inflight/task-a.txt').exists()
        (root/'entered').write_text(sys.argv[3])
        (root/'staged').unlink()
    else:
        if os.environ.get('FAIL_PASTE') == '1': sys.exit(1)
        (root/'staged').touch()
""")
            source = (REPO / "src/agent/codex/cli/task-notifier.sh").read_text()
            functions = '\n'.join(re.search(rf"^{name}\(\) \{{.*?^\}}", source, re.M|re.S).group()
                                  for name in ["deliver_prompt", "send_prompt_keys"])
            harness = """set -euo pipefail
core_target() { printf '=core:0'; }
pane_incarnation() { printf '7:start'; }
core_incarnation() { if [ -f "$WORKSPACE_DIR/replacement" ]; then printf '8:new'; else printf '7:start'; fi; }
tmux() { printf '%%7'; }
wait_for_composer() { return 0; }
prompt_is_staged() { [ -f "$WORKSPACE_DIR/staged" ]; }
has_result() { return 1; }
log_notifier() { :; }
""" + functions + "\ndeliver_prompt task-a.txt 'Sutando task ready: task-a.txt. synthetic'\n"
            env = {"PATH": os.environ['PATH'], "HOME": temp, "WORKSPACE_DIR": temp, "REPO": str(REPO),
                   "PICKUP_PY": str(shim), "NOTIFIER_PY": sys.executable, "TMUX_SOCKET": str(ws/'socket'),
                   "SESSION": "core", "TRACK_INFLIGHT": "1", "POLL_INTERVAL": ".01", "SUBMIT_CONFIRM_TIMEOUT": "1", "SUBMIT_RETRIES": "1",
                   "REPLACEMENT": str(int(replacement)), "FAIL_PASTE": str(int(fail_paste))}
            proc = subprocess.run(['bash', '-c', harness], env=env, capture_output=True, text=True, timeout=5)
            return proc.returncode, (ws/'entered').read_text() if (ws/'entered').exists() else None, (paths(ws)[0]/'task-a.txt').exists(), proc.stderr

    def test_actual_delivery_marks_before_enter_and_pins_pane(self):
        rc, pane, marked, err = self.run_transport()
        self.assertEqual(rc, 0, err)
        self.assertEqual(pane, '%7')
        self.assertTrue(marked)

    def test_replacement_after_mark_never_gets_enter(self):
        rc, pane, marked, err = self.run_transport(replacement=True)
        self.assertNotEqual(rc, 0, err)
        self.assertIsNone(pane)
        self.assertTrue(marked, 'unknown delivery intent must survive')

    def test_failed_paste_has_no_enter_or_submit_receipt(self):
        rc, pane, marked, err = self.run_transport(fail_paste=True)
        self.assertNotEqual(rc, 0, err)
        self.assertIsNone(pane)
        self.assertFalse(marked)


if __name__ == "__main__":
    unittest.main()
