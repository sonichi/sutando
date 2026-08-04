#!/usr/bin/env python3
"""Behavioral coverage for archive-backed inferred task workstreams."""

from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import task_workstreams as workstreams  # noqa: E402


def inherit_worker(workspace: str, child_id: str, start) -> None:
    start.wait()
    workstreams.inherit_assignment(Path(workspace), child_id, "task-a1")


def write_task(
    path: Path,
    task_id: str,
    timestamp: str,
    text: str,
    *,
    tier: str = "owner",
    source: str = "discord",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"id: {task_id}\n"
        f"timestamp: {timestamp}\n"
        f"source: {source}\n"
        f"access_tier: {tier}\n"
        f"task: {text}\n"
    )


def write_result(workspace: Path, task_id: str, text: str = "done") -> None:
    path = workspace / "results" / "archive" / "2026-08" / f"{task_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fixture_workspace() -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="sutando-task-workstreams-"))
    write_task(
        workspace / "tasks" / "archive" / "2026-08" / "task-a1.txt",
        "task-a1", "2026-08-03T10:00:00Z", "implement task grouping",
    )
    write_task(
        workspace / "tasks" / "archive" / "2026-08" / "task-b1.txt",
        "task-b1", "2026-08-03T10:01:00Z", "monitor the trading bot",
    )
    write_task(
        workspace / "tasks" / "archive" / "2026-08" / "task-a2.txt",
        "task-a2", "2026-08-03T10:02:00Z", "show workstreams in the web ui",
    )
    write_task(
        workspace / "tasks" / "archive" / "2026-08" / "task-team.txt",
        "task-team", "2026-08-03T10:03:00Z", "untrusted collaborator request", tier="team",
    )
    for task_id in ("task-a1", "task-b1", "task-a2", "task-team"):
        write_result(workspace, task_id)
    (workspace / "state").mkdir(parents=True, exist_ok=True)
    (workspace / "state" / "core-status.json").write_text('{"status":"idle"}\n')
    return workspace


def test_history_uses_invocation_time_and_owner_candidates() -> None:
    workspace = fixture_workspace()
    rows = workstreams.scan_task_history(workspace)
    assert [row.id for row in rows] == ["task-team", "task-a2", "task-b1", "task-a1"]
    snapshot = workstreams.build_classifier_snapshot(workspace)
    assert [row["id"] for row in snapshot["tasks"]] == ["task-a1", "task-b1", "task-a2"]
    assert "task-team" not in json.dumps(snapshot)


def test_loader_parser_and_history_fail_open_edges() -> None:
    workspace = fixture_workspace()
    store_path = workspace / "data" / "task-workstreams.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({
        "schema_version": 1,
        "workstreams": [],
        "assignments": {},
    }))
    assert workstreams.load_workstream_store(workspace)["workstreams"] == {}

    store_path.write_text(json.dumps({
        "schema_version": 1,
        "workstreams": {},
        "assignments": {},
        "reviews": [],
    }))
    assert workstreams.load_workstream_store(workspace)["reviews"] == {}
    assert workstreams._task_text("id: task-empty\n") == ""
    assert workstreams._parse_timestamp("", 12.5) == 12.5
    assert workstreams._parse_timestamp("2026-08-03T10:00:00", 0) > 0
    assert workstreams._parse_timestamp("not-a-time", 12.5) == 12.5

    duplicate = workspace / "tasks" / "task-a1.txt"
    write_task(duplicate, "task-a1", "2026-08-03T11:00:00Z", "live duplicate")
    assert [path.stem for path in workstreams._task_paths(workspace / "tasks")].count("task-a1") == 1

    no_text = workspace / "tasks" / "task-no-text.txt"
    no_text.write_text("id: task-no-text\nsource: discord\n")
    write_task(
        workspace / "tasks" / "task-old-classifier.txt",
        "task-old-classifier",
        "2026-08-03T12:00:00Z",
        "internal maintenance",
        source="task-project-grouping",
    )
    ids = {row.id for row in workstreams.scan_task_history(workspace)}
    assert "task-no-text" not in ids
    assert "task-old-classifier" not in ids


