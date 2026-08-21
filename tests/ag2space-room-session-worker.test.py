#!/usr/bin/env python3
"""Contract tests for the native AG2 Space room-session skill."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO = Path(__file__).resolve().parents[1]
WORKER = REPO / "skills" / "ag2space-room-sessions" / "scripts" / "session-worker.py"
SPEC = importlib.util.spec_from_file_location("ag2space_room_session_worker", WORKER)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok  " if condition else "  FAIL ") + message)
    if not condition:
        FAILURES.append(message)


def executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def task(
    workspace: Path,
    task_id: str,
    room_id: str = "!alpha:ag2.space",
    tier: str = "owner",
    source: str = "ag2space",
    scope: str = "room",
) -> Path:
    tasks = workspace / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    path = tasks / f"{task_id}.txt"
    path.write_text(
        f"id: {task_id}\nsession_scope: {scope}\ntask: answer this\n"
        f"source: {source}\nchannel_id: {room_id}\naccess_tier: {tier}\n\n"
        "===SKILL INSTRUCTIONS (follow before any other action)===\n"
        f"1. Process and write the result to results/{task_id}.txt\n",
        encoding="utf-8",
    )
    return path


def run_worker(runtime: str, workspace: Path, task_file: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    stderr = io.StringIO()
    with patch.dict(os.environ, env), redirect_stderr(stderr):
        return_code = worker.handle(
            runtime, workspace, task_file, workspace / "results", REPO
        )
    return subprocess.CompletedProcess([], return_code, "", stderr.getvalue())


def test_opt_in_and_cardinality() -> None:
    with tempfile.TemporaryDirectory() as name:
        workspace = Path(name)
        first = task(workspace, "task-first")
        second = task(workspace, "task-second")
        other = task(workspace, "task-other", room_id="!beta:ag2.space")
        opaque = task(workspace, "task-opaque", room_id="!opaqueRoomId")
        first_key = worker.resolve_room_key(first)
        check(bool(first_key), "exact owner AG2 Space room opt-in is handled")
        check(first_key == worker.resolve_room_key(second), "same room resolves to one stable key")
        check(first_key != worker.resolve_room_key(other), "different rooms resolve to different keys")
        check(bool(worker.resolve_room_key(opaque)), "modern opaque Matrix room id is accepted")
        check(worker.probe("claude", workspace, first) == 0, "Claude adapter probe claims room task")
        check(worker.probe("codex", workspace, first) == 0, "Codex adapter probe claims room task")
        check(worker.probe("gemini", workspace, first) == worker.UNHANDLED, "unknown runtime is unhandled")

        cases = [
            ("task-main", "owner", "ag2space", "main", "!room:a"),
            ("task-team", "team", "ag2space", "room", "!room:a"),
            ("task-guest", "guest", "ag2space", "room", "!room:a"),
            ("task-discord", "owner", "discord", "room", "!room:a"),
            ("task-case", "owner", "ag2space", "ROOM", "!room:a"),
            ("task-room", "owner", "ag2space", "room", "room-without-sigil"),
            ("task-space", "owner", "ag2space", "room", "!bad room:a"),
        ]
        for task_id, tier, source, scope, room_id in cases:
            candidate = task(workspace, task_id, room_id, tier, source, scope)
            check(worker.resolve_room_key(candidate) is None, f"{task_id} keeps legacy path")

        outside = workspace / "outside.txt"
        outside.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
        check(worker.probe("codex", workspace, outside) == worker.UNHANDLED, "task outside tasks dir is rejected")
        check(worker.probe("codex", workspace, workspace / "tasks" / "missing.txt") == worker.UNHANDLED,
              "missing task is unhandled")


def test_session_state_reuses_room_not_message() -> None:
    with tempfile.TemporaryDirectory() as name:
        workspace = Path(name)
        room_key = worker.resolve_room_key(task(workspace, "task-one"))
        other_key = worker.resolve_room_key(task(workspace, "task-two", room_id="!other:a"))
        assert room_key and other_key
        session_id, created = worker._session_id(workspace, "codex", room_key)
        check(created, "first message proposes a new provider session")
        worker._record_session(workspace, "codex", room_key, session_id)
        resumed, created = worker._session_id(workspace, "codex", room_key)
        check(not created and resumed == session_id, "second message in room resumes provider session")
        other_id, created = worker._session_id(workspace, "codex", other_key)
        check(created and other_id != session_id, "different room proposes a different provider session")
        worker._record_session(workspace, "codex", room_key, session_id)
        state = json.loads(worker._state_path(workspace).read_text(encoding="utf-8"))
        row = state["sessions"]["codex"][room_key]
        check(row["created_at"] <= row["updated_at"], "session timestamps retain creation time")
        try:
            worker._record_session(workspace, "codex", room_key, "not-a-session")
        except ValueError:
            check(True, "invalid provider session id is refused")
        else:
            check(False, "invalid provider session id is refused")


def test_claude_create_resume_and_failure() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        workspace = root / "workspace"
        (workspace / "results").mkdir(parents=True)
        log = root / "claude.jsonl"
        executable(root / "claude", """#!/usr/bin/env python3
import json, os, pathlib, sys
with pathlib.Path(os.environ['ARG_LOG']).open('a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
if 'Process and write the result to results/' in sys.argv[-1]:
    pathlib.Path(os.environ['ROGUE_RESULT']).write_text('child direct write\\n')
if os.environ.get('FAIL_PROVIDER'):
    print('provider failed', file=sys.stderr)
    raise SystemExit(7)
print('claude room result')
""")
        env = {
            "PATH": f"{root}:{os.environ['PATH']}",
            "ARG_LOG": str(log),
            "ROGUE_RESULT": str(workspace / "results" / "task-claude-one.txt"),
        }
        first = task(workspace, "task-claude-one")
        second = task(workspace, "task-claude-two")
        check(run_worker("claude", workspace, first, env).returncode == 0, "Claude creates room session")
        check(run_worker("claude", workspace, second, env).returncode == 0, "Claude resumes room session")
        args = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        check("--session-id" in args[0] and "--resume" in args[1], "Claude uses create then resume")
        first_id = args[0][args[0].index("--session-id") + 1]
        second_id = args[1][args[1].index("--resume") + 1]
        check(first_id == second_id, "Claude resumes the same room session id")
        check((workspace / "results" / first.name).read_text() == "claude room result\n",
              "child cannot become a second result writer")
        first_prompt = args[0][-1]
        check("answer this" in first_prompt and "SKILL INSTRUCTIONS" not in first_prompt,
              "child receives task content without legacy delivery instructions")
        check(first.name not in first_prompt and str(first) not in first_prompt,
              "child receives neither task id nor original task path")

        failed = task(workspace, "task-claude-fail", room_id="!failure:a")
        result = run_worker("claude", workspace, failed, {**env, "FAIL_PROVIDER": "1"})
        check(result.returncode == 1 and "provider failed" in result.stderr,
              "provider failure returns watcher fallback signal")
        check(not (workspace / "results" / failed.name).exists(), "provider failure publishes no result")


def test_codex_create_resume_and_result_ownership() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        workspace = root / "workspace"
        (workspace / "results").mkdir(parents=True)
        log = root / "codex.jsonl"
        thread_id = str(uuid.uuid4())
        executable(root / "codex", f"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ['ARG_LOG']).open('a') as out:
    out.write(json.dumps(args) + '\\n')
pathlib.Path(args[args.index('-o') + 1]).write_text('codex room result\\n')
if 'resume' not in args:
    print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}))
""")
        env = {"PATH": f"{root}:{os.environ['PATH']}", "ARG_LOG": str(log)}
        first = task(workspace, "task-codex-one")
        second = task(workspace, "task-codex-two")
        check(run_worker("codex", workspace, first, env).returncode == 0, "Codex creates room thread")
        check(run_worker("codex", workspace, second, env).returncode == 0, "Codex resumes room thread")
        args = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        check("resume" not in args[0] and "resume" in args[1], "Codex uses exec then exec resume")
        check(thread_id in args[1], "Codex resumes the reported thread id")
        state = json.loads(worker._state_path(workspace).read_text(encoding="utf-8"))
        room_key = worker.resolve_room_key(first)
        assert room_key
        check(state["sessions"]["codex"][room_key]["session_id"] == thread_id,
              "Codex reported thread id is persisted")

        existing = task(workspace, "task-existing", room_id="!existing:a")
        (workspace / "results" / existing.name).write_text("consumer wins\n")
        before = len(log.read_text().splitlines())
        check(run_worker("codex", workspace, existing, env).returncode == 0,
              "existing consumer result settles task")
        check(len(log.read_text().splitlines()) == before, "settled task never launches provider")
        check((workspace / "results" / existing.name).read_text() == "consumer wins\n",
              "publish-once contract never clobbers consumer")

        whitespace = task(workspace, "task-whitespace", room_id="!whitespace:a")
        (workspace / "results" / whitespace.name).write_text("  \n")
        before = len(log.read_text().splitlines())
        result = run_worker("codex", workspace, whitespace, env)
        check(result.returncode == 1 and len(log.read_text().splitlines()) == before + 1,
              "whitespace live result invokes provider then falls back without clobbering")

        archived = task(workspace, "task-archived", room_id="!archived:a")
        archive = workspace / "results" / "archive"
        archive.mkdir()
        (archive / f"{archived.stem}-123.txt").write_text("already sent\n")
        before = len(log.read_text().splitlines())
        check(run_worker("codex", workspace, archived, env).returncode == 0,
              "gateway-archived result settles task")
        check(len(log.read_text().splitlines()) == before, "archived task is never replayed")

        retained = task(workspace, "task-retained", room_id="!retained:a")
        retention = workspace / "results" / "archive-2026-08-21"
        retention.mkdir()
        (retention / retained.name).write_text("already sent\n")
        check(run_worker("codex", workspace, retained, env).returncode == 0,
              "retention archive also prevents replay")

        archived_blank = task(workspace, "task-archived-blank", room_id="!archived-blank:a")
        (archive / archived_blank.name).write_text("\n")
        before = len(log.read_text().splitlines())
        check(run_worker("codex", workspace, archived_blank, env).returncode == 0,
              "whitespace archived result is not treated as completion")
        check(len(log.read_text().splitlines()) == before + 1,
              "whitespace archive invokes the provider path")


