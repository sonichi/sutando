#!/usr/bin/env python3
"""A queued task archived without a result must not hold either notifier's queue head (#4563).

The decision is task_dispatch.py's `head-abandoned`, the one owner both notifiers call.
This pins the helper's contract, then runs each notifier's SHIPPED process_announced_queue,
queue_head, has_result and task_payload with only the pane-facing calls stubbed: an archived
head is dropped untyped and the task behind it is submitted. The Codex worker-stage
transition runs through the production writer (worker-pool's pool_delivery.py).
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
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISPATCH = REPO / "src" / "delivery" / "task_dispatch.py"
WORKER_STAGE = REPO / "src" / "delivery" / "worker-stage.sh"
POOL_DELIVERY = REPO / "skills" / "worker-pool" / "scripts" / "pool_delivery.py"
RECIPIENT = "worker-3"
NOTIFIERS = {
    "claude": (REPO / "src" / "agent" / "claude" / "cli" / "task-notifier.sh", "wait_for_core_healthy"),
    "codex": (REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh", "wait_for_core_idle"),
}
sys.path.insert(0, str(REPO / "src"))
from delivery.task_dispatch import _main, head_abandoned  # noqa: E402


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

    def test_cli_entry_maps_drop_keep_and_usage_in_process(self):
        argv = ["head-abandoned", str(self.results), "task-a.txt", str(self.payload)]
        self.assertEqual(_main(argv), 0)
        self.payload.write_text("task: x\n")
        self.assertEqual(_main(argv), 1)
        for bad in (argv + ["extra"], argv[:3]):
            err = StringIO()
            with redirect_stderr(err):
                self.assertEqual(_main(bad), 2, bad)
            self.assertIn("head-abandoned <results_dir> <filename> <payload>", err.getvalue())


class ShippedQueueDropsAbandonedHead(unittest.TestCase):
    def _run(self, script: Path, wait_fn: str, *, writer: "Path | None" = None,
             before=None) -> tuple[str, Path]:
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
        names = ["has_result", "task_payload", "queue_head", "process_announced_queue"]
        if writer is not None:
            names.append("mark_worker_stage")
        fns = "\n".join(_function_text(n, text) for n in names)
        stage_impl = (f'source {shlex.quote(str(WORKER_STAGE))}\n' if writer is not None else
                      f'mark_worker_stage() {{ echo "$1 $2" >> {shlex.quote(str(root / "stages.log"))}; }}\n')
        stubs = (
            f'{wait_fn}() {{ return 0; }}\n'
            + stage_impl +
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
        if writer is not None:
            env.update(SUTANDO_INSTANCE_ID=RECIPIENT, WORKER_INSTANCE=RECIPIENT,
                       SUTANDO_POOL_DELIVERY_SCRIPT=str(writer))
        if before is not None:
            before(root)
        # A prior delivery attempt left its in-flight marker before the head was archived.
        subprocess.run([sys.executable, str(DISPATCH), "inflight-mark", env["INFLIGHT_DIR"],
                        "task-gone.txt", "inc-1"], check=True, timeout=30)
        r = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stderr = r.stderr
        self.root = root
        return (submitted.read_text() if submitted.exists() else ""), queue

    def test_both_notifiers_drop_an_archived_head_untyped_and_proceed(self):
        for runtime, (script, wait_fn) in NOTIFIERS.items():
            with self.subTest(runtime=runtime):
                submitted, queue = self._run(script, wait_fn)
                self.assertEqual(submitted, "task-next.txt\n",
                                 f"only the live task may be typed; stderr:\n{self.stderr}")
                self.assertEqual(sorted(p.name for p in queue.iterdir()), [])
                self.assertIn("dropped task-gone.txt from the queue", self.stderr)

    def test_claude_clears_a_dropped_heads_inflight_marker(self):
        script, wait_fn = NOTIFIERS["claude"]
        self._run(script, wait_fn)
        live = subprocess.run([sys.executable, str(DISPATCH), "inflight-live",
                               str(self.root / "inflight"), "task-gone.txt", "inc-1"],
                              timeout=30).returncode
        self.assertEqual(live, 1, "the dropped head's in-flight marker must be cleared")

    @staticmethod
    def _pool(root: Path, *args: str) -> str:
        return subprocess.run([sys.executable, str(POOL_DELIVERY), "--workspace", str(root),
                               "--recipient", RECIPIENT, *args],
                              check=True, capture_output=True, text=True, timeout=30).stdout.strip()

    def _held(self, root: Path) -> None:
        # The worker took the head (pending record + sentinel) before it was archived.
        self._pool(root, "mark-done", "--task-id", "task-gone", "--stage", "pending")
        sentinel = root / "deliveries" / RECIPIENT / "task-gone.txt"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("")
        self._cache_pointer(root)

    @staticmethod
    def _cache_pointer(root: Path) -> bytes:
        # The resolver-cached payload pointer the announcement left: a path whose target is now gone.
        pointer = root / "payload" / "task-gone.txt"
        data = str(root / "tasks" / "task-gone.txt").encode()
        pointer.write_bytes(data)
        return data

    def test_codex_drop_withdraws_the_hold_through_the_production_writer(self):
        script, wait_fn = NOTIFIERS["codex"]
        submitted, queue = self._run(script, wait_fn, writer=POOL_DELIVERY, before=self._held)
        root = self.root
        self.assertEqual(submitted, "task-next.txt\n", self.stderr)
        self.assertEqual(sorted(p.name for p in queue.iterdir()), [])
        self.assertEqual(self._pool(root, "residue", "--task-id", "task-gone"), "clean",
                         "an unpublished task must not read as finished")
        workers = root / "state" / "workers" / RECIPIENT
        self.assertFalse((workers / "done" / "task-gone.flag").exists(),
                         "no completion flag without a published result")
        self.assertEqual(list(workers.rglob("task-gone.*")), [], "no pending or done record survives")
        self.assertEqual([p.name for p in (root / "deliveries" / RECIPIENT).glob("task-gone*")], [],
                         "the sentinel is retired")
        self.assertFalse((root / "payload" / "task-gone.txt").exists(),
                         "a dropped head's cached payload pointer is removed with it")

    def test_codex_keeps_the_head_when_the_abandon_write_fails(self):
        script, wait_fn = NOTIFIERS["codex"]
        seeded = {}
        submitted, queue = self._run(script, wait_fn, writer=Path("/nonexistent/pool_delivery.py"),
                                     before=lambda root: seeded.setdefault("ptr", self._cache_pointer(root)))
        self.assertEqual(submitted, "", "nothing behind the head is typed while it is held")
        self.assertEqual(sorted(p.name for p in queue.iterdir()), ["task-gone.txt", "task-next.txt"])
        self.assertIn("keeping task-gone.txt at the queue head", self.stderr)
        self.assertNotIn("dropped task-gone.txt", self.stderr)
        pointer = self.root / "payload" / "task-gone.txt"
        self.assertTrue(pointer.exists(), "the held head keeps its cached payload pointer")
        self.assertEqual(pointer.read_bytes(), seeded["ptr"])

    def test_both_notifiers_delegate_the_decision_to_task_dispatch(self):
        for runtime, (script, _) in NOTIFIERS.items():
            with self.subTest(runtime=runtime):
                fn = _function_text("process_announced_queue", script.read_text())
                self.assertEqual(fn.count('"$DISPATCH_PY" head-abandoned'), 1)
                self.assertNotIn("[ ! -e", fn, "the abandoned-head rule must not be re-derived in bash")


if __name__ == "__main__":
    unittest.main(verbosity=2)
