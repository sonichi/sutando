#!/usr/bin/env python3
"""A notifier types what a resolver-backed watcher announces.

A watcher run with `SUTANDO_INBOX_RESOLVER` announces the payload's ABSOLUTE
path (the body a delivery sentinel stands for). Both external notifiers used to
refuse any announcement containing a slash at enqueue, and then to refuse every
task on a worker's own inbox as "worker-held", so the external hosting mode
could never serve a delivery inbox. The reading of an announcement now has one
owner, `task_dispatch.announced_entry`, and both notifiers' shipped
`enqueue_announced_task` (extracted from the scripts, not copied) file the task
under the payload's basename, record the payload for the prompt, consult the
worker-held check only when they serve the core's own inbox, and log a reader
failure instead of dropping the task silently.

Run: python3 tests/task-notifier-resolved-announcement.test.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DISPATCH = REPO / "src" / "delivery" / "task_dispatch.py"
NOTIFIERS = {
    "claude": REPO / "src" / "agent" / "claude" / "cli" / "task-notifier.sh",
    "codex": REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh",
}

spec = importlib.util.spec_from_file_location("task_dispatch", DISPATCH)
td = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(td)


def _function_text(name: str, text: str, script: Path) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
    assert m, f"{name} not found in {script}"
    return m.group(0)


class _Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve()
        (self.ws / "tasks").mkdir()
        self.payload = self.ws / "tasks" / "task-abc.txt"
        self.payload.write_text("id: task-abc\npriority: urgent\ntask: x\n")
        self.inbox = self.ws / "deliveries" / "w1"
        self.inbox.mkdir(parents=True)


class AnnouncedEntry(_Workspace):
    def test_a_bare_name_is_a_file_in_the_inbox(self):
        self.assertEqual(td.announced_entry(self.inbox, "task-abc.txt"),
                         ("task-abc.txt", self.inbox / "task-abc.txt"))

    def test_an_absolute_payload_is_accepted_only_from_a_resolver_backed_watcher(self):
        self.assertEqual(td.announced_entry(self.inbox, str(self.payload), payload_dir=self.ws / "tasks"),
                         ("task-abc.txt", self.payload))
        self.assertIsNone(td.announced_entry(self.inbox, str(self.payload)))

    def test_only_the_owning_workspaces_tasks_dir_is_accepted(self):
        elsewhere = self.ws / "elsewhere" / "tasks"
        elsewhere.mkdir(parents=True)
        (elsewhere / "task-abc.txt").write_text("id: task-abc\n")
        (self.ws / "deliveries" / "w1" / "task-abc.txt").write_text("")
        for bad in (str(elsewhere / "task-abc.txt"),            # another workspace's tasks/
                    str(self.ws / "deliveries" / "w1" / "task-abc.txt"),  # the sentinel itself
                    "/etc/hosts"):
            self.assertIsNone(td.announced_entry(self.inbox, bad, payload_dir=self.ws / "tasks"), bad)

    def test_traversal_relative_and_missing_payloads_are_refused(self):
        for bad in ("", "../task-abc.txt", "tasks/task-abc.txt", str(self.ws / "tasks" / "task-none.txt"),
                    str(self.ws / "tasks"), f"{self.ws}/tasks/../tasks/task-abc.txt"):
            self.assertIsNone(td.announced_entry(self.inbox, bad, payload_dir=self.ws / "tasks"), bad)

    def test_the_cli_prints_key_tab_payload_and_refuses_with_1(self):
        r = subprocess.run([sys.executable, str(DISPATCH), "announced-entry", str(self.inbox),
                            str(self.payload), "--resolved", str(self.ws / "tasks")], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, f"task-abc.txt\t{self.payload}\n"))
        r = subprocess.run([sys.executable, str(DISPATCH), "announced-entry", str(self.inbox),
                            str(self.payload), "--resolved"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([sys.executable, str(DISPATCH), "announced-entry", str(self.inbox),
                            str(self.payload)], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        r = subprocess.run([sys.executable, str(DISPATCH), "announced-entry", str(self.inbox),
                            str(self.payload), "--bogus"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


class ShippedEnqueue(_Workspace):
    """Runs each notifier's own enqueue_announced_task with its collaborators stubbed."""

    def setUp(self):
        super().setUp()
        (self.inbox / "task-abc.txt").write_text("")  # the sentinel: this worker holds it
        self.queue = self.ws / "queue"
        self.payloads = self.ws / "payload"
        self.queue.mkdir()
        self.payloads.mkdir()

    def _enqueue(self, runtime: str, announced: str, *, inbox: Path, resolver: str,
                 kind: str = "", dispatch: Path = DISPATCH) -> None:
        script = NOTIFIERS[runtime]
        fn = _function_text("enqueue_announced_task", script.read_text(), script)
        # The real worker-held check against this workspace; result/claim/log stubbed.
        harness = "\n".join([
            "set -u",
            'has_result() { return 1; }',
            'filename_is_claimed() { return 1; }',
            'log_notifier() { printf "%s\\n" "$1" >> "$LOG"; }',
            'filename_is_worker_held() { local rc=0; "$NOTIFIER_PY" "$DISPATCH_PY" worker-holds "$DELIVERIES_DIR" "$1" || rc=$?; [ "$rc" -ne 1 ]; }',
            fn,
            f'enqueue_announced_task "{announced}"',
        ])
        env = {**os.environ, "TASKS_DIR": str(inbox), "WORKSPACE_DIR": str(self.ws),
               "DELIVERIES_DIR": str(self.ws / "deliveries"),
               "queue_dir": str(self.queue), "PAYLOAD_DIR": str(self.payloads), "LOG": str(self.ws / "log"),
               "NOTIFIER_PY": sys.executable, "DISPATCH_PY": str(dispatch)}
        for k in ("SUTANDO_INBOX_RESOLVER", "SUTANDO_INBOX_KIND"):
            env.pop(k, None)
        if resolver:
            env["SUTANDO_INBOX_RESOLVER"] = resolver
        if kind:
            env["SUTANDO_INBOX_KIND"] = kind
        r = subprocess.run(["bash", "-c", harness], env=env, timeout=30, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _queued(self) -> list[str]:
        return sorted(p.name for p in self.queue.iterdir())

    def _log(self) -> str:
        p = self.ws / "log"
        return p.read_text() if p.exists() else ""

    def _reset(self):
        for d in (self.queue, self.payloads):
            for p in d.iterdir():
                p.unlink()
        (self.ws / "log").unlink(missing_ok=True)

    def test_a_resolved_absolute_announcement_is_queued_under_its_basename_with_its_payload(self):
        for runtime in NOTIFIERS:
            with self.subTest(runtime=runtime):
                self._enqueue(runtime, str(self.payload), inbox=self.inbox, resolver="/bin/true", kind="deliveries")
                self.assertEqual(self._queued(), ["task-abc.txt"])
                self.assertEqual((self.queue / "task-abc.txt").read_text(), "urgent")
                self.assertEqual((self.payloads / "task-abc.txt").read_text(), str(self.payload))
                self.assertEqual(self._log(), "")
                self._reset()

    def test_the_same_announcement_from_a_watcher_without_a_resolver_types_nothing_quietly(self):
        for runtime in NOTIFIERS:
            with self.subTest(runtime=runtime):
                self._enqueue(runtime, str(self.payload), inbox=self.inbox, resolver="", kind="deliveries")
                self.assertEqual(self._queued(), [])
                self.assertEqual(self._log(), "")

    def test_on_the_cores_inbox_a_worker_held_task_is_still_refused(self):
        core = self.ws / "tasks"
        for runtime in NOTIFIERS:
            with self.subTest(runtime=runtime):
                self._enqueue(runtime, "task-abc.txt", inbox=core, resolver="")
                self.assertEqual(self._queued(), [])
                self.assertIn("worker-held", self._log())
                self._reset()

    def test_the_hold_is_skipped_by_inbox_kind_not_by_the_resolver_being_set(self):
        core = self.ws / "tasks"
        for runtime in NOTIFIERS:
            with self.subTest(runtime=runtime):
                # A core that happens to run a resolver still honours worker holds.
                self._enqueue(runtime, "task-abc.txt", inbox=core, resolver="/bin/true")
                self.assertEqual(self._queued(), [])
                self.assertIn("worker-held", self._log())
                self._reset()

    def test_a_broken_reader_is_logged_not_silently_dropped(self):
        for runtime in NOTIFIERS:
            with self.subTest(runtime=runtime):
                self._enqueue(runtime, "task-abc.txt", inbox=self.ws / "tasks", resolver="",
                              dispatch=self.ws / "no-such-dispatch.py")
                self.assertEqual(self._queued(), [])
                self.assertIn("announcement not read: task-abc.txt", self._log())
                self._reset()

    def test_traversal_is_still_refused_before_anything_is_written(self):
        for runtime in NOTIFIERS:
            with self.subTest(runtime=runtime):
                self._enqueue(runtime, "../task-abc.txt", inbox=self.inbox, resolver="/bin/true", kind="deliveries")
                self.assertEqual(self._queued(), [])
                self.assertEqual(sorted(p.name for p in self.payloads.iterdir()), [])
                self.assertEqual(self._log(), "")


class PromptNamesThePayload(unittest.TestCase):
    def test_both_notifiers_read_the_recorded_payload_and_fall_back_to_the_inbox_entry(self):
        for runtime, script in NOTIFIERS.items():
            with self.subTest(runtime=runtime):
                text = script.read_text()
                fn = _function_text("task_payload", text, script)
                with tempfile.TemporaryDirectory() as td_:
                    Path(td_, "task-abc.txt").write_text("/elsewhere/tasks/task-abc.txt")
                    harness = fn + '\ntask_payload task-abc.txt; echo; task_payload task-none.txt; echo'
                    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                                       env={**os.environ, "PAYLOAD_DIR": td_, "TASKS_DIR": "/inbox"})
                self.assertEqual(r.stdout, "/elsewhere/tasks/task-abc.txt\n/inbox/task-none.txt\n")
                self.assertIn('$(task_payload "$', text)

    def test_the_payload_record_goes_with_its_queue_marker(self):
        for runtime, script in NOTIFIERS.items():
            with self.subTest(runtime=runtime):
                text = script.read_text()
                self.assertEqual(text.count('rm -f "$queue_dir/$filename" "$PAYLOAD_DIR/$filename"'), 2)
                self.assertNotIn('rm -f "$queue_dir/$filename"\n', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
