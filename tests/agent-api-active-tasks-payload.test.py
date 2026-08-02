#!/usr/bin/env python3
"""Contract tests for agent-api's extracted /tasks/active payload builder."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location(
    "agent_api_active_tasks", REPO / "src" / "agent-api.py"
)
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
assert spec.loader is not None
spec.loader.exec_module(api)


def test_payload_reconciles_files_history_and_questions() -> None:
    original = (api.TASK_DIR, api.RESULT_DIR, api.WORKSPACE_DIR)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        api.TASK_DIR = root / "tasks"
        api.RESULT_DIR = root / "results"
        api.WORKSPACE_DIR = root
        api.TASK_DIR.mkdir()
        (api.RESULT_DIR / "archive" / "2026-08").mkdir(parents=True)
        api.task_history.clear()

        task = api.TASK_DIR / "task-active.txt"
        task.write_text("source: discord\ntask: preserve this title\n")
        os.utime(task, (2000, 2000))
        archived = api.RESULT_DIR / "archive" / "2026-08" / task.name
        archived.write_text("archived result\n")

        live_task = api.TASK_DIR / "task-live-result.txt"
        live_task.write_text("source: api\ntask: live result wins\n")
        os.utime(live_task, (2500, 2500))
        (api.RESULT_DIR / live_task.name).write_text("live result\n")

        remembered_task = api.TASK_DIR / "task-remembered.txt"
        remembered_task.write_text("source: voice\ntask: remembered result survives\n")
        os.utime(remembered_task, (1500, 1500))
        api.task_history["task-remembered"] = {
            "status": "done",
            "text": "remembered result survives",
            "time": 1500,
            "result": "remembered result",
            "source": "voice",
        }

        result_only = api.RESULT_DIR / "task-result-only.txt"
        result_only.write_text("result summary\n")
        os.utime(result_only, (3000, 3000))

        api.task_history["task-stale"] = {
            "status": "working",
            "text": "stale",
            "time": 0,
            "result": "",
            "source": "",
        }

        pending = root / "pending-questions.md"
        pending.write_text("## Choose a mode\n\nPick one.\n\n**Options:** A | B\n")

        try:
            with mock.patch.object(api, "personal_path", return_value=pending):
                payload = api._active_tasks_payload(watcher_ok=True, core_ok=False)
        finally:
            api.TASK_DIR, api.RESULT_DIR, api.WORKSPACE_DIR = original
            api.task_history.clear()

    assert set(payload) == {"tasks", "watcher", "claude", "questions"}
    assert payload["watcher"] is True
    assert payload["claude"] is False
    rows = {row["id"]: row for row in payload["tasks"]}
    assert "task-stale" not in rows
    assert rows["task-active"] == {
        "id": "task-active",
        "status": "done",
        "text": "preserve this title",
        "time": 2000.0,
        "result": "archived result",
        "source": "discord",
    }
    assert rows["task-result-only"]["status"] == "done"
    assert rows["task-result-only"]["text"] == "result summary"
    assert rows["task-live-result"]["result"] == "live result"
    assert rows["task-remembered"]["result"] == "remembered result"
    assert payload["questions"][0]["text"] == "Choose a mode"
    assert payload["questions"][0]["options"] == ["A", "B"]
    assert "start" not in payload["questions"][0]
    assert "end" not in payload["questions"][0]

def test_payload_excludes_workstream_classifier_tasks() -> None:
    """Classifier tasks are machinery, not user work, and must stay out of the
    history the UI renders (#2586).

    This guards a REBASE hazard as much as a behaviour: the filter landed on
    main while this extraction was in review, inside the very block the
    extraction deletes. Resolving that conflict in favour of the extracted call
    — the obviously-correct-looking resolution — silently drops it, and nothing
    else in this suite would notice.
    """
    import task_workstreams

    original = (api.TASK_DIR, api.RESULT_DIR)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        api.TASK_DIR = root / "tasks"
        api.RESULT_DIR = root / "results"
        api.TASK_DIR.mkdir()
        api.RESULT_DIR.mkdir()
        api.task_history.clear()
        for name in (
            f"{task_workstreams.CLASSIFIER_TASK_PREFIX}abc",
            f"{task_workstreams.LEGACY_CLASSIFIER_TASK_PREFIX}def",
            "task-real-work",
        ):
            (api.TASK_DIR / f"{name}.txt").write_text("source: discord\ntask: t\n")
        try:
            ids = {row["id"] for row in api._active_task_rows()}
        finally:
            api.TASK_DIR, api.RESULT_DIR = original
            api.task_history.clear()
    assert "task-real-work" in ids, ids
    leaked = {
        i
        for i in ids
        if i.startswith(
            (
                task_workstreams.CLASSIFIER_TASK_PREFIX,
                task_workstreams.LEGACY_CLASSIFIER_TASK_PREFIX,
            )
        )
    }
    assert not leaked, f"classifier tasks leaked into /tasks/active: {leaked}"


def test_rows_reconcile_result_after_result_scan() -> None:
    """Cover the final reconciliation pass independently of result discovery."""
    original = (api.TASK_DIR, api.RESULT_DIR)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        api.TASK_DIR = root / "tasks"
        api.RESULT_DIR = root / "results"
        api.TASK_DIR.mkdir()
        api.RESULT_DIR.mkdir()
        api.task_history.clear()
        api.task_history["task-reconcile"] = {
            "status": "working",
            "text": "reconcile me",
            "time": 1000,
            "result": "",
            "source": "api",
        }
        (api.RESULT_DIR / "task-reconcile.txt").write_text("finished later\n")

        try:
            # Isolate the final pass: the preceding result-file scan normally
            # promotes this row through _remember_done_result_file first.
            with mock.patch.object(api, "_remember_done_result_file"):
                rows = api._active_task_rows()
        finally:
            api.TASK_DIR, api.RESULT_DIR = original
            api.task_history.clear()

    row = next(row for row in rows if row["id"] == "task-reconcile")
    assert row["status"] == "done"
    assert row["result"] == "finished later"


if __name__ == "__main__":
    test_payload_reconciles_files_history_and_questions()
    test_payload_excludes_workstream_classifier_tasks()
    test_rows_reconcile_result_after_result_scan()
    print("agent-api active tasks payload tests passed")
