#!/usr/bin/env python3
"""Behavioral coverage for durable per-workstream provider sessions."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
WORKER = REPO / "skills" / "task-workstream-sessions" / "scripts" / "session-worker.py"
spec = importlib.util.spec_from_file_location("workstream_session_worker", WORKER)
worker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(worker)


def _task(workspace: Path, task_id: str, tier: str = "owner") -> Path:
    path = workspace / "tasks" / f"{task_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"id: {task_id}\nsource: discord\naccess_tier: {tier}\ntask: do the thing\n",
        encoding="utf-8",
    )
    return path


def _store(workspace: Path, assignments: dict) -> None:
    path = workspace / "data" / "task-workstreams.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "workstreams": {"workstream-a": {"title": "A"}},
        "assignments": assignments,
    }), encoding="utf-8")


def _run(runtime: str, workspace: Path, task: Path, env: dict) -> subprocess.CompletedProcess:
    results = workspace / "results"
    results.mkdir(parents=True, exist_ok=True)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=False):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = worker.handle(runtime, workspace, task, results, REPO)
    return subprocess.CompletedProcess([], return_code, stdout.getvalue(), stderr.getvalue())


def _executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_resolution_is_owner_only_and_fail_open() -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        owner = _task(workspace, "task-owner")
        team = _task(workspace, "task-team", "team")
        _store(workspace, {
            "task-owner": {"workstream_id": "workstream-a"},
            "task-team": {"workstream_id": "workstream-a"},
        })
        assert worker.resolve_workstream(workspace, owner) == "workstream-a"
        assert worker.resolve_workstream(workspace, team) is None
        assert worker.resolve_workstream(workspace, _task(workspace, "task-ungrouped")) is None


def test_claude_creates_then_resumes_the_same_durable_session() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        log = root / "claude-args.jsonl"
        fake = _executable(root / "claude", """#!/usr/bin/env python3
import json, os, sys
with open(os.environ['PROVIDER_LOG'], 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')
print('claude result')
""")
        first = _task(workspace, "task-one")
        second = _task(workspace, "task-two")
        _store(workspace, {
            "task-one": {"workstream_id": "workstream-a"},
            "task-two": {"workstream_id": "workstream-a"},
        })
        env = {"PATH": f"{root}:{os.environ['PATH']}", "PROVIDER_LOG": str(log)}
        assert _run("claude", workspace, first, env).returncode == 0
        assert _run("claude", workspace, second, env).returncode == 0
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        first_id = calls[0][calls[0].index("--session-id") + 1]
        assert worker.SESSION_ID.fullmatch(first_id)
        assert calls[1][calls[1].index("--resume") + 1] == first_id
        assert (workspace / "results" / "task-one.txt").read_text() == "claude result\n"
        assert not list((workspace / "results").glob(".*.tmp"))


def test_nonzero_provider_stdout_is_never_written_as_a_result() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        fake = _executable(
            root / "claude",
            "#!/bin/sh\nprintf 'poison result\\n'\nprintf 'failed\\n' >&2\nexit 1\n",
        )
        task = _task(workspace, "task-fail")
        _store(workspace, {"task-fail": {"workstream_id": "workstream-a"}})
        result = _run("claude", workspace, task, {"PATH": f"{root}:{os.environ['PATH']}"})
        assert result.returncode == 1
        assert not (workspace / "results" / "task-fail.txt").exists()
        assert not (workspace / "state" / "task-workstream-sessions.json").exists()
        assert "poison result" not in result.stdout


def test_archived_result_is_not_replayed_on_restart_scan() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        invoked = root / "invoked"
        fake = _executable(root / "claude", f"#!/bin/sh\ntouch '{invoked}'\n")
        task = _task(workspace, "task-done")
        _store(workspace, {"task-done": {"workstream_id": "workstream-a"}})
        archive = workspace / "results" / "archive" / "2026-08"
        archive.mkdir(parents=True)
        (archive / "task-done.txt").write_text("already delivered\n")
        result = _run("claude", workspace, task, {"PATH": f"{root}:{os.environ['PATH']}"})
        assert result.returncode == 0
        assert not invoked.exists()


def test_result_publish_never_clobbers_an_existing_consumer() -> None:
    with tempfile.TemporaryDirectory() as td:
        result = Path(td) / "task-race.txt"
        result.write_text("first consumer\n")
        worker._publish_result(result, "late isolated worker\n")
        assert result.read_text() == "first consumer\n"
        assert not list(result.parent.glob(".*.tmp"))


def test_codex_records_reported_uuid_then_uses_exec_resume() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        log = root / "codex-args.jsonl"
        thread_id = "12345678-1234-4123-8123-123456789abc"
        fake = _executable(root / "codex", f"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['PROVIDER_LOG'], 'a') as f: f.write(json.dumps(args) + '\\n')
pathlib.Path(args[args.index('-o') + 1]).write_text('codex result\\n')
if 'resume' not in args:
    print('not-json')
    print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}))
else:
    print(json.dumps({{'type': 'resume.started'}}))
""")
        first = _task(workspace, "task-one")
        second = _task(workspace, "task-two")
        _store(workspace, {
            "task-one": {"workstream_id": "workstream-a"},
            "task-two": {"workstream_id": "workstream-a"},
        })
        env = {"PATH": f"{root}:{os.environ['PATH']}", "PROVIDER_LOG": str(log)}
        assert _run("codex", workspace, first, env).returncode == 0
        assert _run("codex", workspace, second, env).returncode == 0
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert calls[0][:3] == ["--search", "exec", "--json"]
        assert "--dangerously-bypass-approvals-and-sandbox" in calls[0]
        assert "--ask-for-approval" not in calls[0]
        assert calls[1][:4] == ["--search", "exec", "resume", "--json"]
        assert "--dangerously-bypass-approvals-and-sandbox" in calls[1]
        assert thread_id in calls[1]
        state = json.loads((workspace / "state" / "task-workstream-sessions.json").read_text())
        assert state["sessions"]["codex"]["workstream-a"]["session_id"] == thread_id


