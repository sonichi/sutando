#!/usr/bin/env python3
"""Tests for src/learn-collector.py and src/learn-log-tail.py (#586 + #590)"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load("learn_collector", REPO / "src" / "learn-collector.py")
log_tail = _load("learn_log_tail", REPO / "src" / "learn-log-tail.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_root(tmp: str) -> Path:
    root = Path(tmp)
    (root / "state").mkdir(exist_ok=True)
    (root / "tasks").mkdir(exist_ok=True)
    (root / "results" / "archive").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# learn-collector tests
# ---------------------------------------------------------------------------

class TestResolveInstance(unittest.TestCase):
    def test_stand_identity_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            identity = root / "stand-identity.json"
            identity.write_text(json.dumps({"machine": "mini"}))
            with unittest.mock.patch.object(collector, "ROOT", root):
                inst = collector.resolve_instance()
            self.assertEqual(inst, "mini")

    def test_stand_identity_instance_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            (root / "stand-identity.json").write_text(json.dumps({"instance": "studio"}))
            with unittest.mock.patch.object(collector, "ROOT", root):
                inst = collector.resolve_instance()
            self.assertEqual(inst, "studio")

    def test_hostname_fallback_mbp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            import socket
            with unittest.mock.patch.object(collector, "ROOT", root), \
                 unittest.mock.patch("socket.gethostname", return_value="bassils-mbp.local"):
                inst = collector.resolve_instance()
            self.assertEqual(inst, "macbook")

    def test_hostname_fallback_mini(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            with unittest.mock.patch.object(collector, "ROOT", root), \
                 unittest.mock.patch("socket.gethostname", return_value="mac-mini.local"):
                inst = collector.resolve_instance()
            self.assertEqual(inst, "mini")


class TestEmit(unittest.TestCase):
    def test_emit_writes_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            jsonl = root / "state" / "learning-test.jsonl"
            with unittest.mock.patch.object(collector, "STATE_DIR", root / "state"), \
                 unittest.mock.patch.object(collector, "JSONL_PATH", jsonl), \
                 unittest.mock.patch.object(collector, "INSTANCE", "test"):
                collector.emit("tasks", "incoming", {"task_id": "task-123", "path": "tasks/task-123.txt"})
            lines = jsonl.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            event = json.loads(lines[0])
            self.assertEqual(event["source"], "tasks")
            self.assertEqual(event["kind"], "incoming")
            self.assertEqual(event["instance"], "test")
            self.assertIn("ts", event)
            self.assertIn("task_id", event["data"])

    def test_emit_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            jsonl = root / "state" / "learning-test.jsonl"
            with unittest.mock.patch.object(collector, "STATE_DIR", root / "state"), \
                 unittest.mock.patch.object(collector, "JSONL_PATH", jsonl), \
                 unittest.mock.patch.object(collector, "INSTANCE", "test"):
                collector.emit("tasks", "incoming", {"task_id": "t1"})
                collector.emit("results", "produced", {"task_id": "t1"})
            lines = jsonl.read_text().splitlines()
            self.assertEqual(len(lines), 2)


class TestCursorPersistence(unittest.TestCase):
    def test_save_and_load_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            cursor_path = root / "state" / "learning-test.cursor"
            with unittest.mock.patch.object(collector, "STATE_DIR", root / "state"), \
                 unittest.mock.patch.object(collector, "CURSOR_PATH", cursor_path):
                cursor = {"tasks": 1234567890.0}
                collector.save_cursor(cursor)
                loaded = collector.load_cursor()
            self.assertEqual(loaded, cursor)

    def test_load_cursor_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            cursor_path = root / "state" / "nonexistent.cursor"
            with unittest.mock.patch.object(collector, "CURSOR_PATH", cursor_path):
                result = collector.load_cursor()
            self.assertEqual(result, {})


class TestScanDir(unittest.TestCase):
    def test_emits_new_task_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            tasks_dir = root / "tasks"
            (tasks_dir / "task-111.txt").write_text("hello")
            jsonl = root / "state" / "learning-test.jsonl"
            cursor = {}
            with unittest.mock.patch.object(collector, "STATE_DIR", root / "state"), \
                 unittest.mock.patch.object(collector, "JSONL_PATH", jsonl), \
                 unittest.mock.patch.object(collector, "ROOT", root), \
                 unittest.mock.patch.object(collector, "INSTANCE", "test"):
                count = collector.scan_dir(tasks_dir, "tasks", cursor, "tasks", "incoming", recursive=False)
            self.assertEqual(count, 1)
            event = json.loads(jsonl.read_text().splitlines()[0])
            self.assertEqual(event["kind"], "incoming")
            self.assertIn("task_id", event["data"])

    def test_cursor_prevents_duplicate_emit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            tasks_dir = root / "tasks"
            tf = tasks_dir / "task-222.txt"
            tf.write_text("hello")
            jsonl = root / "state" / "learning-test.jsonl"
            cursor = {}
            with unittest.mock.patch.object(collector, "STATE_DIR", root / "state"), \
                 unittest.mock.patch.object(collector, "JSONL_PATH", jsonl), \
                 unittest.mock.patch.object(collector, "ROOT", root), \
                 unittest.mock.patch.object(collector, "INSTANCE", "test"):
                collector.scan_dir(tasks_dir, "tasks", cursor, "tasks", "incoming", recursive=False)
                count2 = collector.scan_dir(tasks_dir, "tasks", cursor, "tasks", "incoming", recursive=False)
            self.assertEqual(count2, 0)

    def test_missing_dir_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            nonexistent = root / "no-such-dir"
            jsonl = root / "state" / "learning-test.jsonl"
            cursor = {}
            with unittest.mock.patch.object(collector, "STATE_DIR", root / "state"), \
                 unittest.mock.patch.object(collector, "JSONL_PATH", jsonl), \
                 unittest.mock.patch.object(collector, "INSTANCE", "test"):
                count = collector.scan_dir(nonexistent, "x", cursor, "x", "incoming", recursive=False)
            self.assertEqual(count, 0)


# ---------------------------------------------------------------------------
# learn-log-tail tests
# ---------------------------------------------------------------------------

class TestCompileFilters(unittest.TestCase):
    def test_groups_by_log_name(self):
        filters = [
            ("discord-bridge.log", "skip", r"\[skip\]"),
            ("discord-bridge.log", "error", r"ERROR"),
            ("voice-agent.log", "error", r"ERROR"),
        ]
        grouped = log_tail.compile_filters(filters)
        self.assertIn("discord-bridge.log", grouped)
        self.assertIn("voice-agent.log", grouped)
        self.assertEqual(len(grouped["discord-bridge.log"]), 2)
        self.assertEqual(len(grouped["voice-agent.log"]), 1)

    def test_compiles_regex(self):
        filters = [("discord-bridge.log", "error", r"\bERROR\b")]
        grouped = log_tail.compile_filters(filters)
        kind, pattern = grouped["discord-bridge.log"][0]
        self.assertEqual(kind, "error")
        self.assertIsNotNone(pattern.search("ERROR: something bad"))
        self.assertIsNone(pattern.search("no errors here"))


class TestTailLog(unittest.TestCase):
    def _patched_emit(self, root, events_out):
        def _emit(source, kind, data):
            events_out.append({"source": source, "kind": kind, "data": data})
        return _emit

    def test_emits_matching_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            log_path = root / "logs" / "discord-bridge.log"
            log_path.write_text("[skip] some message\n")
            cursor = {}
            filters = [("skip", __import__("re").compile(r"\[skip\]"))]
            events = []
            with unittest.mock.patch.object(log_tail, "emit", self._patched_emit(root, events)):
                count = log_tail.tail_log(log_path, filters, cursor)
            self.assertEqual(count, 1)
            self.assertEqual(events[0]["kind"], "skip")

    def test_does_not_replay_on_second_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            log_path = root / "logs" / "discord-bridge.log"
            log_path.write_text("[skip] once\n")
            cursor = {}
            filters = [("skip", __import__("re").compile(r"\[skip\]"))]
            events = []
            with unittest.mock.patch.object(log_tail, "emit", self._patched_emit(root, events)):
                log_tail.tail_log(log_path, filters, cursor)
                count2 = log_tail.tail_log(log_path, filters, cursor)
            self.assertEqual(count2, 0)

    def test_handles_missing_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            log_path = root / "logs" / "nonexistent.log"
            cursor = {}
            filters = [("error", __import__("re").compile(r"ERROR"))]
            events = []
            with unittest.mock.patch.object(log_tail, "emit", self._patched_emit(root, events)):
                count = log_tail.tail_log(log_path, filters, cursor)
            self.assertEqual(count, 0)

    def test_handles_log_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            log_path = root / "logs" / "discord-bridge.log"
            log_path.write_text("ERROR: bad\n")
            cursor = {"discord-bridge.log": 9999}  # cursor beyond file size
            filters = [("error", __import__("re").compile(r"ERROR"))]
            events = []
            with unittest.mock.patch.object(log_tail, "emit", self._patched_emit(root, events)):
                log_tail.tail_log(log_path, filters, cursor)
            rotated_events = [e for e in events if e["kind"] == "rotated"]
            self.assertTrue(rotated_events, "expected a 'rotated' event on offset reset")

    def test_first_match_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            log_path = root / "logs" / "discord-bridge.log"
            log_path.write_text("[skip] but also ERROR here\n")
            cursor = {}
            import re
            filters = [
                ("skip", re.compile(r"\[skip\]")),
                ("error", re.compile(r"ERROR")),
            ]
            events = []
            with unittest.mock.patch.object(log_tail, "emit", self._patched_emit(root, events)):
                log_tail.tail_log(log_path, filters, cursor)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["kind"], "skip")

    def test_partial_line_not_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            log_path = root / "logs" / "discord-bridge.log"
            log_path.write_bytes(b"[skip] complete\npartial no newline")
            cursor = {}
            filters = [("skip", __import__("re").compile(r"\[skip\]"))]
            events = []
            with unittest.mock.patch.object(log_tail, "emit", self._patched_emit(root, events)):
                log_tail.tail_log(log_path, filters, cursor)
            self.assertEqual(len(events), 1)
            # cursor should be at end of "complete\n", not end of file
            self.assertLess(cursor["discord-bridge.log"], log_path.stat().st_size)


class TestLoadFilters(unittest.TestCase):
    def test_default_filters_returned_without_custom_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            with unittest.mock.patch.object(log_tail, "STATE_DIR", root / "state"):
                filters = log_tail.load_filters()
            self.assertIs(filters, log_tail.DEFAULT_FILTERS)

    def test_custom_filters_loaded_from_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            custom = root / "state" / "learn-filters.json"
            custom.write_text(json.dumps([["mylog.log", "custom_kind", r"CUSTOM"]]))
            with unittest.mock.patch.object(log_tail, "STATE_DIR", root / "state"):
                filters = log_tail.load_filters()
            self.assertEqual(len(filters), 1)
            self.assertEqual(filters[0][1], "custom_kind")


class TestWorkspacePaths(unittest.TestCase):
    """Verify both daemons route state files through resolve_workspace (#949)."""

    def test_collector_state_dir_under_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            ws.mkdir()
            with unittest.mock.patch.dict(os.environ, {"SUTANDO_WORKSPACE": str(ws)}):
                mod = _load("lc_ws", REPO / "src" / "learn-collector.py")
            self.assertEqual(str(mod.STATE_DIR), str(ws / "state"))

    def test_collector_tasks_dir_under_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            ws.mkdir()
            with unittest.mock.patch.dict(os.environ, {"SUTANDO_WORKSPACE": str(ws)}):
                mod = _load("lc_ws2", REPO / "src" / "learn-collector.py")
            self.assertEqual(str(mod.TASKS_DIR), str(ws / "tasks"))

    def test_collector_results_archive_under_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            ws.mkdir()
            with unittest.mock.patch.dict(os.environ, {"SUTANDO_WORKSPACE": str(ws)}):
                mod = _load("lc_ws3", REPO / "src" / "learn-collector.py")
            self.assertEqual(str(mod.RESULTS_ARCHIVE_DIR), str(ws / "results" / "archive"))

    def test_log_tail_state_dir_under_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            ws.mkdir()
            with unittest.mock.patch.dict(os.environ, {"SUTANDO_WORKSPACE": str(ws)}):
                mod = _load("lt_ws", REPO / "src" / "learn-log-tail.py")
            self.assertEqual(str(mod.STATE_DIR), str(ws / "state"))

    def test_log_tail_logs_dir_under_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            ws.mkdir()
            with unittest.mock.patch.dict(os.environ, {"SUTANDO_WORKSPACE": str(ws)}):
                mod = _load("lt_ws2", REPO / "src" / "learn-log-tail.py")
            self.assertEqual(str(mod.LOGS_DIR), str(ws / "logs"))

    def test_collector_state_dir_not_under_repo(self):
        self.assertFalse(
            str(collector.STATE_DIR).startswith(str(REPO)),
            f"STATE_DIR {collector.STATE_DIR} should not be inside repo {REPO}",
        )

    def test_log_tail_state_dir_not_under_repo(self):
        self.assertFalse(
            str(log_tail.STATE_DIR).startswith(str(REPO)),
            f"STATE_DIR {log_tail.STATE_DIR} should not be inside repo {REPO}",
        )


class TestDefaultFilterCoverage(unittest.TestCase):
    def test_discord_bridge_filters_defined(self):
        logs = {f[0] for f in log_tail.DEFAULT_FILTERS}
        self.assertIn("discord-bridge.log", logs)

    def test_voice_agent_filters_defined(self):
        logs = {f[0] for f in log_tail.DEFAULT_FILTERS}
        self.assertIn("voice-agent.log", logs)

    def test_conversation_server_filters_defined(self):
        logs = {f[0] for f in log_tail.DEFAULT_FILTERS}
        self.assertIn("conversation-server.log", logs)

    def test_telegram_bridge_filters_defined(self):
        logs = {f[0] for f in log_tail.DEFAULT_FILTERS}
        self.assertIn("telegram-bridge.log", logs)

    def test_error_pattern_in_all_logs(self):
        for log_name, kind, pattern in log_tail.DEFAULT_FILTERS:
            if kind == "error":
                import re
                self.assertIsNotNone(re.compile(pattern).search("ERROR: something"), f"{log_name} error pattern broken")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import unittest.mock
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestResolveInstance, TestEmit, TestCursorPersistence, TestScanDir,
        TestCompileFilters, TestTailLog, TestLoadFilters, TestDefaultFilterCoverage,
        TestWorkspacePaths,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
