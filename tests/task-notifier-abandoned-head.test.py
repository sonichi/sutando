#!/usr/bin/env python3
"""A queued task archived without a result must not hold either notifier's queue head (#4563).

The decision is task_dispatch.py's `head-abandoned`, the one owner both notifiers call.
This pins the helper's contract, then runs each notifier's SHIPPED process_announced_queue,
queue_head, has_result and task_payload with only the pane-facing calls stubbed: an archived
head is dropped untyped and the task behind it is submitted.
Run: python3 tests/task-notifier-abandoned-head.test.py
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
    "claude": (REPO / "src" / "agent" / "claude" / "cli" / "task-notifier.sh", "wait_for_core_healthy"),
    "codex": (REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh", "wait_for_core_idle"),
}
sys.path.insert(0, str(REPO / "src"))
from delivery.task_dispatch import head_abandoned  # noqa: E402


def _function_text(name: str, text: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
    assert m, f"{name} not found"
    return m.group(0)


class HeadAbandonedContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.results = self.root / "results"
        self.results.mkdir()
        self.payload = self.root / "tasks" / "task-a.txt"
        self.payload.parent.mkdir()

    def _cli(self, payload: Path) -> int:
        return subprocess.run([sys.executable, str(DISPATCH), "head-abandoned", str(self.results),
                               "task-a.txt", str(payload)], capture_output=True, timeout=30).returncode

    def test_gone_payload_and_no_result_is_abandoned(self):
        self.assertTrue(head_abandoned(self.results, "task-a.txt", self.payload))
        self.assertEqual(self._cli(self.payload), 0)

    def test_a_live_payload_is_kept(self):
        self.payload.write_text("task: x\n")
        self.assertFalse(head_abandoned(self.results, "task-a.txt", self.payload))
        self.assertEqual(self._cli(self.payload), 1)

    def test_a_ready_result_is_not_abandoned_even_with_the_payload_gone(self):
        (self.results / "task-a.txt").write_text("done\n")
        self.assertFalse(head_abandoned(self.results, "task-a.txt", self.payload))

    def test_an_empty_placeholder_result_does_not_count_as_a_result(self):
        (self.results / "task-a.txt").write_text("  \n")
        self.assertTrue(head_abandoned(self.results, "task-a.txt", self.payload))

    def test_wrong_arity_is_a_usage_error_never_a_drop(self):
        r = subprocess.run([sys.executable, str(DISPATCH), "head-abandoned", str(self.results), "task-a.txt"],
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 2)


class ShippedQueueDropsAbandonedHead(unittest.TestCase):
    def _run(self, script: Path, wait_fn: str) -> tuple[str, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        tasks, results, queue, payload = (root / d for d in ("tasks", "results", "queue", "payload"))
        for d in (tasks, results, queue, payload):
            d.mkdir()
        # task-gone was queued first, then archived out of tasks/ with no result.
        (queue / "task-gone.txt").write_text("normal")
        time.sleep(1.1)
        (tasks / "task-next.txt").write_text("task: say OK\n")
        (queue / "task-next.txt").write_text("normal")
        submitted = root / "submitted.log"
        text = script.read_text()
        fns = "\n".join(_function_text(n, text) for n in
                        ("has_result", "task_payload", "queue_head", "process_announced_queue"))
        stubs = (
            f'{wait_fn}() {{ return 0; }}\n'
            'mark_worker_stage() { :; }\n'
            'log_notifier() { printf "%s\\n" "$*" >&2; }\n'
            # The core answers whatever is typed into it, so a typed head would not block here.
            f'submit_task() {{ echo "$1" >> {shlex.quote(str(submitted))}; '
            f'printf "OK\\n" > "$RESULTS_DIR/$1"; }}\n'
        )
        harness = (f'set -e\nqueue_dir={shlex.quote(str(queue))}\n' + stubs + fns
                   + '\nprocess_announced_queue\n')
        env = {"HOME": os.environ.get("HOME", ""), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "NOTIFIER_PY": sys.executable, "DISPATCH_PY": str(DISPATCH),
               "RESULTS_DIR": str(results), "TASKS_DIR": str(tasks), "WORKSPACE_DIR": str(root),
               "PAYLOAD_DIR": str(payload), "INFLIGHT_DIR": str(root / "inflight")}
        r = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stderr = r.stderr
        return (submitted.read_text() if submitted.exists() else ""), queue

    def test_both_notifiers_drop_an_archived_head_untyped_and_proceed(self):
        for runtime, (script, wait_fn) in NOTIFIERS.items():
            with self.subTest(runtime=runtime):
                submitted, queue = self._run(script, wait_fn)
                self.assertEqual(submitted, "task-next.txt\n",
                                 f"only the live task may be typed; stderr:\n{self.stderr}")
                self.assertEqual(sorted(p.name for p in queue.iterdir()), [])
                self.assertIn("dropped task-gone.txt from the queue", self.stderr)

    def test_both_notifiers_delegate_the_decision_to_task_dispatch(self):
        for runtime, (script, _) in NOTIFIERS.items():
            with self.subTest(runtime=runtime):
                fn = _function_text("process_announced_queue", script.read_text())
                self.assertEqual(fn.count('"$DISPATCH_PY" head-abandoned'), 1)
                self.assertNotIn("[ ! -e", fn, "the abandoned-head rule must not be re-derived in bash")


if __name__ == "__main__":
    unittest.main(verbosity=2)