def test_apply_is_validated_stable_sticky_and_fail_open() -> None:
    workspace = fixture_workspace()
    store_path = workspace / "data" / "task-workstreams.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text("not json")
    assert workstreams.load_workstream_store(workspace)["workstreams"] == {}

    snapshot = workstreams.build_classifier_snapshot(workspace)
    try:
        workstreams.apply_inference(workspace, {
            "snapshot_hash": "stale",
            "workstreams": [],
        })
        raise AssertionError("a stale snapshot should be rejected")
    except ValueError as exc:
        assert "snapshot_hash" in str(exc)
    try:
        workstreams.apply_inference(workspace, {
            "snapshot_hash": snapshot["snapshot_hash"],
            "workstreams": {},
        })
        raise AssertionError("non-list workstreams should be rejected")
    except ValueError as exc:
        assert str(exc) == "workstreams must be a list"

    sutando_workstream_id = workstreams._workstream_id("Sutando task management")
    proposal = {
        "snapshot_hash": snapshot["snapshot_hash"],
        "workstreams": [
            None,
            {
                "name": "Invalid confidence",
                "confidence": "not-a-number",
                "task_ids": ["task-b1"],
            },
            {
                "name": "Sutando task management",
                "summary": "group and display related tasks",
                "confidence": 0.94,
                "task_ids": ["task-a1"],
            },
            {
                "workstream_id": sutando_workstream_id,
                "name": "Sutando task management",
                "confidence": 0.94,
                "task_ids": ["task-a2"],
            },
            {
                "name": "Trading bot",
                "confidence": 0.4,
                "task_ids": ["task-b1"],
            },
            {
                "name": "Phantom workstream",
                "confidence": 0.99,
                "task_ids": ["task-does-not-exist"],
            },
        ],
    }
    result = workstreams.apply_inference(workspace, proposal)
    assert result.assigned == 2 and result.workstreams_created == 1
    store = workstreams.load_workstream_store(workspace)
    a1 = store["assignments"]["task-a1"]
    a2 = store["assignments"]["task-a2"]
    assert a1["workstream_id"] == a2["workstream_id"]
    assert "task-b1" not in store["assignments"]
    assert store["reviews"]["task-b1"]["origin"] == "classifier-omitted"
    assert a1["workstream_id"].startswith("workstream-sutando-task-management-")
    assert workstreams.build_classifier_snapshot(workspace)["tasks"] == []

    # Existing assignments stay sticky and explicit follow-ups inherit without an LLM call.
    assert workstreams.inherit_assignment(workspace, "task-followup", "task-a1")
    assert workstreams.inherit_assignment(workspace, "task-followup", "task-a1")
    assert not workstreams.inherit_assignment(workspace, "task-orphan", "task-missing")
    assert workstreams.load_workstream_store(workspace)["assignments"]["task-followup"]["workstream_id"] == a1["workstream_id"]

    payload = workstreams.task_history_payload(workspace)
    task = next(row for row in payload["tasks"] if row["id"] == "task-a1")
    assert task["workstream_name"] == "Sutando task management"


def test_legacy_project_sidecar_migrates_on_the_next_write() -> None:
    workspace = fixture_workspace()
    legacy_path = workspace / "data" / "task-projects.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps({
        "schema_version": 1,
        "projects": {
            "project-sutando": {
                "title": "Sutando task management",
                "summary": "group related task history",
            },
        },
        "assignments": {
            "task-a1": {"project_id": "project-sutando", "origin": "inferred"},
            "malformed": "not-an-assignment",
        },
        "reviews": {},
    }))

    store = workstreams.load_workstream_store(workspace)
    assert store["workstreams"]["project-sutando"]["title"] == "Sutando task management"
    assert store["assignments"]["task-a1"]["workstream_id"] == "project-sutando"
    assert "project_id" not in store["assignments"]["task-a1"]

    snapshot = workstreams.build_classifier_snapshot(workspace)
    workstreams.apply_inference(workspace, {
        "snapshot_hash": snapshot["snapshot_hash"],
        "workstreams": [],
    })
    migrated = json.loads((workspace / "data" / "task-workstreams.json").read_text())
    assert "workstreams" in migrated and "projects" not in migrated
    assert migrated["assignments"]["task-a1"]["workstream_id"] == "project-sutando"