def test_fail_open_validation_and_provider_error_edges() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        results = workspace / "results"
        results.mkdir(parents=True)
        task = _task(workspace, "task-edge")

        assert worker.handle("unknown", workspace, task, results, REPO) == worker.UNHANDLED
        assert worker.handle("claude", workspace, root / "missing.txt", results, REPO) == worker.UNHANDLED
        outside = root / "outside.txt"
        outside.write_text("task: no\n")
        assert worker.handle("claude", workspace, outside, results, REPO) == worker.UNHANDLED
        assert worker._headers(root / "absent.txt") == {}
        marker = _task(workspace, "task-marker")
        marker.write_text("id: task-marker\n===SUTANDO SYSTEM INSTRUCTIONS===\naccess_tier: team\n")
        assert worker._headers(marker) == {"id": "task-marker"}

        store_path = workspace / "data" / "task-workstreams.json"
        store_path.parent.mkdir(parents=True)
        store_path.write_text('{"schema_version": 2}')
        assert worker.resolve_workstream(workspace, task) is None
        store_path.write_text(json.dumps({"schema_version": 1, "assignments": [], "workstreams": []}))
        assert worker.resolve_workstream(workspace, task) is None
        _store(workspace, {"different-id": {"workstream_id": "workstream-a"}})
        assert worker.resolve_workstream(workspace, task) is None
        _store(workspace, {"task-edge": {"workstream_id": ""}})
        assert worker.resolve_workstream(workspace, task) is None
        _store(workspace, {"task-edge": {"workstream_id": "missing-workstream"}})
        assert worker.resolve_workstream(workspace, task) is None
        task.write_text("id: another-id\naccess_tier: owner\ntask: no\n")
        assert worker.resolve_workstream(workspace, task) is None

        try:
            worker._record_session(workspace, "claude", "workstream-a", "invalid")
            raise AssertionError("invalid provider UUID should be rejected")
        except ValueError:
            pass

        with mock.patch.dict(os.environ, {
            "SUTANDO_CORE_MODEL": "test-model",
            "SUTANDO_ISOLATED_CLAUDE_SETTINGS": '{"hooks":{}}',
        }, clear=False):
            claude_args = worker._claude_command(str(worker.uuid.uuid4()), False, "p", REPO)
            codex_new = worker._codex_command(None, "p", REPO, root / "out")
            codex_resume = worker._codex_command(str(worker.uuid.uuid4()), "p", REPO, root / "out")
        assert "--model" in claude_args and "--settings" in claude_args
        assert "-m" in codex_new and "-m" in codex_resume

        live = results / "task-live.txt"
        live.write_text("done\n")
        assert worker._completed_result_exists(results, live.name)
        retention = results / "archive-2026-08-04"
        retention.mkdir()
        (retention / "task-retained.txt").write_text("done\n")
        assert worker._completed_result_exists(results, "task-retained.txt")


