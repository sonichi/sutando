#!/usr/bin/env python3
"""Tests for skills/task-orphan-check/scripts/classify.py — step 2 of the orphan check.

The case that made the rules a script (2026-09-11, desktop v0.6.8-rc3 on engine 4b02fbaf):
the onboarding card queued `task-claude-import-<ms>.txt` (`channel_id: onboarding-wizard`,
task-mid shape, `priority: low`); the core answered the greeting, ran its boot recap and went
idle without starting it. Under the prose rule that consented, resumable task became an ORPHAN
at 300 s — archived into the recovery DM on the next boot instead of being re-emitted by the
watcher's sweep. These pin: an import task that has started is never archived, whatever its
age; one that has not started gets a 30-minute line, not 5; everything else keeps the old rules.

Run: python3 tests/task-orphan-check-classify.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "task-orphan-check" / "scripts" / "classify.py"

NOW = 1789134000.0  # 2026-09-11T13:40:00Z
IMPORT_ID = "task-claude-import-1789133620883"  # queued 13:33:40Z, 379 s before NOW


def _load() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("orphan_classify", SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def import_task_text(task_id: str = IMPORT_ID, queued: float = NOW - 379, tier: str = "owner") -> str:
    # The desktop's legacy writer (claude_import.rs): task-mid — `task:` BEFORE the routing
    # headers, so the strict task-last parser would read channel_id/priority as absent.
    return (f"id: {task_id}\ntimestamp: {iso(queued)}\n"
            "task: Run the import-claude-context skill (onboarding run). Consent given on the "
            "onboarding card at 2026-09-11T13:33:40Z. Index and summarise my Claude Code history.\n"
            "source: chat\ninteraction_type: message\nchannel_id: onboarding-wizard\n"
            f"user_id: onboarding-wizard\naccess_tier: {tier}\npriority: low\n")


def chat_task_text(task_id: str, queued: float, source: str = "chat", extra: str = "") -> str:
    return (f"id: {task_id}\ntimestamp: {iso(queued)}\nsource: {source}\n"
            f"channel_id: local-chat\naccess_tier: owner\npriority: normal\n{extra}"
            "task: Hi — I'm all set up, say hello.\n")


class Workspace:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ws"
        (self.root / "tasks").mkdir(parents=True)
        (self.root / "results").mkdir()

    def task(self, name: str, text: str) -> Path:
        p = self.root / "tasks" / name
        p.write_text(text)
        return p

    def status(self, phase: str, updated: float) -> None:
        d = self.root / "data" / "claude-import"
        d.mkdir(parents=True, exist_ok=True)
        (d / "status.json").write_text(json.dumps({"phase": phase, "updated_at": iso(updated)}))

    def cleanup(self):
        self.tmp.cleanup()


class ClassifyBase(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def one(self, now: float = NOW) -> dict:
        out = self.mod.classify_workspace(self.ws.root, now)
        self.assertEqual(len(out["tasks"]), 1, out)
        return out["tasks"][0]


class TestImportTask(ClassifyBase):
    def test_rc3_incident_import_not_started_is_fresh_at_six_minutes(self):
        """379 s old, no marker, no status.json: the prose rule said ORPHAN; now FRESH."""
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text())
        row = self.one()
        self.assertTrue(row["import"])
        self.assertEqual(row["verdict"], "fresh")
        self.assertEqual(row["channel_id"], "onboarding-wizard", "task-mid headers must be read")
        self.assertEqual(row["age_s"], 379)
        self.assertEqual(row["age_from"], "timestamp")

    def test_import_not_started_past_thirty_minutes_is_orphan(self):
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text(queued=NOW - 1801))
        row = self.one()
        self.assertEqual(row["verdict"], "orphan")
        self.assertIn("never started", row["reason"])

    def test_import_not_started_just_under_thirty_minutes_is_fresh(self):
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text(queued=NOW - 1799))
        self.assertEqual(self.one()["verdict"], "fresh")

    def test_started_import_is_resume_whatever_its_age(self):
        """The acknowledgement's on-disk twin (status.json written after the task) exempts it."""
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text(queued=NOW - 7200))
        self.ws.status("summarizing", NOW - 7000)
        row = self.one()
        self.assertEqual(row["verdict"], "import-resume")
        self.assertIn("summarizing", row["reason"])
        self.assertIn("never archived", row["reason"])

    def test_status_from_an_earlier_import_does_not_count_as_started(self):
        """A previous run's status.json predates this task: it has NOT started."""
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text(queued=NOW - 1801))
        self.ws.status("staged", NOW - 90000)
        self.assertEqual(self.one()["verdict"], "orphan")

    def test_status_phase_done_after_the_task_is_a_completion_marker(self):
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text(queued=NOW - 7200))
        self.ws.status("done", NOW - 100)
        row = self.one()
        self.assertEqual(row["verdict"], "done")
        self.assertIn("phase done", row["reason"])

    def test_result_file_beats_everything(self):
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text(queued=NOW - 7200))
        (self.ws.root / "results" / f"{IMPORT_ID}.txt").write_text("[no-send]\n")
        row = self.one()
        self.assertEqual(row["verdict"], "done")
        self.assertFalse(row["import"], "marker check runs before the import branch")

    def test_owner_body_naming_the_skill_counts_as_import(self):
        text = chat_task_text("task-chat-1", NOW - 1000,
                              extra="").replace("say hello.", "run /import-claude-context now.")
        self.ws.task("task-chat-1.txt", text)
        row = self.one()
        self.assertTrue(row["import"])
        self.assertEqual(row["verdict"], "fresh")

    def test_non_owner_task_naming_the_skill_gets_no_exemption(self):
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text(queued=NOW - 1000, tier="team"))
        row = self.one()
        self.assertFalse(row["import"])
        self.assertEqual(row["verdict"], "orphan")

    def test_unreadable_status_json_means_not_started(self):
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text(queued=NOW - 1000))
        d = self.ws.root / "data" / "claude-import"
        d.mkdir(parents=True)
        (d / "status.json").write_text("{not json")
        self.assertEqual(self.one()["verdict"], "fresh")
        (d / "status.json").write_text("[1, 2]")
        self.assertEqual(self.one()["verdict"], "fresh")

    def test_status_without_updated_at_falls_back_to_its_mtime(self):
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text(queued=NOW - 1000))
        d = self.ws.root / "data" / "claude-import"
        d.mkdir(parents=True)
        p = d / "status.json"
        p.write_text(json.dumps({"phase": "indexed"}))
        os.utime(p, (NOW - 500, NOW - 500))
        self.assertEqual(self.one()["verdict"], "import-resume")
        os.utime(p, (NOW - 5000, NOW - 5000))
        self.assertEqual(self.one()["verdict"], "fresh")