def test_classifier_enqueue_is_idle_gated_deduped_and_non_mutating() -> None:
    workspace = fixture_workspace()
    unavailable = workstreams.maybe_enqueue_classifier_task(
        workspace, skill_file=workspace / "missing-skill.md"
    )
    assert not unavailable.pending and unavailable.reason == "skill-unavailable"
    original = workspace / "tasks" / "archive" / "2026-08" / "task-a1.txt"
    before = original.read_bytes()
    first = workstreams.maybe_enqueue_classifier_task(workspace)
    assert first.pending and first.enqueued and first.reason == "enqueued"
    task_file = workspace / "tasks" / f"{first.task_id}.txt"
    assert task_file.exists()
    assert "$task-workstream-grouping" in task_file.read_text()
    assert original.read_bytes() == before

    with mock.patch.object(
        workstreams,
        "scan_task_history",
        side_effect=AssertionError("unchanged inflight state must not scan history"),
    ):
        second = workstreams.maybe_enqueue_classifier_task(workspace)
    assert second.pending and not second.enqueued and second.reason == "already-queued"
    workstreams.mark_classifier_complete(workspace, first.snapshot_hash)
    with mock.patch.object(
        workstreams,
        "scan_task_history",
        side_effect=AssertionError("unchanged complete state must not scan history"),
    ):
        settled = workstreams.maybe_enqueue_classifier_task(workspace)
    assert not settled.pending and not settled.enqueued and settled.reason == "complete"

    write_task(
        workspace / "tasks" / "archive" / "2026-08" / "task-new.txt",
        "task-new",
        "2026-08-03T13:00:00Z",
        "a newly archived task",
    )
    with mock.patch.object(
        workstreams,
        "scan_task_history",
        wraps=workstreams.scan_task_history,
    ) as changed_scan:
        changed = workstreams.classifier_status(workspace)
    assert changed.reason == "ready"
    assert changed_scan.call_count == 1

    real_scan = workstreams.scan_task_history

    def scan_while_another_task_arrives(target: Path):
        rows = real_scan(target)
        write_task(
            target / "tasks" / "archive" / "2026-08" / "task-raced.txt",
            "task-raced",
            "2026-08-03T13:01:00Z",
            "arrived during the classifier scan",
        )
        write_result(target, "task-raced")
        return rows

    with mock.patch.object(
        workstreams,
        "scan_task_history",
        side_effect=scan_while_another_task_arrives,
    ):
        raced = workstreams.classifier_status(workspace)
    assert raced.pending and not raced.enqueued and raced.reason == "source-changed"
    with mock.patch.object(
        workstreams,
        "scan_task_history",
        wraps=real_scan,
    ) as retry_scan:
        retried = workstreams.classifier_status(workspace)
    assert retried.reason == "ready"
    assert retry_scan.call_count == 1

    manual = fixture_workspace()
    manual_snapshot = workstreams.build_classifier_snapshot(manual)
    workstreams.apply_inference(manual, {
        "snapshot_hash": manual_snapshot["snapshot_hash"],
        "workstreams": [],
    })
    reviewed = workstreams.classifier_status(manual)
    assert not reviewed.pending and reviewed.reason == "complete"

    other = fixture_workspace()
    (other / "state" / "core-status.json").write_text('{"status":"running"}\n')
    with mock.patch.object(
        workstreams,
        "build_classifier_snapshot",
        side_effect=AssertionError("busy maintenance must not scan task history"),
    ):
        blocked = workstreams.maybe_enqueue_classifier_task(other)
    assert blocked.pending and not blocked.enqueued and blocked.reason == "core-busy"
    assert not list((other / "tasks").glob("task-workstream-grouping-*.txt"))

    invalid_age = fixture_workspace()
    invalid_snapshot = workstreams.build_classifier_snapshot(invalid_age)
    invalid_source_token, invalid_source_directories = workstreams._task_source_state(
        invalid_age, {}, discover=True,
    )
    (invalid_age / "state" / "task-workstream-classifier.json").write_text(json.dumps({
        "snapshot_hash": invalid_snapshot["snapshot_hash"],
        "status": "inflight",
        "enqueued_at": "not-a-number",
        "source_token": invalid_source_token,
        "source_directories": list(invalid_source_directories),
    }))
    assert workstreams.classifier_status(invalid_age).reason == "ready"

    active = fixture_workspace()
    write_task(
        active / "tasks" / "task-active.txt",
        "task-active",
        "2026-08-03T12:00:00Z",
        "continue active work",
    )
    assert workstreams.classifier_status(active).reason == "active-user-task"