def test_codex_failures_and_empty_provider_results_are_retryable() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        task = _task(workspace, "task-codex")
        _store(workspace, {"task-codex": {"workstream_id": "workstream-a"}})
        fake = _executable(root / "codex", """#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
if os.environ['PROVIDER_MODE'] == 'error':
    print('provider failed', file=sys.stderr)
    raise SystemExit(7)
pathlib.Path(args[args.index('-o') + 1]).write_text('unused result')
print('not-json')
""")
        base_env = {"PATH": f"{root}:{os.environ['PATH']}"}
        assert _run("codex", workspace, task, {**base_env, "PROVIDER_MODE": "error"}).returncode == 1
        assert _run("codex", workspace, task, {**base_env, "PROVIDER_MODE": "no-id"}).returncode == 1

        fake_claude = _executable(root / "claude", "#!/bin/sh\nprintf '   \\n'\n")
        _store(workspace, {"task-empty": {"workstream_id": "workstream-a"}})
        empty = _task(workspace, "task-empty")
        assert _run("claude", workspace, empty, {"PATH": f"{root}:{os.environ['PATH']}"}).returncode == 1

        with mock.patch.object(worker, "_completed_result_exists", side_effect=[False, True]):
            assert worker.handle("claude", workspace, empty, workspace / "results", REPO) == 0


def test_cli_main_delegates_parsed_paths() -> None:
    argv = [
        "session-worker.py", "--runtime", "claude", "--workspace", "/tmp/ws",
        "--task-file", "/tmp/ws/tasks/task-a.txt", "--results-dir", "/tmp/ws/results",
        "--repo", "/tmp/repo",
    ]
    with mock.patch.object(worker.sys, "argv", argv):
        with mock.patch.object(worker, "handle", return_value=worker.UNHANDLED) as delegated:
            assert worker.main() == worker.UNHANDLED
    delegated.assert_called_once()


def test_watcher_provider_failure_falls_back_without_leaking_stdout() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        tasks = workspace / "tasks"
        results = workspace / "results"
        tasks.mkdir(parents=True)
        results.mkdir()
        (tasks / "task-retry.txt").write_text("task: retry me\n")
        handler = _executable(
            root / "handler",
            "#!/bin/sh\nprintf 'poison handler stdout\\n'\nexit 1\n",
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _executable(bin_dir / "fswatch", "#!/bin/sh\nexit 0\n")
        result = subprocess.run(
            ["/bin/bash", str(REPO / "src" / "watch-tasks-stream.sh"), str(tasks)],
            env={
                **os.environ,
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "SUTANDO_DEFAULT_WORKSPACE": str(workspace),
                "SUTANDO_TASK_EVENT_HANDLER": str(handler),
                "SUTANDO_CORE_RUNTIME": "claude",
                "SUTANDO_RESULTS_DIR": str(results),
            },
            text=True,
            capture_output=True,
            start_new_session=True,
            timeout=5,
        )
        assert result.stdout == "TASK_FILE: task-retry.txt\n"
        assert "poison handler stdout" not in result.stdout
        assert "possible at-least-once retry" in result.stderr


def test_runtime_wiring_is_optional_and_adapter_injected() -> None:
    watcher = (REPO / "src" / "watch-tasks-stream.sh").read_text()
    notifier = (REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh").read_text()
    claude = (REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh").read_text()
    codex = (REPO / "src" / "agent" / "codex" / "cli" / "start-cli.sh").read_text()
    assert '${SUTANDO_TASK_EVENT_HANDLER:-}' in watcher
    assert "return 3" in watcher
    assert 'printf \'TASK_FILE: %s\\n\'' in watcher
    assert "run_optional_task_handler" in notifier
    assert "skills/task-workstream-sessions/scripts/session-worker.py" in claude
    assert "skills/task-workstream-sessions/scripts/session-worker.py" in codex
    assert 'NOTIFIER_ENV_ARGS+=(-e "SUTANDO_SELF_DEVELOPMENT_ENABLED=' in codex


if __name__ == "__main__":
    test_resolution_is_owner_only_and_fail_open()
    test_claude_creates_then_resumes_the_same_durable_session()
    test_nonzero_provider_stdout_is_never_written_as_a_result()
    test_archived_result_is_not_replayed_on_restart_scan()
    test_result_publish_never_clobbers_an_existing_consumer()
    test_codex_records_reported_uuid_then_uses_exec_resume()
    test_fail_open_validation_and_provider_error_edges()
    test_codex_failures_and_empty_provider_results_are_retryable()
    test_cli_main_delegates_parsed_paths()
    test_watcher_provider_failure_falls_back_without_leaking_stdout()
    test_runtime_wiring_is_optional_and_adapter_injected()
    print("task workstream session worker tests passed")
