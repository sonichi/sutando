#!/usr/bin/env python3
"""Tests for the durable Codex schedule-crons runner."""

import importlib.util
import json
import plistlib
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "schedule-crons" / "scripts" / "codex-scheduler.py"
SPEC = importlib.util.spec_from_file_location("codex_scheduler", SCRIPT)
assert SPEC and SPEC.loader
scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler)


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 15, hour, minute, tzinfo=timezone.utc)


def config(workspace: Path, **overrides):
    job = {
        "name": "daily-news",
        "cron": "0 6 * * *",
        "timezone": "UTC",
        "execution": "codex-task",
        "delivery": "proactive",
        "prompt": "Make the news digest.",
        "retry_minutes": 5,
        "max_attempts": 2,
    }
    job.update(overrides)
    path = workspace / "hosts" / "test-host" / "crons.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"name": "session-job", "cron": "* * * * *", "prompt": "ignore"}, job]))


def test_cron_parser():
    local = datetime(2026, 7, 15, 6, 10, tzinfo=timezone.utc)
    assert scheduler.cron_matches("*/5 6 15 7 *", local)
    assert scheduler.cron_matches("10 6 * * 3", local)
    assert not scheduler.cron_matches("11 6 * * *", local)


def test_enqueue_retry_complete_and_no_duplicate():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        config(ws)
        first = scheduler.tick(ws, "test-host", at(6, 0))
        assert first["job_count"] == 1
        assert first["events"][0]["event"] == "enqueued"
        tasks = list((ws / "tasks").glob("*.txt"))
        assert len(tasks) == 1
        body = tasks[0].read_text()
        assert "source: cron" in body and "priority: low" in body
        assert "proactive-daily-news" in body and "[no-send]" in body

        assert scheduler.tick(ws, "test-host", at(6, 1))["events"] == []
        retry = scheduler.tick(ws, "test-host", at(6, 5))
        assert retry["events"] == [{"job": "daily-news", "event": "retried", "attempt": 2}]
        assert len(list((ws / "tasks").glob("*.txt"))) == 1

        task_id = tasks[0].stem
        result = ws / "results" / f"{task_id}.txt"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text("[no-send]\n")
        done = scheduler.tick(ws, "test-host", at(6, 6))
        assert done["events"] == [{"job": "daily-news", "event": "completed"}]
        state = json.loads((ws / "state" / "schedules" / "codex-scheduler.json").read_text())
        assert state["jobs"]["daily-news"]["last_success_at"]
        assert state["jobs"]["daily-news"]["current"] is None


def test_failure_alert_and_health():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        config(ws, max_attempts=1)
        scheduler.tick(ws, "test-host", at(6, 0))
        failed = scheduler.tick(ws, "test-host", at(6, 5))
        assert failed["events"] == [{"job": "daily-news", "event": "failed"}]
        assert len(list((ws / "results").glob("proactive-schedule-alert-*.txt"))) == 1
        code, report = scheduler.health(ws, "test-host", at(6, 5))
        assert code == 1 and "latest run failed" in report["problems"][0]


def test_wake_catchup_and_timezone():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        config(ws, cron="0 6 * * *", timezone="America/Los_Angeles")
        scheduler.tick(ws, "test-host", at(12, 59))  # 05:59 PDT
        caught = scheduler.tick(ws, "test-host", at(13, 7))  # woke after 06:00 PDT
        assert caught["events"][0]["event"] == "enqueued"
        assert caught["events"][0]["task_id"].endswith(str(int(at(13, 0).timestamp())))


def test_install_plist():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "workspace"
        home = Path(td) / "home"
        config(ws)
        with mock.patch.object(Path, "home", return_value=home):
            path = scheduler.install(ws, "test-host", REPO, write_only=True)
        with path.open("rb") as handle:
            plist = plistlib.load(handle)
        assert plist["Label"] == scheduler.LABEL
        assert plist["StartCalendarInterval"] == {}
        assert plist["RunAtLoad"] is True
        assert str(ws) in plist["ProgramArguments"]


def main():
    tests = [
        test_cron_parser,
        test_enqueue_retry_complete_and_no_duplicate,
        test_failure_alert_and_health,
        test_wake_catchup_and_timezone,
        test_install_plist,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print("All codex-scheduler tests passed.")


if __name__ == "__main__":
    main()