def test_classifier_source_directory_cache_rejects_unsafe_entries_fail_open() -> None:
    missing = Path(tempfile.mkdtemp()) / "missing-workspace"
    directories = workstreams._source_directories(
        missing,
        {
            "source_directories": [
                None,
                "../outside",
                "results/archive-2026-08-03",
            ],
        },
        discover=True,
    )
    assert "../outside" not in directories
    assert "results/archive-2026-08-03" in directories


def test_stale_classifier_is_archived_before_replacement() -> None:
    workspace = fixture_workspace()
    first = workstreams.maybe_enqueue_classifier_task(workspace)
    first_path = workspace / "tasks" / f"{first.task_id}.txt"
    state_path = workspace / "state" / "task-workstream-classifier.json"
    state = json.loads(state_path.read_text())
    state["enqueued_at"] = 0
    state_path.write_text(json.dumps(state))

    replacement = workstreams.maybe_enqueue_classifier_task(workspace)

    assert replacement.enqueued and replacement.task_id != first.task_id
    assert not first_path.exists()
    archived = list((workspace / "tasks" / "archive").glob(f"*/{first.task_id}*.txt"))
    assert len(archived) == 1
    live = list((workspace / "tasks").glob("task-workstream-grouping-*.txt"))
    assert live == [workspace / "tasks" / f"{replacement.task_id}.txt"]


def test_classifier_maintenance_runs_without_a_dashboard_and_survives_errors() -> None:
    workspace = fixture_workspace()
    stop = threading.Event()
    calls = []

    def probe(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise RuntimeError("transient classifier failure")
        stop.set()

    with (
        mock.patch.object(workstreams, "maybe_enqueue_classifier_task", side_effect=probe),
        mock.patch.object(workstreams.LOGGER, "warning") as warning,
    ):
        workstreams.run_classifier_maintenance(
            workspace,
            skill_file=REPO / "skills" / "task-workstream-grouping" / "SKILL.md",
            stop_event=stop,
            interval_seconds=0.01,
        )

    assert len(calls) == 2
    assert calls[0][0] == (workspace,)
    assert calls[0][1]["skill_file"].name == "SKILL.md"
    warning.assert_called_once_with(
        "task workstream classifier maintenance failed: %s",
        mock.ANY,
    )


def test_concurrent_inheritance_keeps_every_assignment() -> None:
    workspace = fixture_workspace()
    snapshot = workstreams.build_classifier_snapshot(workspace)
    workstreams.apply_inference(workspace, {
        "snapshot_hash": snapshot["snapshot_hash"],
        "workstreams": [{
            "name": "Sutando task management",
            "confidence": 0.95,
            "task_ids": ["task-a1"],
        }],
    })
    context = multiprocessing.get_context("fork")
    start = context.Event()
    children = [f"task-child-{index}" for index in range(8)]
    processes = [
        context.Process(target=inherit_worker, args=(str(workspace), child, start))
        for child in children
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assignments = workstreams.load_workstream_store(workspace)["assignments"]
    assert all(child in assignments for child in children)


def main() -> None:
    tests = [
        test_history_uses_invocation_time_and_owner_candidates,
        test_loader_parser_and_history_fail_open_edges,
        test_apply_is_validated_stable_sticky_and_fail_open,
        test_legacy_project_sidecar_migrates_on_the_next_write,
        test_classifier_enqueue_is_idle_gated_deduped_and_non_mutating,
        test_classifier_source_directory_cache_rejects_unsafe_entries_fail_open,
        test_stale_classifier_is_archived_before_replacement,
        test_classifier_maintenance_runs_without_a_dashboard_and_survives_errors,
        test_concurrent_inheritance_keeps_every_assignment,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print("task workstream tests passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise
