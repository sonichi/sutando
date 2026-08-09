#!/usr/bin/env python3
"""Behavioral coverage for durable per-workstream provider sessions."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import subprocess
import tempfile
import time
from collections import Counter
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
        assert worker.probe("claude", workspace, owner) == 0
        assert worker.probe("claude", workspace, team) == worker.UNHANDLED


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
        # Current Codex threads are UUIDv7. Keep the real provider shape here
        # so the worker cannot silently reject a successful live launch.
        thread_id = "019fcfd0-12bf-7d63-b4b0-d386f5966622"
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
            "#!/bin/sh\n"
            "case \" $* \" in *\" --probe \"*) exit 0;; esac\n"
            "printf 'poison handler stdout\\n'\nexit 1\n",
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
        assert (workspace / "state" / "task-event-handler-fallbacks" / "task-retry.txt").is_file()
        assert not (workspace / "state" / "task-event-handler-claims" / "task-retry.txt").exists()


def test_slow_handler_does_not_block_the_next_task_event() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        tasks = workspace / "tasks"
        results = workspace / "results"
        tasks.mkdir(parents=True)
        results.mkdir()
        (tasks / "task-a-slow.txt").write_text("task: slow isolated work\n")
        (tasks / "task-b-live.txt").write_text("task: live-core work\n")
        handler = _executable(
            root / "handler",
            "#!/bin/sh\n"
            "probe=0\ntask_file=\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    --probe) probe=1;;\n"
            "    --task-file) shift; task_file=$1;;\n"
            "  esac\n"
            "  shift\n"
            "done\n"
            "if [ \"$probe\" = 1 ]; then\n"
            "  case \"$task_file\" in *task-a-slow.txt) exit 0;; *) exit 3;; esac\n"
            "fi\n"
            "sleep 5\n",
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _executable(bin_dir / "fswatch", "#!/bin/sh\nsleep 5\n")
        process = subprocess.Popen(
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            started = time.monotonic()
            assert process.stdout is not None
            line = process.stdout.readline()
            elapsed = time.monotonic() - started
            assert line == "TASK_FILE: task-b-live.txt\n"
            assert elapsed < 1.0, f"second task event was blocked for {elapsed:.2f}s"
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=2)


def test_watcher_bounds_provider_backlog_and_drains_every_receipt_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        tasks = workspace / "tasks"
        results = workspace / "results"
        tasks.mkdir(parents=True)
        results.mkdir()
        isolated = [f"task-{letter}-isolated.txt" for letter in "abcd"]
        for name in [*isolated, "task-z-live.txt"]:
            (tasks / name).write_text(f"task: {name}\n")
        handler = _executable(
            root / "handler",
            """#!/usr/bin/env python3
import os
import pathlib
import sys
import time

args = sys.argv[1:]
task = pathlib.Path(args[args.index("--task-file") + 1]).name
if "--probe" in args:
    raise SystemExit(3 if task == "task-z-live.txt" else 0)

root = pathlib.Path(os.environ["HANDLER_STATE"])
lock = root / "lock"
while True:
    try:
        lock.mkdir()
        break
    except FileExistsError:
        time.sleep(0.005)
active_path = root / "active"
maximum_path = root / "maximum"
active = int(active_path.read_text()) + 1 if active_path.exists() else 1
maximum = int(maximum_path.read_text()) if maximum_path.exists() else 0
active_path.write_text(str(active))
maximum_path.write_text(str(max(active, maximum)))
with (root / "calls").open("a") as log:
    log.write(task + "\\n")
lock.rmdir()

deadline = time.monotonic() + 4
while not (root / "release").exists() and time.monotonic() < deadline:
    time.sleep(0.01)

while True:
    try:
        lock.mkdir()
        break
    except FileExistsError:
        time.sleep(0.005)
active = int(active_path.read_text()) - 1
active_path.write_text(str(active))
lock.rmdir()
""",
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _executable(bin_dir / "fswatch", "#!/bin/sh\nsleep 5\n")
        state = root / "handler-state"
        state.mkdir()
        process = subprocess.Popen(
            ["/bin/bash", str(REPO / "src" / "watch-tasks-stream.sh"), str(tasks)],
            env={
                **os.environ,
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "HANDLER_STATE": str(state),
                "SUTANDO_DEFAULT_WORKSPACE": str(workspace),
                "SUTANDO_TASK_EVENT_HANDLER": str(handler),
                "SUTANDO_CORE_RUNTIME": "claude",
                "SUTANDO_RESULTS_DIR": str(results),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            started = time.monotonic()
            assert process.stdout is not None
            assert process.stdout.readline() == "TASK_FILE: task-z-live.txt\n"
            assert time.monotonic() - started < 1.0
            assert int((state / "maximum").read_text()) <= 2

            (state / "release").touch()
            calls = []
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                calls = (state / "calls").read_text().splitlines()
                if len(calls) == len(isolated):
                    break
                time.sleep(0.01)
            assert sorted(calls) == sorted(isolated)
            assert int((state / "maximum").read_text()) <= 2
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=2)


def test_overlapping_watcher_preserves_live_claim_and_owner_shutdown_falls_back() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        tasks = workspace / "tasks"
        results = workspace / "results"
        tasks.mkdir(parents=True)
        results.mkdir()
        task = tasks / "task-overlap.txt"
        task.write_text("task: one provider owner only\n")
        calls = root / "calls"
        handler = _executable(
            root / "handler",
            "#!/bin/sh\n"
            "probe=0\n"
            "while [ $# -gt 0 ]; do\n"
            "  [ \"$1\" = --probe ] && probe=1\n"
            "  shift\n"
            "done\n"
            "[ \"$probe\" = 1 ] && exit 0\n"
            "printf 'provider\\n' >> \"$HANDLER_CALLS\"\n"
            "sleep 10\n",
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _executable(bin_dir / "fswatch", "#!/bin/sh\nsleep 10\n")
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HANDLER_CALLS": str(calls),
            "SUTANDO_DEFAULT_WORKSPACE": str(workspace),
            "SUTANDO_TASK_EVENT_HANDLER": str(handler),
            "SUTANDO_CORE_RUNTIME": "claude",
            "SUTANDO_RESULTS_DIR": str(results),
        }

        def start_watcher():
            return subprocess.Popen(
                ["/bin/bash", str(REPO / "src" / "watch-tasks-stream.sh"), str(tasks)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

        owner = start_watcher()
        overlap = None
        try:
            claim = workspace / "state" / "task-event-handler-claims" / task.name
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if claim.is_file() and calls.exists():
                    break
                time.sleep(0.01)
            assert claim.is_file()
            assert calls.read_text().splitlines() == ["provider"]

            overlap = start_watcher()
            time.sleep(0.3)
            assert claim.is_file(), "overlap must preserve the live owner's atomic claim"
            assert calls.read_text().splitlines() == ["provider"]

            os.killpg(overlap.pid, signal.SIGTERM)
            overlap.communicate(timeout=2)
            overlap = None
            assert claim.is_file(), "non-owner cleanup must not remove another watcher's claim"

            os.killpg(owner.pid, signal.SIGTERM)
            owner_stdout, _ = owner.communicate(timeout=2)
            owner_events = [
                line.removeprefix("TASK_FILE: ")
                for line in owner_stdout.splitlines()
                if line.startswith("TASK_FILE: ")
            ]
            assert 1 <= owner_events.count(task.name) <= 2
            assert not claim.exists()
            assert (
                workspace / "state" / "task-event-handler-fallbacks" / task.name
            ).is_file()
        finally:
            if overlap is not None and overlap.poll() is None:
                os.killpg(overlap.pid, signal.SIGKILL)
                overlap.communicate(timeout=2)
            if owner.poll() is None:
                os.killpg(owner.pid, signal.SIGKILL)
                owner.communicate(timeout=2)


def _assert_shutdown_falls_back_without_surviving_workers() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        tasks = workspace / "tasks"
        results = workspace / "results"
        tasks.mkdir(parents=True)
        results.mkdir()
        names = [f"task-shutdown-{index}.txt" for index in range(4)]
        for name in names:
            (tasks / name).write_text(f"task: {name}\n")
        calls = root / "calls"
        handler = _executable(
            root / "handler",
            "#!/bin/sh\n"
            "probe=0\ntask_file=\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in --probe) probe=1;; --task-file) shift; task_file=$1;; esac\n"
            "  shift\n"
            "done\n"
            "[ \"$probe\" = 1 ] && exit 0\n"
            "basename \"$task_file\" >> \"$HANDLER_CALLS\"\n"
            "sleep 10\n",
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _executable(bin_dir / "fswatch", "#!/bin/sh\nsleep 10\n")
        process = subprocess.Popen(
            ["/bin/bash", str(REPO / "src" / "watch-tasks-stream.sh"), str(tasks)],
            env={
                **os.environ,
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "HANDLER_CALLS": str(calls),
                "SUTANDO_DEFAULT_WORKSPACE": str(workspace),
                "SUTANDO_TASK_EVENT_HANDLER": str(handler),
                "SUTANDO_CORE_RUNTIME": "claude",
                "SUTANDO_RESULTS_DIR": str(results),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        watcher_pgid = process.pid
        try:
            claims = workspace / "state" / "task-event-handler-claims"
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and len(list(claims.glob("task-*.txt"))) < 4:
                time.sleep(0.01)
            assert sorted(path.name for path in claims.glob("task-*.txt")) == names

            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=2)
            process = None
            remaining_claims = sorted(path.name for path in claims.glob("task-*.txt"))
            fallbacks = workspace / "state" / "task-event-handler-fallbacks"
            fallback_names = sorted(path.name for path in fallbacks.glob("task-*.txt"))
            events = Counter(
                line.removeprefix("TASK_FILE: ")
                for line in stdout.splitlines()
                if line.startswith("TASK_FILE: ")
            )
            assert set(events) == set(names), (
                repr(stdout), repr(stderr), remaining_claims, fallback_names
            )
            assert all(1 <= events[name] <= 2 for name in names), events
            assert not remaining_claims
            assert fallback_names == names
            assert len(calls.read_text().splitlines()) <= 2
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    os.killpg(watcher_pgid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError(
                    f"watcher process group {watcher_pgid} still has a live worker"
                )
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=2)


def test_shutdown_falls_back_without_surviving_workers() -> None:
    for _ in range(10):
        _assert_shutdown_falls_back_without_surviving_workers()


def test_codex_notifier_dispatches_each_isolated_task_once_without_waiting() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        tasks = workspace / "tasks"
        results = workspace / "results"
        state = workspace / "state"
        tasks.mkdir(parents=True)
        results.mkdir()
        state.mkdir()
        for name in ("task-one.txt", "task-two.txt"):
            (tasks / name).write_text(f"priority: normal\ntask: {name}\n")
        log = root / "handler.log"
        handler = _executable(
            root / "handler",
            "#!/bin/sh\n"
            "probe=0\ntask_file=\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in --probe) probe=1;; --task-file) shift; task_file=$1;; esac\n"
            "  shift\n"
            "done\n"
            "[ \"$probe\" = 1 ] && exit 0\n"
            "basename \"$task_file\" >> \"$HANDLER_LOG\"\n"
            "sleep 5\n",
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _executable(bin_dir / "fswatch", "#!/bin/sh\nsleep 5\n")
        _executable(bin_dir / "tmux", "#!/bin/sh\nexit 0\n")
        process = subprocess.Popen(
            ["/bin/bash", str(REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh")],
            env={
                **os.environ,
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "HANDLER_LOG": str(log),
                "SUTANDO_DEFAULT_WORKSPACE": str(workspace),
                "SUTANDO_TASKS_DIR": str(tasks),
                "SUTANDO_RESULTS_DIR": str(results),
                "SUTANDO_TASK_EVENT_HANDLER": str(handler),
                "SUTANDO_NOTIFIER_POLL_INTERVAL": "0.02",
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            started = time.monotonic()
            calls = []
            while time.monotonic() - started < 1.0:
                calls = log.read_text().splitlines() if log.exists() else []
                if len(calls) == 2:
                    break
                time.sleep(0.01)
            assert sorted(calls) == ["task-one.txt", "task-two.txt"]
            assert time.monotonic() - started < 1.0
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=2)


def test_codex_notifier_never_submits_a_watcher_claim_to_live_core() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        tasks = workspace / "tasks"
        results = workspace / "results"
        state = workspace / "state"
        tasks.mkdir(parents=True)
        results.mkdir()
        state.mkdir()
        (state / "core-status.json").write_text('{"status":"idle"}\n')
        # The unhandled file sorts first in the watcher sweep, while the
        # not-yet-claimed isolated file has higher queue priority. This closes
        # the event-before-claim race, not only the easy claim-first ordering.
        (tasks / "task-a-live.txt").write_text("priority: normal\ntask: live\n")
        (tasks / "task-z-isolated.txt").write_text("priority: urgent\ntask: isolated\n")
        handler_log = root / "handler.log"
        handler = _executable(
            root / "handler",
            "#!/bin/sh\n"
            "probe=0\ntask_file=\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in --probe) probe=1;; --task-file) shift; task_file=$1;; esac\n"
            "  shift\n"
            "done\n"
            "if [ \"$probe\" = 1 ]; then\n"
            "  case \"$task_file\" in *task-z-isolated.txt) exit 0;; *) exit 3;; esac\n"
            "fi\n"
            "basename \"$task_file\" >> \"$HANDLER_LOG\"\n"
            "sleep 5\n",
        )
        tmux_log = root / "tmux.log"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _executable(bin_dir / "fswatch", "#!/bin/sh\nsleep 5\n")
        _executable(
            bin_dir / "tmux",
            "#!/bin/sh\n"
            "case \" $* \" in *\" capture-pane \"*) exit 0;; esac\n"
            "printf '%s\\n' \"$*\" >> \"$TMUX_LOG\"\n"
            "exit 0\n",
        )
        process = subprocess.Popen(
            ["/bin/bash", str(REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh")],
            env={
                **os.environ,
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "HANDLER_LOG": str(handler_log),
                "TMUX_LOG": str(tmux_log),
                "SUTANDO_DEFAULT_WORKSPACE": str(workspace),
                "SUTANDO_TASKS_DIR": str(tasks),
                "SUTANDO_RESULTS_DIR": str(results),
                "SUTANDO_TASK_EVENT_HANDLER": str(handler),
                "SUTANDO_NOTIFIER_POLL_INTERVAL": "0.02",
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 1.5
            tmux_calls = ""
            while time.monotonic() < deadline:
                tmux_calls = tmux_log.read_text() if tmux_log.exists() else ""
                if "task-a-live.txt" in tmux_calls:
                    break
                time.sleep(0.01)
            assert "task-a-live.txt" in tmux_calls
            assert "task-z-isolated.txt" not in tmux_calls
            deadline = time.monotonic() + 1
            while not handler_log.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert handler_log.read_text().splitlines() == ["task-z-isolated.txt"]
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=2)


def test_runtime_wiring_is_optional_and_adapter_injected() -> None:
    watcher = (REPO / "src" / "watch-tasks-stream.sh").read_text()
    notifier = (REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh").read_text()
    claude = (REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh").read_text()
    codex = (REPO / "src" / "agent" / "codex" / "cli" / "start-cli.sh").read_text()
    assert '${SUTANDO_TASK_EVENT_HANDLER:-}' in watcher
    assert "--probe" in watcher
    assert 'printf \'TASK_FILE: %s\\n\'' in watcher
    assert "TASK_HANDLER_WORKERS=2" in watcher
    assert "probe_optional_task_handler" in notifier
    assert 'os.environ.pop("SUTANDO_TASK_EVENT_HANDLER"' not in notifier
    assert "TASK_HANDLER_CLAIMS_DIR" in notifier
    assert "TASK_HANDLER_FALLBACKS_DIR" in notifier
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
    test_slow_handler_does_not_block_the_next_task_event()
    test_watcher_bounds_provider_backlog_and_drains_every_receipt_once()
    test_overlapping_watcher_preserves_live_claim_and_owner_shutdown_falls_back()
    test_shutdown_falls_back_without_surviving_workers()
    test_codex_notifier_dispatches_each_isolated_task_once_without_waiting()
    test_codex_notifier_never_submits_a_watcher_claim_to_live_core()
    test_runtime_wiring_is_optional_and_adapter_injected()
    print("task workstream session worker tests passed")