class TestOrdinaryTasks(ClassifyBase):
    def test_fresh_under_five_minutes(self):
        self.ws.task("task-a8c6d205.txt", chat_task_text("task-a8c6d205", NOW - 299))
        row = self.one()
        self.assertEqual(row["verdict"], "fresh")
        self.assertFalse(row["import"])
        self.assertEqual(row["label"], "local-chat")

    def test_orphan_at_five_minutes(self):
        self.ws.task("task-a8c6d205.txt", chat_task_text("task-a8c6d205", NOW - 300))
        self.assertEqual(self.one()["verdict"], "orphan")

    def test_done_by_live_result(self):
        self.ws.task("task-1.txt", chat_task_text("task-1", NOW - 9999))
        (self.ws.root / "results" / "task-1.txt").write_text("done\n")
        self.assertEqual(self.one()["verdict"], "done")

    def test_done_by_archived_result(self):
        self.ws.task("task-1.txt", chat_task_text("task-1", NOW - 9999))
        (self.ws.root / "results" / "archive" / "2026-09").mkdir(parents=True)
        (self.ws.root / "results" / "archive" / "2026-09" / "task-1.txt").write_text("done\n")
        row = self.one()
        self.assertEqual(row["verdict"], "done")
        self.assertIn("archive", row["reason"])

    def test_done_by_proactive_marker_and_sending_variant(self):
        self.ws.task("task-1.txt", chat_task_text("task-1", NOW - 9999))
        (self.ws.root / "results" / "proactive-task-1.txt").write_text("x\n")
        self.assertEqual(self.one()["verdict"], "done")
        (self.ws.root / "results" / "proactive-task-1.txt").rename(
            self.ws.root / "results" / "proactive-task-1.txt.sending")
        self.assertEqual(self.one()["verdict"], "done")

    def test_label_prefers_room_name_with_id(self):
        self.ws.task("task-1.txt", chat_task_text("task-1", NOW - 10, source="ag2space",
                                                  extra="room_name: Sutando DM\n"))
        self.assertEqual(self.one()["label"], "Sutando DM (local-chat)")

    def test_voice_source_and_legacy_tier_alias_are_reported(self):
        self.ws.task("task-1.txt", "id: task-1\nsource: voice\naccess_tier: other\n"
                                   f"timestamp: {iso(NOW - 400)}\ntask: call\n")
        row = self.one()
        self.assertEqual(row["source"], "voice")
        self.assertEqual(row["access_tier"], "guest")
        self.assertEqual(row["verdict"], "orphan")