def test_codex_event_and_state_edges() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        workspace = root / "workspace"
        (workspace / "results").mkdir(parents=True)
        valid_id = str(uuid.uuid4())
        executable(root / "codex", f"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
pathlib.Path(args[args.index('-o') + 1]).write_text('edge result\\n')
mode = os.environ.get('MODE')
if mode == 'fail':
    print('codex failed', file=sys.stderr)
    raise SystemExit(8)
if mode == 'events':
    print('not-json')
    print(json.dumps({{'type': 'thread.started', 'thread_id': 'bad'}}))
    print(json.dumps({{'type': 'other', 'thread_id': '{valid_id}'}}))
    print(json.dumps({{'type': 'thread.started', 'thread_id': '{valid_id}'}}))
""")
        env = {"PATH": f"{root}:{os.environ['PATH']}"}
        edge = task(workspace, "task-events")
        room_key = worker.resolve_room_key(edge)
        assert room_key
        worker._atomic_text(
            worker._state_path(workspace),
            json.dumps({"schema_version": 1, "sessions": {"codex": {
                room_key: {"session_id": "corrupt"}
            }}}),
        )
        check(run_worker("codex", workspace, edge, {**env, "MODE": "events"}).returncode == 0,
              "Codex ignores malformed events and replaces corrupt state")

        no_id = task(workspace, "task-no-id", room_id="!no-id:a")
        result = run_worker("codex", workspace, no_id, env)
        check(result.returncode == 1 and "valid thread.started" in result.stderr,
              "Codex requires a valid new thread id")

        failed = task(workspace, "task-codex-failure", room_id="!codex-failure:a")
        result = run_worker("codex", workspace, failed, {**env, "MODE": "fail"})
        check(result.returncode == 1 and "codex failed" in result.stderr,
              "Codex provider failure returns watcher fallback signal")


def test_runtime_edges_and_adapter_wiring() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        sleeper = executable(root / "sleepy", "#!/bin/sh\nsleep 2\n")
        with patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": "0.1", "SUTANDO_TIER_STALL_TIMEOUT": "0.05"}):
            started = time.monotonic()
            try:
                worker._run_bounded([str(sleeper)], root)
            except TimeoutError:
                check(time.monotonic() - started < 1, "stalled provider is terminated within bound")
            else:
                check(False, "stalled provider is terminated within bound")
        with patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": "0"}):
            try:
                worker._run_bounded(["true"], root)
            except ValueError:
                check(True, "non-positive provider timeout is rejected")
            else:
                check(False, "non-positive provider timeout is rejected")

        with patch.dict(os.environ, {}, clear=True):
            check(worker._timeout("SUTANDO_TIER_HARD_TIMEOUT", None) == 900,
                  "manifest declares the hard-timeout default")
            check(worker._timeout("SUTANDO_TIER_STALL_TIMEOUT", None) == 180,
                  "manifest declares the stall-timeout default")
        with patch.dict(os.environ, {"SUTANDO_TIER_STALL_TIMEOUT": "12"}):
            check(worker._timeout("SUTANDO_TIER_STALL_TIMEOUT", None) == 12,
                  "environment overrides the manifest timeout")
            check(worker._timeout("SUTANDO_TIER_STALL_TIMEOUT", 13) == 13,
                  "CLI timeout overrides the environment")

        with patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": "0.05",
                                     "SUTANDO_TIER_STALL_TIMEOUT": "2"}):
            for command, message in [
                ([str(sleeper)], "hard timeout terminates a provider with open streams"),
                (["sh", "-c", "exec 1>&- 2>&-; sleep 2"],
                 "hard timeout terminates a provider after streams close"),
            ]:
                try:
                    worker._run_bounded(command, root)
                except TimeoutError:
                    check(True, message)
                else:
                    check(False, message)

        ended = MagicMock()
        ended.poll.return_value = 0
        worker._terminate(ended)
        check(not ended.wait.called, "termination is a no-op for an exited provider")
        stubborn = MagicMock(pid=123)
        stubborn.poll.return_value = None
        stubborn.wait.side_effect = [subprocess.TimeoutExpired("provider", 2), None]
        with patch.object(worker.os, "killpg") as killpg:
            worker._terminate(stubborn)
        check(killpg.call_count == 2, "termination escalates an unresponsive provider")

        model = "test-model"
        settings = '{"hooks":{}}'
        with patch.dict(os.environ, {"SUTANDO_CORE_MODEL": model,
                                     "SUTANDO_ISOLATED_CLAUDE_SETTINGS": settings}):
            claude = worker._claude_command(str(uuid.uuid4()), False, "prompt")
            codex = worker._codex_command(None, "prompt", root / "out", REPO)
        check("--model" in claude and "--settings" in claude, "Claude inherits model and hook settings")
        check("-m" in codex, "Codex inherits selected core model")

    relative = "skills/ag2space-room-sessions/scripts/session-worker.py"
    claude_start = (REPO / "src/agent/claude/cli/start-cli.sh").read_text(encoding="utf-8")
    codex_start = (REPO / "src/agent/codex/cli/start-cli.sh").read_text(encoding="utf-8")
    watcher = (REPO / "src/watch-tasks-stream.sh").read_text(encoding="utf-8")
    check(relative in claude_start and "SUTANDO_ISOLATED_CLAUDE_SETTINGS" in claude_start,
          "Claude adapter injects room-session capability")
    check(relative in codex_start and 'version_files+=("$ROOM_SESSION_HANDLER")' in codex_start,
          "Codex adapter injects and versions room-session capability")
    check("ag2space-room-sessions" not in watcher, "generic watcher does not name concrete skill")


def test_cli_probe_and_empty_output() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        workspace = root / "workspace"
        (workspace / "results").mkdir(parents=True)
        room_task = task(workspace, "task-probe")
        probe = subprocess.run(
            [str(WORKER), "--runtime", "codex", "--workspace", str(workspace),
             "--task-file", str(room_task), "--results-dir", str(workspace / "results"),
             "--repo", str(REPO), "--probe"],
            cwd=REPO, check=False,
        )
        check(probe.returncode == 0, "CLI probe delegates to room policy")
        executable(root / "claude", "#!/bin/sh\nprintf '   \\n'\n")
        result = run_worker("claude", workspace, room_task, {"PATH": f"{root}:{os.environ['PATH']}"})
        check(result.returncode == 1 and "empty result" in result.stderr,
              "empty provider output falls back without publishing")


def test_direct_failure_and_cli_edges() -> None:
    with tempfile.TemporaryDirectory() as name:
        workspace = Path(name)
        results = workspace / "results"
        results.mkdir()
        unhandled = task(workspace, "task-unhandled", tier="team")
        check(worker.handle("codex", workspace, unhandled, results, REPO) == worker.UNHANDLED,
              "handle preserves the generic watcher path for an unhandled task")
        check(worker.resolve_room_key(workspace / "tasks" / "absent.txt") is None,
              "room resolver fails closed on an unreadable task")

        published = results / "publish-once.txt"
        worker._publish_once(published, "first\n")
        accepted = worker._publish_once(published, "second\n")
        check(accepted and published.read_text() == "first\n",
              "publish-once accepts an already-ready winning writer")

        raced = task(workspace, "task-raced")
        with patch.object(worker, "_completed_result_exists", side_effect=[False, True]), \
             patch.object(worker, "_run_codex") as provider:
            code = worker.handle("codex", workspace, raced, results, REPO)
        check(code == 0 and not provider.called, "locked completion check prevents a duplicate launch")

        argv = ["session-worker", "--runtime", "codex", "--workspace", str(workspace),
                "--task-file", str(raced), "--results-dir", str(results), "--repo", str(REPO)]
        with patch.object(sys, "argv", argv + ["--probe"]), patch.object(worker, "probe", return_value=17):
            check(worker.main() == 17, "CLI main dispatches probe mode")
        with patch.object(sys, "argv", argv), patch.object(worker, "handle", return_value=18):
            check(worker.main() == 18, "CLI main dispatches handle mode")


def main() -> int:
    test_opt_in_and_cardinality()
    test_session_state_reuses_room_not_message()
    test_claude_create_resume_and_failure()
    test_codex_create_resume_and_result_ownership()
    test_codex_event_and_state_edges()
    test_runtime_edges_and_adapter_wiring()
    test_cli_probe_and_empty_output()
    test_direct_failure_and_cli_edges()
    print(f"\nResults: {len(FAILURES)} failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
