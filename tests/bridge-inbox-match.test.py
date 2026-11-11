#!/usr/bin/env python3
"""A bridge started from another install's engine writes tasks the core never reads.

Field case: the Slack bridge kept running from an older app's engine, so it wrote
into that install's workspace while the core watched the desktop app's. Each
bridge now reports the inbox it uses; the probe compares the reports of live
bridge pids with the core's inbox and never guesses for a bridge without one.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))  # `python3 -I` does not add the script dir

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-bridge-inbox-")
_slack_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_slack_cfg.mkdir(parents=True, exist_ok=True)
(_slack_cfg / "access.json").write_text("{}")
_discord_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_discord_cfg.mkdir(parents=True, exist_ok=True)
(_discord_cfg / "access.json").write_text("{}")
_telegram_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_telegram_cfg.mkdir(parents=True, exist_ok=True)
(_telegram_cfg / "access.json").write_text("{}")
sys.path.insert(0, str(ROOT / "src"))

import bridge_inbox  # noqa: E402

START = {"111": "lstart:A", "222": "lstart:B", "333": "lstart:C"}


class Probe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dir = self.tmp / "bridges"
        self.core = self.tmp / "Application Support" / "desktop" / "workspace" / "tasks"
        self.other = self.tmp / "Application Support" / "old-app" / "workspace" / "tasks"
        self.core.mkdir(parents=True)
        self.other.mkdir(parents=True)

    def _write(self, bridge, pid, tasks_dir, start=None):
        with mock.patch.object(bridge_inbox, "process_start",
                               lambda p: start or START.get(str(p))):
            return bridge_inbox.report_inbox(bridge, tasks_dir, directory=self.dir, pid=int(pid))

    def _probe(self, live: dict, starts=None):
        starts = starts or START
        return bridge_inbox.check_bridge_inbox_match(
            [self.core], lambda script: [p for p, s in live.items() if s == script],
            directory=self.dir,
            argv_of=lambda pid: ["python3", f"/engines/e{pid}/src/{live[pid]}"],
            cwd_of=lambda pid: "/", start_of=lambda pid: starts.get(str(pid)))

    def test_record_matching_core_is_ok(self):
        self._write("slack-bridge", 111, self.core)
        got = self._probe({"111": "slack-bridge.py"})
        self.assertEqual(got["status"], "ok", got)
        self.assertEqual(got["detail"], "1 bridge(s) write the core inbox")

    def test_mismatch_warns_naming_both_paths(self):
        self._write("slack-bridge", 111, self.other)
        got = self._probe({"111": "slack-bridge.py"})
        self.assertEqual(got["status"], "warn", got)
        self.assertIn(os.path.realpath(self.other), got["detail"])
        self.assertIn(os.path.realpath(self.core), got["detail"])

    def test_dead_pid_record_is_ignored(self):
        self._write("slack-bridge", 222, self.other)  # pid 222 no longer runs
        self._write("slack-bridge", 111, self.core)
        got = self._probe({"111": "slack-bridge.py"})
        self.assertEqual(got["status"], "ok", got)
        self.assertNotIn("222", got["detail"])

    def test_reused_pid_is_stale_not_a_match(self):
        self._write("slack-bridge", 111, self.core)
        got = self._probe({"111": "slack-bridge.py"}, starts={"111": "lstart:LATER"})
        self.assertEqual(got["status"], "ok", got)
        self.assertIn("stale (pid reused)", got["detail"])
        self.assertNotIn("write the core inbox", got["detail"])

    def test_live_bridge_without_record_is_unknown_with_hint(self):
        got = self._probe({"333": "telegram-bridge.py"})
        self.assertEqual(got["status"], "ok", got)
        self.assertIn("telegram-bridge pid 333 from /engines/e333: inbox record missing", got["detail"])
        self.assertIn("may predate inbox reporting", got["detail"])
        self.assertIn("destination is unverified; restart it from the desktop engine", got["detail"])

    def test_malformed_and_unreadable_records_are_unknown(self):
        self.dir.mkdir(parents=True)
        (self.dir / "slack-bridge-111.json").write_text("{not json")
        bad = {"version": 1, "pid": 222, "bridge": "slack-bridge", "tasks_dir": "relative",
               "engine_dir": "/e", "start": "lstart:B", "started_at": 1}
        (self.dir / "discord-bridge-222.json").write_text(json.dumps(bad))
        (self.dir / "telegram-bridge-333.json").mkdir()  # a directory cannot be read as a file
        got = self._probe({"111": "slack-bridge.py", "222": "discord-bridge.py",
                           "333": "telegram-bridge.py"})
        self.assertEqual(got["status"], "ok", got)
        self.assertIn("slack-bridge pid 111 from /engines/e111: inbox record malformed", got["detail"])
        self.assertIn("discord-bridge pid 222 from /engines/e222: inbox record malformed", got["detail"])
        self.assertIn("telegram-bridge pid 333 from /engines/e333: inbox record unreadable", got["detail"])

    def test_writer_fails_open(self):
        blocker = self.tmp / "file-not-dir"
        blocker.write_text("x")
        self.assertIsNone(bridge_inbox.report_inbox("slack-bridge", self.core, directory=blocker))
        self.assertIsNone(bridge_inbox.report_inbox("not-a-bridge", self.core, directory=self.dir))

    def test_concurrent_writers_leave_only_valid_records(self):
        pids = [os.getpid(), os.getppid()]
        errors = []

        def write(bridge, pid, n):
            for _ in range(n):
                if bridge_inbox.report_inbox(bridge, self.core, directory=self.dir, pid=pid) is None:
                    errors.append((bridge, pid))
        threads = [threading.Thread(target=write, args=(b, p, 25))
                   for b in bridge_inbox.BRIDGE_SCRIPTS for p in pids for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith(".")), [])
        for b in bridge_inbox.BRIDGE_SCRIPTS:
            for p in pids:
                rec, why = bridge_inbox.read_report(b, p, directory=self.dir)
                self.assertEqual(why, "ok", (b, p))
                self.assertEqual(rec["tasks_dir"], os.path.realpath(self.core))

    def test_record_schema(self):
        target = bridge_inbox.report_inbox("slack-bridge", self.core, directory=self.dir)
        rec = json.loads(target.read_text())
        self.assertEqual(set(rec), {"version", "pid", "bridge", "tasks_dir", "engine_dir",
                                    "start", "started_at"})
        self.assertEqual(rec["version"], 1)
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual(rec["engine_dir"], os.path.realpath(ROOT))
        self.assertEqual(rec["start"], bridge_inbox.process_start(os.getpid()))


class HealthCheckDelegates(unittest.TestCase):
    def test_health_check_reads_the_reports(self):
        spec = importlib.util.spec_from_file_location("health_check", ROOT / "src" / "health-check.py")
        health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(health)
        self.assertIs(health.bridge_inbox, bridge_inbox)
        tmp = Path(tempfile.mkdtemp())
        core, other = tmp / "core" / "tasks", tmp / "other" / "tasks"
        pid = os.getpid()
        stubs = [
            mock.patch.object(health, "_core_inboxes", lambda: [str(core)]),
            mock.patch.object(health, "probe_pids",
                              lambda pat: ([str(pid)], True) if "slack" in pat else ([], True)),
            mock.patch.object(health, "_drop_launcher_parents", lambda pids: pids),
            mock.patch.object(bridge_inbox, "sentinel_dir", lambda: tmp / "bridges"),
        ]
        for st in stubs:
            st.start()
            self.addCleanup(st.stop)
        results = {}
        for label, inbox in (("core", core), ("other", other)):
            bridge_inbox.report_inbox("slack-bridge", inbox)
            results[label] = health.check_bridge_inbox_match()
        self.assertEqual(results["core"]["status"], "ok", results)
        self.assertEqual(results["other"]["status"], "warn", results)
        self.assertIn(os.path.realpath(other), results["other"]["detail"])

    def test_health_check_runs_the_probe(self):
        src = (ROOT / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_bridge_inbox_match())", src)


if __name__ == "__main__":
    unittest.main()
