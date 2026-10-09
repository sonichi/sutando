#!/usr/bin/env python3
"""Hermetic acceptance regressions for the reliability experiment. No live tasks.
Run: python3 tests/cron-outstanding-payload.test.py
"""
import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
os.environ["SUTANDO_TELEMETRY"] = "0"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cron = load("cron_runner_acceptance", "src/cron-runner.py")

def fire(root, queue):
    cron.WORKSPACE = Path(root)
    cron.TASKS_DIR = Path(root) / "tasks"
    queue.put(str(cron.emit_task("audit", {"prompt": "must survive"})))


class CronAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cron.WORKSPACE = self.root
        cron.TASKS_DIR = self.root / "tasks"

    def test_all_worker_ownership_stages_keep_payload_and_identity(self):
        for suffix in (".txt", ".accepted", ".claimed"):
            with self.subTest(suffix=suffix):
                first = cron.emit_task("audit", {"prompt": "original"})
                sentinel = self.root / "deliveries" / "worker" / (first.stem + suffix)
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.write_text("assigned")
                original = first.read_bytes()
                second = cron.emit_task("audit", {"prompt": "next interval"})
                self.assertEqual(first, second)
                self.assertEqual(first.read_bytes(), original)
                self.assertTrue(sentinel.is_file())
                sentinel.unlink()
                first.unlink()

    def test_handler_claim_keeps_payload(self):
        first = cron.emit_task("audit", {"prompt": "original"})
        claim = self.root / "state/task-event-handler-claims" / first.name
        claim.parent.mkdir(parents=True)
        claim.write_text("owned")
        cron.emit_task("audit", {"prompt": "replacement"})
        self.assertIn("task: original", first.read_text())

    def test_concurrent_processes_publish_one_complete_task(self):
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        workers = [context.Process(target=fire, args=(str(self.root), queue)) for _ in range(12)]
        for worker in workers:
            worker.start()
        paths = [queue.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(timeout=15)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(len(list(cron.TASKS_DIR.glob("task-*.txt"))), 1)
        self.assertIn("task: must survive\n", Path(paths[0]).read_text())

    def test_existing_legacy_backlog_is_never_deleted(self):
        cron.TASKS_DIR.mkdir()
        old = [cron.TASKS_DIR / f"task-cron-audit-{i}.txt" for i in (1, 2)]
        for path in old:
            path.write_text("preserve")
        self.assertEqual(cron.emit_task("audit", {}), old[0])
        self.assertTrue(all(path.exists() for path in old))

    def test_atomic_publish_failure_leaves_no_partial_task(self):
        with patch.object(cron.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                cron.emit_task("audit", {"prompt": "partial must not be seen"})
        self.assertFalse(list(cron.TASKS_DIR.glob("task-*.txt")))
        self.assertFalse(list(cron.TASKS_DIR.glob(".task-*.txt.*")))

    def test_newest_configuration_applies_after_prior_task_retires(self):
        first = cron.emit_task("audit", {"prompt": "original"})
        first.unlink()
        latest = cron.emit_task("audit", {"prompt": "updated config"})
        self.assertIn("task: updated config", latest.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
