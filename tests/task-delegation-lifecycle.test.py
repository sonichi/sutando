#!/usr/bin/env python3
"""Core lifecycle evaluation: sequential traces and scheduled replacement boundaries.

Run: python3 tests/task-delegation-lifecycle.test.py
These checks do not establish process concurrency, crash durability, or live delivery.
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import result_publish  # noqa: E402
import task_delegation as delegation  # noqa: E402

IDS = ("task-lifecycle-a", "task-lifecycle-b")
CONTEXTS = tuple(itertools.product(range(2), IDS))
TTLS = {"renew-short": 3, "renew-long": 60}


@dataclass
class Expected:
    task_mtime: Optional[float] = 9900
    lease: Optional[tuple] = None
    result: Optional[str] = None

    def delegated(self, now):
        if self.task_mtime is None or self.lease is None:
            return False
        started, ttl = self.lease
        return self.task_mtime <= started <= now < started + ttl


class DelegationLifecycle(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory(prefix="sutando-lifecycle-") as root:
            self.workspaces = tuple(Path(root) / f"workspace-{n}" for n in range(2))
            self.expected = {key: Expected() for key in CONTEXTS}
            self.now, self.trace = 10000, []
            for key in CONTEXTS:
                self.write_task(key, 9900)
            for key in CONTEXTS:
                ws, tid = self.context(key)
                delegation.mark(ws, tid, 600, now=self.now)
                self.expected[key].lease = (self.now, 600)
                self.trace.append(f"seed {key}")
                self.assert_state()
            yield

    def context(self, key):
        return self.workspaces[key[0]], key[1]

    def task_path(self, key):
        ws, tid = self.context(key)
        return ws / "tasks" / f"{tid}.txt"

    def marker_path(self, key):
        ws, tid = self.context(key)
        return ws / "state" / "task-delegations" / f"{tid}.json"

    def write_task(self, key, when):
        path = self.task_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"id: {key[1]}\ntask: turn {when}\n", encoding="utf-8")
        os.utime(path, (when, when))
        self.expected[key].task_mtime = when

    def assert_state(self, pending=None):
        for key, expected in self.expected.items():
            ws, tid = self.context(key)
            probes = {self.now}
            if expected.lease:
                started, ttl = expected.lease
                probes.update((started + ttl - 1, started + ttl))
            for now in sorted(probes):
                with self.subTest(context=key, now=now, trace=" -> ".join(self.trace)):
                    live = expected.delegated(now)
                    self.assertEqual(delegation.is_delegated(ws, tid, now=now), live)
                    self.assertEqual(delegation.holder(ws, tid, now=now),
                                     "background-agent" if live else None)
                    self.assertIsNone(delegation.holder(ws, tid, now=now, include_background=False))
            with self.subTest(context=key, trace=" -> ".join(self.trace)):
                self.assertEqual(self.marker_path(key).exists(), expected.lease is not None)
                result = ws / "results" / f"{tid}.txt"
                self.assertEqual(result.read_text(encoding="utf-8") if result.exists() else None,
                                 expected.result)
        for n, ws in enumerate(self.workspaces):
            with self.subTest(workspace=n, trace=" -> ".join(self.trace)):
                markers = ws / "state" / "task-delegations"
                self.assertEqual({p.name for p in markers.iterdir()} if markers.exists() else set(),
                                 {f"{tid}.json" for (w, tid), e in self.expected.items()
                                  if w == n and e.lease is not None}
                                 | ({pending.name} if pending is not None and pending.parent == markers else set()))
                self.assertEqual({p.name for p in (ws / "results").glob("*")},
                                 {f"{tid}.txt" for (w, tid), e in self.expected.items()
                                  if w == n and e.result is not None})

    def step(self, command, key):
        self.now += 1
        self.trace.append(f"{command} {key} @{self.now}")
        ws, tid = self.context(key)
        expected = self.expected[key]
        if command in TTLS:
            ttl = TTLS[command]
            self.assertEqual(delegation.mark(ws, tid, ttl, now=self.now), self.marker_path(key))
            expected.lease = (self.now, ttl)
        elif command in ("rewrite", "recreate"):
            self.write_task(key, self.now)
        elif command == "delete":
            self.task_path(key).unlink()
            expected.task_mtime = None
        elif command == "clear":
            self.assertEqual(delegation.clear(ws, tid), expected.lease is not None)
            expected.lease = None
        elif command == "publish":
            body = f"complete {tid} at {self.now}\n"
            target = ws / "results" / f"{tid}.txt"
            self.assertEqual(result_publish.publish_text(target, body), target)
            expected.result, expected.lease = body, None
        else:
            self.fail(f"unknown command: {command}")
        self.assert_state()

    def test_bounded_permutations_match_model_after_every_transition(self):
        count = 0
        commands = ("renew-short", "renew-long", "rewrite", "clear", "publish")
        for key, order in itertools.product(CONTEXTS, itertools.permutations(commands)):
            with self.subTest(target=key, order=order), self.fixture():
                for command in order:
                    self.step(command, key)
                count += 1
        self.assertEqual(count, 480)
        print(f"lifecycle permutations: {count} traces", flush=True)

    def test_delete_recreate_remark_and_worker_outcomes_across_lifecycle(self):
        for key in CONTEXTS:
            with self.subTest(target=key), self.fixture():
                for command in ("delete", "recreate", "renew-short", "clear",
                                "renew-long", "publish", "renew-long"):
                    self.step(command, key)
                    ws, tid = self.context(key)
                    background = "background-agent" if self.expected[key].delegated(self.now) else None
                    for outcome in (None, "worker-a", PermissionError("worker store unreadable")):
                        for include in (True, False):
                            calls = []

                            def lookup(workspace, task_id):
                                calls.append((workspace, task_id))
                                if isinstance(outcome, OSError):
                                    raise outcome
                                return outcome

                            with self.subTest(command=command, worker=outcome, include=include):
                                if isinstance(outcome, OSError):
                                    with self.assertRaisesRegex(PermissionError, "worker store unreadable"):
                                        delegation.holder(ws, tid, worker_holder=lookup,
                                                          include_background=include, now=self.now)
                                else:
                                    held = delegation.holder(ws, tid, worker_holder=lookup,
                                                             include_background=include, now=self.now)
                                    self.assertEqual(held, outcome or (background if include else None))
                                self.assertEqual(calls, [(ws, tid)])
                            self.assert_state()
        print("lifecycle delete/worker schedules: 4 traces", flush=True)

    def test_failed_existing_lease_replacement_preserves_bytes_and_expiry(self):
        for key, establish in itertools.product(CONTEXTS, TTLS):
            with self.subTest(target=key, established=establish), self.fixture():
                self.step(establish, key)
                old_bytes = self.marker_path(key).read_bytes()
                old_start, old_ttl = self.expected[key].lease
                renewal = "renew-long" if establish == "renew-short" else "renew-short"
                with mock.patch.object(delegation.os, "replace", side_effect=OSError("renewal refused")):
                    with self.assertRaisesRegex(OSError, "renewal refused"):
                        self.step(renewal, key)
                self.assertEqual(self.marker_path(key).read_bytes(), old_bytes)
                self.assert_state()
                for now in (old_start + old_ttl - 1, old_start + old_ttl):
                    self.now = now
                    self.trace.append(f"probe old expiry @{now}")
                    self.assert_state()
        print("lifecycle failed renewals: 8 traces", flush=True)

    def test_failed_publication_preserves_existing_result_and_live_lease(self):
        for key in CONTEXTS:
            with self.subTest(target=key), self.fixture():
                self.step("publish", key)
                self.step("renew-long", key)
                old_bytes = self.marker_path(key).read_bytes()
                with mock.patch.object(result_publish.os, "replace", side_effect=OSError("publish refused")):
                    with self.assertRaisesRegex(OSError, "publish refused"):
                        self.step("publish", key)
                self.assertEqual(self.marker_path(key).read_bytes(), old_bytes)
                self.assert_state()
        print("lifecycle failed publications: 4 traces", flush=True)

    def test_rewrite_before_real_renewal_replace_makes_committed_lease_stale(self):
        for key in CONTEXTS:
            with self.subTest(target=key), self.fixture():
                self.step("renew-short", key)
                old_bytes = self.marker_path(key).read_bytes()
                real_replace, calls = os.replace, []

                def before_replace(source, target):
                    calls.append(Path(target))
                    self.assertEqual(Path(target), self.marker_path(key))
                    self.assertEqual(Path(target).read_bytes(), old_bytes)
                    self.assert_state(pending=Path(source))
                    self.trace.append(f"rewrite before replace {key} @{self.now + 1}")
                    self.write_task(key, self.now + 1)
                    self.assert_state(pending=Path(source))
                    real_replace(source, target)

                with mock.patch.object(delegation.os, "replace", side_effect=before_replace):
                    self.step("renew-long", key)
                self.assertEqual(calls, [self.marker_path(key)])
                self.assertNotEqual(self.marker_path(key).read_bytes(), old_bytes)
                self.assertFalse(self.expected[key].delegated(self.now))
                self.assert_state()
                self.step("renew-long", key)
        print("lifecycle replacement-boundary schedules: 4 traces", flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
