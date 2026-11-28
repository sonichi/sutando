#!/usr/bin/env python3
"""Bounded local lifecycle evaluation of wake-aware inbox completion.

Commands write tasks, stage/publish answers, rename claims, and move fixture
archives; every command checks both disposable workspaces against an independent
model of commanded ids, presence, filenames, wake flags, timestamps, and results.
The model never reads task bytes, JSON state, or production lookup output.

This covers settled local files and supported archive layouts, not concurrent
writers, the production archiver's collision policy, routing, or live delivery.
Run: python3 tests/task-wake-inbox-lifecycle.test.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from _helpers.hermetic_env import hermetic_env
from delivery.task_dispatch import has_ready_result, unanswered_in_inbox
from local_task_protocol import write_task_file
from result_publish import publish_staged, publish_text, stage_text

BASE_TIME = 1_700_000_000
ARCHIVES = {
    "flat": "archive/{task_id}.txt",
    "month": "archive/2023-11/{task_id}.txt",
    "flat-epoch": "archive/{task_id}-1700000100.txt",
    "month-epoch": "archive/2023-11/{task_id}-1700000200.txt",
    "retention": "archive-2023-11-15/{task_id}.txt",
    "retention-epoch": "archive-2023-11-15/{task_id}-1700000300.txt",
}


@dataclass
class Task:
    task_id: str
    filename: str
    wake: bool
    timestamp: int
    present: bool = True


@dataclass
class Result:
    task_id: str
    filename: str
    timestamp: int
    ready: bool
    published: bool = True


@dataclass
class Inbox:
    root: Path
    tasks: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)


class Trace:
    def __init__(self, case, root):
        self.case = case
        self.inboxes = {name: Inbox(root / name) for name in ("alpha", "beta")}
        self.commands = 0
        self.readiness_checks = 0
        for inbox in self.inboxes.values():
            (inbox.root / "tasks").mkdir(parents=True)
            (inbox.root / "results").mkdir()

    @staticmethod
    def _stamp(path, timestamp):
        os.utime(path, (BASE_TIME + timestamp, BASE_TIME + timestamp))

    @staticmethod
    def _ready(inbox, task_id, task=None):
        since = task.timestamp if task and task.present and task.wake else None
        return any(result.task_id == task_id and result.published and result.ready
                   and (since is None or result.timestamp >= since)
                   for result in inbox.results.values())

    def _check(self, command):
        self.commands += 1
        for home, inbox in self.inboxes.items():
            by_id = {}
            for task in inbox.tasks.values():
                by_id.setdefault(task.task_id, [])
                if task.present:
                    by_id[task.task_id].append(task)
            expected = set()
            for task_id, present in by_id.items():
                # Deduplication witnesses only duplicate one turn, never competing incarnations.
                self.case.assertLessEqual(len({(t.wake, t.timestamp) for t in present}), 1)
                if present and not self._ready(inbox, task_id, present[0]):
                    expected.add(task_id)
                probes = present or [None]
                for task in probes:
                    filename = task.filename if task else f"{task_id}.txt"
                    actual = has_ready_result(inbox.root / "results", f"{task_id}.txt",
                                              task_file=inbox.root / "tasks" / filename)
                    self.case.assertEqual(actual, self._ready(inbox, task_id, task),
                                          (command, home, task_id, filename))
                    self.readiness_checks += 1
            actual = unanswered_in_inbox(inbox.root / "tasks", inbox.root / "results")
            self.case.assertEqual({task_id for task_id, _ in actual}, expected,
                                  (command, home))
            self.case.assertEqual(len(actual), len(expected), (command, home, "deduplication"))
            for task_id, path in actual:
                self.case.assertIn(path.name, {task.filename for task in by_id[task_id]})

    def write_task(self, task_id, timestamp, *, wake=False, home="alpha"):
        inbox = self.inboxes[home]
        headers = [("source", "test")]
        if wake:
            headers.append(("wake_episode", f"episode-{timestamp}"))
        path = write_task_file(inbox.root / "tasks", task_id, headers, "fixture request")
        self._stamp(path, timestamp)
        inbox.tasks[path.name] = Task(task_id, path.name, wake, timestamp)
        self._check(("write-task", home, task_id, timestamp, wake))

    def stage(self, task_id, timestamp, *, body="answer\n", home="alpha"):
        inbox = self.inboxes[home]
        path = stage_text(inbox.root / "results" / f"{task_id}.txt", body)
        self._stamp(path, timestamp)
        inbox.results[path.name] = Result(task_id, path.name, timestamp, bool(body.strip()), False)
        self._check(("stage", home, task_id))

    def publish_stage(self, task_id, *, home="alpha"):
        inbox = self.inboxes[home]
        staged = [r for r in inbox.results.values() if r.task_id == task_id and not r.published]
        self.case.assertEqual(len(staged), 1)
        result = staged[0]
        target = inbox.root / "results" / f"{task_id}.txt"
        publish_staged(inbox.root / "results" / result.filename, target)
        self._stamp(target, result.timestamp)
        del inbox.results[result.filename]
        inbox.results[target.name] = replace(result, filename=target.name, published=True)
        self._check(("publish-staged", home, task_id))

    def publish(self, task_id, timestamp, *, body="answer\n", home="alpha"):
        inbox = self.inboxes[home]
        path = inbox.root / "results" / f"{task_id}.txt"
        publish_text(path, body)
        self._stamp(path, timestamp)
        inbox.results[path.name] = Result(task_id, path.name, timestamp, bool(body.strip()))
        self._check(("publish-text", home, task_id, timestamp))

    def archive_result(self, task_id, layout, *, home="alpha"):
        inbox = self.inboxes[home]
        source = f"{task_id}.txt"
        dest = ARCHIVES[layout].format(task_id=task_id)
        self.case.assertNotIn(dest, inbox.results)
        path = inbox.root / "results" / dest
        path.parent.mkdir(parents=True, exist_ok=True)
        (inbox.root / "results" / source).rename(path)
        result = inbox.results.pop(source)
        self._stamp(path, result.timestamp)
        inbox.results[dest] = replace(result, filename=dest)
        self._check(("archive-result", home, task_id, layout))

    def claim(self, task_id, *, home="alpha"):
        inbox = self.inboxes[home]
        source = f"{task_id}.txt"
        dest = f"{task_id}.claimed-core-a.txt"
        task = inbox.tasks.pop(source)
        path = inbox.root / "tasks" / dest
        (inbox.root / "tasks" / source).rename(path)
        self._stamp(path, task.timestamp)
        inbox.tasks[dest] = replace(task, filename=dest)
        self._check(("claim-rename", home, task_id))

    def archive_task(self, filename, slot, *, home="alpha"):
        inbox = self.inboxes[home]
        task = inbox.tasks[filename]
        path = inbox.root / "tasks" / "archive" / slot / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        (inbox.root / "tasks" / filename).rename(path)
        self._stamp(path, task.timestamp)
        inbox.tasks[filename] = replace(task, present=False)
        self._check(("archive-task", home, filename, slot))


class WakeInboxLifecycleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = mock.patch.dict(os.environ, hermetic_env(home=self.root / "home"), clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.trace = Trace(self, self.root)

    def test_original_answer_and_repeated_wakes_follow_one_id(self):
        trace, task_id = self.trace, "task-lifecycle"
        trace.write_task(task_id, 100)
        trace.stage(task_id, 90)
        trace.publish_stage(task_id)
        trace.archive_result(task_id, "month")
        trace.archive_task(f"{task_id}.txt", "original")
        trace.write_task(task_id, 200, wake=True)
        trace.claim(task_id)
        trace.stage(task_id, 200)
        trace.publish_stage(task_id)
        trace.archive_result(task_id, "month-epoch")
        trace.archive_task(f"{task_id}.claimed-core-a.txt", "first-wake")
        trace.write_task(task_id, 300, wake=True)
        trace.publish(task_id, 301, body=" \t\n")
        trace.stage(task_id, 301)
        trace.publish_stage(task_id)
        trace.archive_result(task_id, "retention")
        trace.archive_task(f"{task_id}.txt", "second-wake")
        trace.write_task(task_id, 400, wake=True)
        self.assertEqual(trace.commands, 18)

    def test_older_equal_and_newer_answers_in_every_supported_layout(self):
        trace = self.trace
        for layout in ("live", *ARCHIVES):
            for relation, offset in (("older", -1), ("equal", 0), ("newer", 1)):
                with self.subTest(layout=layout, relation=relation):
                    task_id = f"task-matrix-{layout}-{relation}"
                    trace.write_task(task_id, 500, wake=True)
                    trace.publish(task_id, 500 + offset)
                    if layout != "live":
                        trace.archive_result(task_id, layout)
                    trace.publish(task_id, 502, body="" if offset == 0 else " \t\n")
        self.assertEqual(trace.commands, 81)

    def test_nonnumeric_near_prefix_ids_and_same_turn_claim_deduplication(self):
        trace, task_id = self.trace, "task-prefix"
        trace.write_task(task_id, 600, wake=True)
        for suffix, layout in (("-1-other", "flat"), ("-1700000000x", "month"),
                               ("-notes", "retention"), ("2", "flat")):
            near_id = f"{task_id}{suffix}"
            trace.write_task(near_id, 600, wake=True)
            trace.publish(near_id, 601)
            trace.archive_result(near_id, layout)
        trace.claim(task_id)
        trace.write_task(task_id, 600, wake=True)
        trace.publish(task_id, 600)
        trace.archive_result(task_id, "flat-epoch")
        trace.archive_task(f"{task_id}.txt", "duplicate-bare")
        trace.archive_task(f"{task_id}.claimed-core-a.txt", "duplicate-claim")
        trace.write_task(task_id, 700, wake=True)
        self.assertEqual(trace.commands, 20)

    def test_two_workspaces_keep_identical_ids_and_wakes_independent(self):
        trace, task_id = self.trace, "task-isolation"
        trace.write_task(task_id, 800, wake=True)
        trace.write_task(task_id, 800, wake=True, home="beta")
        trace.stage(task_id, 800)
        trace.publish_stage(task_id)
        trace.archive_result(task_id, "retention-epoch")
        trace.claim(task_id, home="beta")
        trace.publish(task_id, 799, home="beta")
        trace.archive_result(task_id, "month", home="beta")
        trace.publish(task_id, 801, home="beta")
        trace.archive_task(f"{task_id}.txt", "answered")
        trace.write_task(task_id, 900, wake=True)
        trace.publish(task_id, 901, body="", home="beta")
        self.assertEqual(trace.commands, 12)


if __name__ == "__main__":
    unittest.main()
