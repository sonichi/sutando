#!/usr/bin/env python3
"""A submitted task whose payload is removed with no findable result must release the queue (#5753).

Another consumer (the desktop onboarding probe) can read and delete a task's result and archive
its task file; a notifier waiting on that result held every later task for COMPLETION_TIMEOUT.
Runs each notifier's SHIPPED submit_task, process_announced_queue, queue_head, has_result,
task_payload and log_notifier, with only the pane-facing calls stubbed: the delivery stub plays
the core and the other consumer.
Run: python3 tests/task-notifier-abandoned-inflight.test.py
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISPATCH = REPO / "src" / "delivery" / "task_dispatch.py"
NOTIFIERS = {
    "claude": dict(script=REPO / "src" / "agent" / "claude" / "cli" / "task-notifier.sh",
                   log="claude-task-notifier.log",
                   shipped=["submit_task", "submit_task_grown", "has_result", "task_payload",
                            "queue_head", "process_announced_queue", "log_notifier"],
                   stubs='wait_for_core_healthy() { return 0; }\n'
                         'task_prompt() { printf "Sutando task ready: %s" "$1"; }\n'
                         'grow_window() { return 0; }\nrows_for() { printf 20; }\n'
                         'restore_window() { return 0; }\ncapture_raw() { printf ""; }\n'
                         'core_incarnation() { printf inc-1; }\n'
                         'prompt_is_staged() { return 1; }\ncomposer_is_cut_prompt() { return 1; }\n'
                         'composer_holds_prompt() { return 1; }\n'),
    "codex": dict(script=REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh",
                  log="task-notifier.log",
                  shipped=["submit_task", "has_result", "task_payload", "queue_head",
                           "process_announced_queue", "log_notifier"],
                  stubs='wait_for_core_idle() { return 0; }\nmark_worker_stage() { return 0; }\n'
                        'workstream_context_file=""\nprepare_workstream_context() { return 0; }\n'
                        'clear_workstream_context() { return 0; }\n'),
}
DROP_LINE = "dropped task-first.txt from the queue: its task file is gone and no result was found"


def _function_text(name: str, text: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
    assert m, f"{name} not found"
    return m.group(0)


class InflightAbandonment(unittest.TestCase):
    # Bounded well under COMPLETION_TIMEOUT, so a notifier still waiting on the gone result fails.
    RUN_S = 20
    COMPLETION_TIMEOUT = 120

    def _run(self, runtime: str, on_first: str, completion_timeout: "int | None" = None
             ) -> tuple[list[tuple[str, float]], str, float]:
        cfg = NOTIFIERS[runtime]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        tasks, results, queue, payload, logs = (root / d for d in
                                                ("tasks", "results", "queue", "payload", "logs"))
        for d in (tasks, results, queue, payload, logs, tasks / "archive"):
            d.mkdir()
        for name in ("task-first.txt", "task-next.txt"):
            (tasks / name).write_text("task: say OK\n")
            (queue / name).write_text("normal")
            (payload / name).write_text(str(tasks / name))
            time.sleep(1.1)  # queue_head orders by marker mtime
        submitted = root / "submitted.log"
        text = cfg["script"].read_text()
        fns = "\n".join(_function_text(n, text) for n in cfg["shipped"])
        # The core answers task-next at once; task-first plays the scenario under test.
        deliver = (
            'deliver_prompt() {\n'
            f'  printf "%s %s\\n" "$1" "$(date +%s)" >> {shlex.quote(str(submitted))}\n'
            '  if [ "$1" = task-first.txt ]; then\n'
            f'{on_first}\n'
            '  else printf "OK\\n" > "$RESULTS_DIR/$1"; fi\n'
            '}\n'
        )
        harness = ('set -euo pipefail\n'
                   f'queue_dir={shlex.quote(str(queue))}\n'
                   'tmux() { return 0; }\n'
                   + cfg["stubs"] + deliver + fns + '\nprocess_announced_queue\n')
        env = {"HOME": str(root), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "NOTIFIER_PY": sys.executable, "DISPATCH_PY": str(DISPATCH),
               "RESULTS_DIR": str(results), "TASKS_DIR": str(tasks), "WORKSPACE_DIR": str(root),
               "PAYLOAD_DIR": str(payload), "INFLIGHT_DIR": str(root / "inflight"),
               "TMUX_SOCKET": str(root / "sock"), "SESSION": "core", "POLL_INTERVAL": "0.1",
               "COMPLETION_TIMEOUT": str(completion_timeout or self.COMPLETION_TIMEOUT)}
        start = time.time()
        try:
            r = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True,
                               timeout=self.RUN_S, cwd=str(root))
        except subprocess.TimeoutExpired:
            self.fail(f"{runtime}: the queue was still held after {self.RUN_S}s; submitted: "
                      + (submitted.read_text() if submitted.exists() else "<none>"))
        elapsed = time.time() - start
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [(ln.split()[0], float(ln.split()[1]))
                for ln in (submitted.read_text().splitlines() if submitted.exists() else [])]
        log = (logs / cfg["log"])
        self.stderr = r.stderr
        self.queue = queue
        return rows, (log.read_text() if log.exists() else ""), elapsed

    def _assert_released(self, runtime, rows, log):
        self.assertEqual([n for n, _ in rows], ["task-first.txt", "task-next.txt"], self.stderr)
        self.assertEqual(list(self.queue.iterdir()), [], "both tasks leave the queue")
        self.assertEqual(log.count(DROP_LINE), 1, f"{runtime} log:\n{log}")

    def test_payload_archived_with_no_result_releases_the_queue(self):
        archive = '    mv "$TASKS_DIR/$1" "$TASKS_DIR/archive/$1"'
        for runtime in NOTIFIERS:
            with self.subTest(runtime=runtime):
                rows, log, _ = self._run(runtime, archive)
                self._assert_released(runtime, rows, log)

    def test_result_published_then_deleted_after_archive_counts_as_answered(self):
        # The desktop poll: archive the task, then read and delete the result before any poll sees it.
        race = ('    mv "$TASKS_DIR/$1" "$TASKS_DIR/archive/$1"\n'
                '    printf "OK\\n" > "$RESULTS_DIR/$1"\n'
                '    rm -f "$RESULTS_DIR/$1"')
        for runtime in NOTIFIERS:
            with self.subTest(runtime=runtime):
                rows, log, _ = self._run(runtime, race)
                self._assert_released(runtime, rows, log)

    def test_a_present_payload_with_no_result_yet_still_waits(self):
        delay = 3
        late = (f'    ( sleep {delay}; printf "OK\\n" > "$RESULTS_DIR/$1" ) &')
        for runtime in NOTIFIERS:
            with self.subTest(runtime=runtime):
                rows, log, _ = self._run(runtime, late)
                self.assertEqual([n for n, _ in rows], ["task-first.txt", "task-next.txt"], self.stderr)
                self.assertGreaterEqual(rows[1][1] - rows[0][1], delay - 1,
                                        "task-next must wait for task-first's result")
                self.assertNotIn("dropped", log)

    def test_both_wait_loops_delegate_to_task_dispatch(self):
        for runtime, cfg in NOTIFIERS.items():
            with self.subTest(runtime=runtime):
                text = cfg["script"].read_text()
                fn = _function_text("submit_task_grown" if runtime == "claude" else "submit_task", text)
                self.assertEqual(fn.count('"$DISPATCH_PY" head-abandoned'), 1)
                self.assertNotIn("[ ! -e", fn, "the abandonment rule must not be re-derived in bash")

    def test_a_timed_out_wait_is_logged_to_the_notifier_log(self):
        # Payload stays present and no result arrives, so only the timeout can end the wait.
        for runtime in NOTIFIERS:
            with self.subTest(runtime=runtime):
                rows, log, _ = self._run(runtime, '    :', completion_timeout=1)
                self.assertEqual([n for n, _ in rows], ["task-first.txt"], self.stderr)
                self.assertIn("timed out waiting for result: task-first.txt", log,
                              f"{runtime} log:\n{log}")
                self.assertNotIn("dropped", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