class TestAgeSources(ClassifyBase):
    def test_bad_timestamp_falls_back_to_epoch_ms_in_id(self):
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text().replace(iso(NOW - 379), "yesterday"))
        row = self.one()
        self.assertEqual(row["age_from"], "id")
        self.assertEqual(row["age_s"], 379)

    def test_no_timestamp_no_epoch_falls_back_to_mtime(self):
        p = self.ws.task("task-abc.txt", "id: task-abc\nsource: chat\ntask: hi\n")
        os.utime(p, (NOW - 50, NOW - 50))
        row = self.one()
        self.assertEqual(row["age_from"], "mtime")
        self.assertEqual(row["verdict"], "fresh")

    def test_missing_id_header_uses_the_filename(self):
        self.ws.task("task-noid.txt", "source: chat\ntask: hi\n")
        self.assertEqual(self.one()["id"], "task-noid")

    def test_parse_iso_shapes(self):
        p = self.mod.parse_iso
        self.assertEqual(p("2026-09-11T13:33:40Z"), 1789133620.0)
        self.assertEqual(p("2026-09-11T13:33:40+00:00"), 1789133620.0)
        self.assertEqual(p("2026-09-11T13:33:40"), 1789133620.0)
        self.assertIsNone(p(""))
        self.assertIsNone(p("not a date"))

    def test_stat_failure_reads_as_epoch_zero(self):
        gone = self.ws.root / "tasks" / "task-gone.txt"
        epoch, how = self.mod.task_queued_at({}, "task-gone", gone)
        self.assertEqual((epoch, how), (0.0, "mtime"))


class TestWorkspaceShapes(ClassifyBase):
    def test_missing_workspace(self):
        out = self.mod.classify_workspace(self.ws.root / "nope", NOW)
        self.assertIn("workspace not found", out["note"])
        self.assertEqual(out["tasks"], [])

    def test_missing_tasks_dir(self):
        (self.ws.root / "tasks").rmdir()
        out = self.mod.classify_workspace(self.ws.root, NOW)
        self.assertIn("no tasks/ dir", out["note"])

    def test_counts_and_only_top_level_txt(self):
        self.ws.task("task-1.txt", chat_task_text("task-1", NOW - 10))
        self.ws.task("task-2.txt", chat_task_text("task-2", NOW - 1000))
        self.ws.task("task-3.txt.deferred", chat_task_text("task-3", NOW - 1000))
        (self.ws.root / "tasks" / "archive").mkdir()
        (self.ws.root / "tasks" / "archive" / "task-4.txt").write_text("x")
        out = self.mod.classify_workspace(self.ws.root, NOW)
        self.assertEqual(out["counts"], {"fresh": 1, "orphan": 1, "total": 2})

    def test_unreadable_task_file_is_a_conservative_orphan(self):
        p = self.ws.task("task-1.txt", chat_task_text("task-1", NOW - 10))
        p.chmod(0)
        try:
            out = self.mod.classify_workspace(self.ws.root, NOW)
        finally:
            p.chmod(0o600)
        if os.geteuid() == 0:  # root reads anything; nothing to assert
            return
        self.assertEqual(out["tasks"][0]["verdict"], "orphan")
        self.assertIn("unreadable", out["tasks"][0]["reason"])

    def test_default_now_is_wall_clock(self):
        self.ws.task("task-1.txt", chat_task_text("task-1", 1.0))
        out = self.mod.classify_workspace(self.ws.root)
        self.assertEqual(out["tasks"][0]["verdict"], "orphan")


class TestCli(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def run_cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                              text=True, env={**os.environ, **(env or {})})

    def test_json_on_stdout_with_explicit_workspace(self):
        self.ws.task(f"{IMPORT_ID}.txt", import_task_text())
        res = self.run_cli("--workspace", str(self.ws.root), "--now", str(NOW))
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["counts"], {"fresh": 1, "total": 1})
        self.assertEqual(out["tasks"][0]["verdict"], "fresh")

    def test_workspace_resolved_through_sutando_config(self):
        mod = _load()
        fake = types.SimpleNamespace(stdout=f"{self.ws.root}\n", returncode=0)
        with unittest.mock.patch.object(mod.subprocess, "run", return_value=fake):
            self.assertEqual(mod.resolve_workspace(), self.ws.root)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(mod.main([]), 0)
            self.assertEqual(json.loads(buf.getvalue())["counts"], {"total": 0})
        with unittest.mock.patch.object(mod.subprocess, "run",
                                        return_value=types.SimpleNamespace(stdout="", returncode=1)):
            self.assertIsNone(mod.resolve_workspace())
            self.assertEqual(mod.main([]), 2)
        with unittest.mock.patch.object(mod.subprocess, "run", side_effect=OSError("no bash")):
            self.assertIsNone(mod.resolve_workspace())


if __name__ == "__main__":
    unittest.main(verbosity=1)
