#!/usr/bin/env python3
"""Contract and adapter-wiring tests for shared task/result archival."""
from __future__ import annotations

import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from task_archive import archive_file  # noqa: E402

# telegram-bridge resolves access control at import time. Isolate that lookup
# from the operator's real config before exec_module. The static adapter test
# below names all three bridges, so the hermetic contract requires every
# canonical channel path to be seeded.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(
    prefix="task-archive-policy-config-"
)
_TMP_CONFIG = os.environ["CLAUDE_CONFIG_DIR"]
atexit.register(lambda: shutil.rmtree(_TMP_CONFIG, ignore_errors=True))
os.environ["HOME"] = _TMP_CONFIG
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-not-real"
_ACCESS_DIR = (
    Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
)
_ACCESS_DIR.mkdir(parents=True, exist_ok=True)
(_ACCESS_DIR / "access.json").write_text('{"allowFrom": []}\n')
_DISCORD_ACCESS_DIR = (
    Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
)
_DISCORD_ACCESS_DIR.mkdir(parents=True, exist_ok=True)
(_DISCORD_ACCESS_DIR / "access.json").write_text('{"allowFrom": []}\n')
_SLACK_ACCESS_DIR = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_SLACK_ACCESS_DIR.mkdir(parents=True, exist_ok=True)
(_SLACK_ACCESS_DIR / "access.json").write_text('{"allowFrom": []}\n')

_TELEGRAM_SPEC = importlib.util.spec_from_file_location(
    "telegram_bridge_archive_policy",
    REPO / "src" / "telegram-bridge.py",
)
_TELEGRAM_MODULE = importlib.util.module_from_spec(_TELEGRAM_SPEC)
_TELEGRAM_SPEC.loader.exec_module(_TELEGRAM_MODULE)


class TaskArchivePolicyTests(unittest.TestCase):
    def test_tasks_and_results_use_injected_monthly_roots(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = root / "tasks-archive"
            results = root / "results-archive"
            fixed = datetime(2026, 8, 2)

            task = root / "task-live.txt"
            task.write_text("task body")
            self.assertTrue(
                archive_file(task, "tasks", "task-1", tasks, results, now=fixed)
            )
            self.assertEqual(
                (tasks / "2026-08" / "task-1.txt").read_text(), "task body"
            )

            result = root / "result-live.txt"
            result.write_text("result body")
            self.assertTrue(
                archive_file(result, "results", "task-2", tasks, results, now=fixed)
            )
            self.assertEqual(
                (results / "2026-08" / "task-2.txt").read_text(), "result body"
            )

    def test_missing_source_is_a_noop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertFalse(
                archive_file(
                    root / "missing.txt",
                    "tasks",
                    "task-missing",
                    root / "ta",
                    root / "ra",
                )
            )
            self.assertFalse((root / "ta").exists())

    def test_failed_move_logs_and_removes_stale_live_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "live.txt"
            source.write_text("stale")
            errors = []
            with patch("task_archive.shutil.move", side_effect=OSError("disk")):
                self.assertFalse(
                    archive_file(
                        source,
                        "tasks",
                        "task-3",
                        root / "ta",
                        root / "ra",
                        on_error=errors.append,
                    )
                )
            self.assertFalse(source.exists())
            self.assertEqual(str(errors[0]), "disk")

    def test_failed_move_contains_logger_and_unlink_failures(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "live.txt"
            source.write_text("stale")

            def broken_logger(_exc):
                raise RuntimeError("logger unavailable")

            with patch("task_archive.shutil.move", side_effect=OSError("disk")):
                with patch.object(Path, "unlink", side_effect=OSError("busy")):
                    self.assertFalse(
                        archive_file(
                            source,
                            "tasks",
                            "task-4",
                            root / "ta",
                            root / "ra",
                            on_error=broken_logger,
                        )
                    )
            self.assertTrue(source.exists())

    def test_telegram_adapter_executes_the_shared_writer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _TELEGRAM_MODULE.ARCHIVE_TASKS_DIR = root / "tasks-archive"
            _TELEGRAM_MODULE.ARCHIVE_RESULTS_DIR = root / "results-archive"
            source = root / "result.txt"
            source.write_text("delivered")
            _TELEGRAM_MODULE.archive_file(source, "results", "task-telegram")
            archived = list((root / "results-archive").glob("*/task-telegram.txt"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_text(), "delivered")

    def test_python_adapters_delegate_instead_of_copying_policy(self):
        for filename in (
            "discord-bridge.py",
            "slack-bridge.py",
            "telegram-bridge.py",
        ):
            source = (REPO / "src" / filename).read_text()
            self.assertIn(
                "from task_archive import archive_file as _archive_file_shared",
                source,
            )
            self.assertNotIn("shutil.move(str(src)", source)


if __name__ == "__main__":
    unittest.main()
