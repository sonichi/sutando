#!/usr/bin/env python3
"""Behavioral coverage for durable per-workstream provider sessions."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
# Guards a hang, not promptness — a leaked worker holds stdout open forever, so any
# bound catches it. Every timing claim here is a separate assert; keep this generous.
SHUTDOWN_DRAIN_TIMEOUT_S = 30
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


def test_resolution_routes_bounded_tiers_before_owner_workstreams() -> None:
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
        assert worker.probe("claude", workspace, team) == worker.MUST_HANDLE
        assert worker.probe("claude", workspace, _task(workspace, "task-guest", "guest")) == worker.UNHANDLED


def test_tier_parser_prevents_task_body_escalation_and_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        task_last = _task(workspace, "task-task-last", "team")
        task_last.write_text(task_last.read_text() + "access_tier: owner\n")
        assert worker.resolve_access_tier(task_last) == "team"

        task_mid = _task(workspace, "task-task-mid")
        task_mid.write_text(
            "id: task-task-mid\ntask: confined body\nsource: ag2space\naccess_tier: guest\n")
        assert worker.resolve_access_tier(task_mid) == "guest"

        invalid = _task(workspace, "task-invalid", "sudo")
        assert worker.resolve_access_tier(invalid) == "guest"
        assert worker.resolve_access_tier(_task(workspace, "task-other", "other")) == "guest"
        assert worker.resolve_access_tier(workspace / "tasks" / "absent.txt") == "guest"
        missing = _task(workspace, "task-legacy")
        missing.write_text("id: task-legacy\ntask: legacy local task\n")
        assert worker.resolve_access_tier(missing) == "owner"


def test_team_uses_owner_claude_native_sandbox_while_guest_stays_legacy() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        log = root / "claude-args.jsonl"
        _executable(root / "claude", """#!/usr/bin/env python3
import json, os, sys
if '--version' in sys.argv:
    print('2.1.220 (Claude Code)')
    raise SystemExit
with open(os.environ['PROVIDER_LOG'], 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')
print(json.dumps({'type': 'result', 'result': 'bounded claude result'}))
""")
        env = {"PATH": f"{root}:{os.environ['PATH']}", "PROVIDER_LOG": str(log)}
        team = _task(workspace, "task-team-runtime", "team")
        guest = _task(workspace, "task-guest-runtime", "guest")
        assert _run("claude", workspace, team, env).returncode == 0
        assert _run("claude", workspace, guest, env).returncode == worker.UNHANDLED

        [team_args] = [json.loads(line) for line in log.read_text().splitlines()]
        assert team_args[:2] == ["-p", "--no-session-persistence"]
        assert team_args[team_args.index("--permission-mode") + 1] == "acceptEdits"
        assert team_args[team_args.index("--tools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
        settings = json.loads(team_args[team_args.index("--settings") + 1])
        assert settings["sandbox"]["enabled"] is True
        assert settings["sandbox"]["failIfUnavailable"] is True
        assert settings["sandbox"]["allowUnsandboxedCommands"] is False
        assert settings["sandbox"]["network"] == {
            "allowedDomains": [], "strictAllowlist": True}
        assert "--verbose" in team_args and "stream-json" in team_args
        assert "codex" not in team_args
        assert (workspace / "results" / team.name).read_text() == "bounded claude result"
        assert not (workspace / "results" / guest.name).exists()


def test_team_uses_owner_codex_workspace_sandbox_while_guest_stays_legacy() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        log = root / "codex-args.jsonl"
        _executable(root / "codex", """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['PROVIDER_LOG'], 'a') as f: f.write(json.dumps(args) + '\\n')
pathlib.Path(args[args.index('-o') + 1]).write_text('bounded codex result\\n')
""")
        env = {"PATH": f"{root}:{os.environ['PATH']}", "PROVIDER_LOG": str(log)}
        team = _task(workspace, "task-team-codex", "team")
        guest = _task(workspace, "task-guest-codex", "guest")
        assert _run("codex", workspace, team, env).returncode == 0
        assert _run("codex", workspace, guest, env).returncode == worker.UNHANDLED
        [team_args] = [json.loads(line) for line in log.read_text().splitlines()]
        assert team_args[team_args.index("--sandbox") + 1] == "workspace-write"
        assert "--ignore-user-config" in team_args and "--ephemeral" in team_args
        assert "--json" in team_args


def test_team_capability_root_is_the_owner_configured_workspace() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        task_workspace = root / "task-queue"
        owner_workspace = root / "owner-workspace"
        owner_workspace.mkdir()
        owner_workspace = owner_workspace.resolve()
        (owner_workspace / "owner-project.txt").write_text("team-visible\n")

        claude_log = root / "claude.json"
        _executable(root / "claude", """#!/usr/bin/env python3
import json, os, sys
if '--version' in sys.argv:
    print('2.1.220 (Claude Code)')
    raise SystemExit
json.dump({'args': sys.argv[1:], 'cwd': os.getcwd()}, open(os.environ['PROVIDER_LOG'], 'w'))
print(json.dumps({'type': 'result', 'result': 'bounded claude result'}))
""")
        team = _task(task_workspace, "task-team-owner-workspace", "team")
        shared_env = {
            "PATH": f"{root}:{os.environ['PATH']}",
            "SUTANDO_ISOLATED_WORKING_DIR": str(owner_workspace),
            "PROVIDER_LOG": str(claude_log),
        }
        assert _run("claude", task_workspace, team, shared_env).returncode == 0
        claude = json.loads(claude_log.read_text())
        assert claude["cwd"] != str(owner_workspace)
        assert claude["args"][claude["args"].index("--add-dir") + 1] == str(owner_workspace)
        settings = json.loads(claude["args"][claude["args"].index("--settings") + 1])
        assert str(owner_workspace) in settings["sandbox"]["filesystem"]["allowRead"]
        assert str(owner_workspace) in settings["sandbox"]["filesystem"]["allowWrite"]
        assert str(REPO) not in settings["sandbox"]["filesystem"]["allowWrite"]
        prompt = claude["args"][-1]
        assert f"team workspace at {owner_workspace}" in prompt

        codex_log = root / "codex.json"
        _executable(root / "codex", """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
json.dump({'args': args, 'cwd': os.getcwd()}, open(os.environ['PROVIDER_LOG'], 'w'))
pathlib.Path(args[args.index('-o') + 1]).write_text('bounded codex result\\n')
""")
        team = _task(task_workspace, "task-team-owner-workspace-codex", "team")
        shared_env["PROVIDER_LOG"] = str(codex_log)
        assert _run("codex", task_workspace, team, shared_env).returncode == 0
        codex = json.loads(codex_log.read_text())
        assert codex["cwd"] == str(owner_workspace)
        assert codex["args"][codex["args"].index("-C") + 1] == str(owner_workspace)
        assert str(REPO) not in codex["args"]


def test_bounded_runtime_failure_never_falls_back_to_owner_core() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        _executable(
            root / "claude",
            "#!/bin/sh\n[ \"$1\" = --version ] && { echo '2.1.220'; exit; }\n"
            "printf 'sandbox unavailable\\n' >&2\nexit 9\n",
        )
        task = _task(workspace, "task-team-fail-closed", "team")
        result = _run("claude", workspace, task, {"PATH": f"{root}:{os.environ['PATH']}"})
        assert result.returncode == 0
        body = (workspace / "results" / task.name).read_text()
        assert "restricted runtime was unavailable" in body
        assert "No unrestricted fallback was used" in body


def test_stalled_team_runtime_is_killed_and_publishes_safe_result() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        _executable(
            root / "claude",
            "#!/bin/sh\n[ \"$1\" = --version ] && { echo '2.1.220'; exit; }\nsleep 30\n",
        )
        task = _task(workspace, "task-team-stall", "team")
        started = time.monotonic()
        result = _run("claude", workspace, task, {
            "PATH": f"{root}:{os.environ['PATH']}",
            "SUTANDO_TIER_STALL_TIMEOUT": "0.15",
            "SUTANDO_TIER_HARD_TIMEOUT": "1",
        })
        assert time.monotonic() - started < 2
        assert result.returncode == 0
        assert "No unrestricted fallback was used" in (
            workspace / "results" / task.name).read_text()


def test_partial_output_then_stall_still_hits_the_deadline() -> None:
    """Regression: a provider that emits a partial line (no newline) then hangs
    must NOT wedge the timeout loop. A blocking readline() would block on the
    incomplete line and never re-check the deadline; nonblocking reads must fail
    closed at the hard timeout instead of waiting out the 5s child."""
    child = "import sys, time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(5)"
    started = time.monotonic()
    with mock.patch.dict(os.environ, {
        "SUTANDO_TIER_HARD_TIMEOUT": "0.3",
        "SUTANDO_TIER_STALL_TIMEOUT": "0.2",
    }):
        try:
            worker._run_process_bounded([sys.executable, "-c", child], Path("."))
            raise AssertionError("expected a TimeoutError, provider was not bounded")
        except TimeoutError:
            elapsed = time.monotonic() - started
            # must trip on the deadline (~0.3s), not wait out the 5s child
            assert elapsed < 2, f"timeout loop blocked on the partial line ({elapsed:.2f}s)"


def test_closes_pipes_then_stalls_still_hits_the_deadline() -> None:
    """Regression: a provider that closes stdout+stderr then hangs must NOT sail
    past the deadline via the post-EOF wait. Once both pipes EOF, the selector
    loop exits; a plain process.wait() there would block on the still-running
    child forever. The bounded wait must fail closed at the deadline instead."""
    child = "import os, time; os.close(1); os.close(2); time.sleep(5)"
    started = time.monotonic()
    with mock.patch.dict(os.environ, {
        "SUTANDO_TIER_HARD_TIMEOUT": "0.3",
        "SUTANDO_TIER_STALL_TIMEOUT": "0.2",
    }):
        try:
            worker._run_process_bounded([sys.executable, "-c", child], Path("."))
            raise AssertionError("expected a TimeoutError, provider was not bounded")
        except TimeoutError:
            elapsed = time.monotonic() - started
            assert elapsed < 2, f"post-EOF wait blocked on the stalled child ({elapsed:.2f}s)"


def test_older_claude_fails_closed_before_receiving_team_prompt() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        invoked = root / "invoked"
        _executable(
            root / "claude",
            f"#!/bin/sh\n[ \"$1\" = --version ] && {{ echo '2.1.218'; exit; }}\ntouch '{invoked}'\n",
        )
        task = _task(workspace, "task-team-old-claude", "team")
        result = _run("claude", workspace, task, {"PATH": f"{root}:{os.environ['PATH']}"})
        assert result.returncode == 0
        assert not invoked.exists()
        assert "need 2.1.219+" in result.stderr
        assert "No unrestricted fallback was used" in (
            workspace / "results" / task.name).read_text()


def test_installed_claude_enforces_team_credential_and_network_boundary() -> None:
    """Hermetic real-CLI probe: the local server replaces model inference."""
    claude = shutil.which("claude")
    if not claude:
        return
    try:
        worker._require_claude_team_sandbox()
    except RuntimeError:
        return  # Older installed CLIs are rejected by the production gate.

    requests: list[dict] = []
    network_probes: list[str] = []

    class ProbeHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            network_probes.append(self.path)
            self.send_response(200)
            self.end_headers()

    probe_server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    probe_thread = threading.Thread(target=probe_server.serve_forever, daemon=True)
    probe_thread.start()
    shell_command = (
        "printf 'ENV=%s\\n' \"$GITHUB_TOKEN\"; "
        "cat \"$HOME/.aws/team-secret\"; "
        "printf TEAM_WRITE > \"$TEAM_WORKSPACE/team-output\"; "
        f"curl --max-time 2 -sS http://127.0.0.1:{probe_server.server_port}/probe"
    )

    def sse(block: dict, stop_reason: str) -> bytes:
        message = {
            "id": "msg_test", "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-5", "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        }
        events = [
            ("message_start", {"type": "message_start", "message": message}),
            ("content_block_start", {
                "type": "content_block_start", "index": 0, "content_block": block}),
        ]
        delta = (
            {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            if block["type"] == "tool_use"
            else {"type": "text_delta", "text": block["text"]}
        )
        events.extend([
            ("content_block_delta", {
                "type": "content_block_delta", "index": 0, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 1},
            }),
            ("message_stop", {"type": "message_stop"}),
        ])
        return "".join(
            f"event: {event}\ndata: {json.dumps(payload)}\n\n"
            for event, payload in events
        ).encode()

    class ApiHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            requests.append(body)
            has_result = any(
                isinstance(message.get("content"), list)
                and any(
                    isinstance(item, dict) and item.get("type") == "tool_result"
                    for item in message["content"])
                for message in body.get("messages", [])
            )
            block = (
                {"type": "text", "text": "sandbox probe complete"}
                if has_result else {
                    "type": "tool_use", "id": "toolu_test", "name": "Bash",
                    "input": {"command": shell_command},
                }
            )
            payload = sse(block, "end_turn" if has_result else "tool_use")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    api_server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
    api_thread.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            repo = root / "repo"
            (home / ".aws").mkdir(parents=True)
            repo.mkdir()
            (home / ".aws" / "team-secret").write_text("FILE_SECRET")
            with mock.patch.object(worker.Path, "home", return_value=home):
                settings = worker._claude_tier_settings(repo)
            command = worker._claude_bounded_command("run the requested probe", repo)
            command[0] = claude
            command.insert(2, "--bare")
            environment = {
                **os.environ,
                "HOME": str(home),
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{api_server.server_port}",
                "ANTHROPIC_API_KEY": "test-only-key",
                "GITHUB_TOKEN": "ENV_SECRET",
                "TEAM_WORKSPACE": str(repo),
                "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
            }
            settings_index = command.index("--settings") + 1
            command[settings_index] = settings
            completed = subprocess.run(
                command, cwd=repo, env=environment, text=True,
                capture_output=True, timeout=20,
            )
            team_write = (repo / "team-output").read_text()
        assert completed.returncode == 0, completed.stderr
        tool_results = [
            item
            for request in requests
            for message in request.get("messages", [])
            if isinstance(message.get("content"), list)
            for item in message["content"]
            if isinstance(item, dict) and item.get("type") == "tool_result"
        ]
        assert tool_results
        output = str(tool_results[-1].get("content") or "")
        assert "ENV_SECRET" not in output and "FILE_SECRET" not in output, output
        assert "ENV=\n" in output, output
        assert "Operation not permitted" in output or "Permission denied" in output, output
        assert team_write == "TEAM_WRITE"
        assert network_probes == []
    finally:
        api_server.shutdown()
        probe_server.shutdown()


def test_bounded_runtime_helper_edges() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        with mock.patch.dict(os.environ, {"SUTANDO_CORE_MODEL": "tier-model"}, clear=False):
            assert "--model" in worker._claude_bounded_command("p", REPO)
            assert "-m" in worker._codex_bounded_command("p", REPO, root / "out")

        with mock.patch.object(worker.subprocess, "run", side_effect=OSError("missing")):
            try:
                worker._require_claude_team_sandbox()
                raise AssertionError("missing Claude must fail closed")
            except RuntimeError as exc:
                assert "could not verify" in str(exc)
        invalid_version = subprocess.CompletedProcess([], 0, "not-a-version", "")
        with mock.patch.object(worker.subprocess, "run", return_value=invalid_version):
            try:
                worker._require_claude_team_sandbox()
                raise AssertionError("unparseable Claude version must fail closed")
            except RuntimeError as exc:
                assert "sandbox version" in str(exc)

        already_done = mock.Mock()
        already_done.poll.return_value = 0
        worker._terminate_process_group(already_done)
        already_done.wait.assert_not_called()

        stubborn = mock.Mock(pid=12345)
        stubborn.poll.return_value = None
        stubborn.wait.side_effect = [subprocess.TimeoutExpired("provider", 2), 0]
        with mock.patch.object(
            worker.os, "killpg", side_effect=[None, ProcessLookupError]
        ) as killed:
            worker._terminate_process_group(stubborn)
        assert killed.call_count == 2

        with mock.patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": "0"}, clear=False):
            try:
                worker._run_process_bounded(["/bin/true"], REPO)
                raise AssertionError("invalid timeout must be rejected")
            except ValueError:
                pass
        with mock.patch.dict(os.environ, {
            "SUTANDO_TIER_HARD_TIMEOUT": "0.1",
            "SUTANDO_TIER_STALL_TIMEOUT": "2",
        }, clear=False):
            try:
                worker._run_process_bounded(["/bin/sleep", "30"], REPO)
                raise AssertionError("hard timeout must stop the provider")
            except TimeoutError as exc:
                assert "hard timeout" in str(exc)

        try:
            worker._claude_stream_result("not-json\n{}")
            raise AssertionError("missing result event must fail")
        except RuntimeError as exc:
            assert "terminal result" in str(exc)

        (workspace / "state").mkdir(parents=True)
        with mock.patch.object(worker, "_run_process_bounded", return_value=(7, "", "nope")):
            try:
                worker._run_bounded("codex", "p", REPO, workspace)
                raise AssertionError("Codex failure must fail closed")
            except RuntimeError as exc:
                assert str(exc) == "nope"


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
        state_path = workspace / "state" / "task-workstream-sessions.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "schema_version": 1,
            "sessions": {"codex": {"workstream-a": {"session_id": "corrupt"}}},
        }))
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


def test_required_team_handler_failure_never_emits_live_core_event() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        tasks = workspace / "tasks"
        results = workspace / "results"
        tasks.mkdir(parents=True)
        results.mkdir()
        task = tasks / "task-team-required.txt"
        task.write_text("access_tier: team\ntask: protected\n")
        handler = _executable(
            root / "handler",
            "#!/bin/sh\ncase \" $* \" in *\" --probe \"*) exit 4;; esac\nexit 9\n",
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _executable(bin_dir / "fswatch", "#!/bin/sh\nsleep 0.2\n")
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
        assert "TASK_FILE:" not in result.stdout
        assert "safe terminal failure" in result.stderr
        assert "No unrestricted fallback was used" in (results / task.name).read_text()
        assert not (workspace / "state" / "task-event-handler-fallbacks" / task.name).exists()
        assert not (workspace / "state" / "task-event-handler-claims" / task.name).exists()


def test_required_team_handler_shutdown_never_falls_through() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        tasks = workspace / "tasks"
        results = workspace / "results"
        tasks.mkdir(parents=True)
        results.mkdir()
        task = tasks / "task-team-interrupted.txt"
        task.write_text("access_tier: team\ntask: protected\n")
        handler = _executable(
            root / "handler",
            "#!/bin/sh\ncase \" $* \" in *\" --probe \"*) exit 4;; esac\nsleep 30\n",
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _executable(bin_dir / "fswatch", "#!/bin/sh\nsleep 30\n")
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
        claim = workspace / "state" / "task-event-handler-claims" / task.name
        try:
            deadline = time.monotonic() + 1
            while not claim.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert claim.exists()
            assert claim.read_text().splitlines()[3] == "must-handle"
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=3)
            assert "TASK_FILE:" not in stdout
            assert "safe terminal failure" in stderr
            assert "No unrestricted fallback was used" in (results / task.name).read_text()
            assert not claim.exists()
            assert not (workspace / "state" / "task-event-handler-fallbacks" / task.name).exists()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=2)


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
            process.communicate(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)


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
            process.communicate(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)


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
            overlap.communicate(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)
            overlap = None
            assert claim.is_file(), "non-owner cleanup must not remove another watcher's claim"

            os.killpg(owner.pid, signal.SIGTERM)
            owner_stdout, _ = owner.communicate(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)
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
                overlap.communicate(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)
            if owner.poll() is None:
                os.killpg(owner.pid, signal.SIGKILL)
                owner.communicate(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)


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
            stdout, stderr = process.communicate(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)
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
                process.communicate(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)


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
            process.communicate(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)


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
            process.communicate(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)


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
    test_resolution_routes_bounded_tiers_before_owner_workstreams()
    test_tier_parser_prevents_task_body_escalation_and_fails_closed()
    test_team_uses_owner_claude_native_sandbox_while_guest_stays_legacy()
    test_team_uses_owner_codex_workspace_sandbox_while_guest_stays_legacy()
    test_team_capability_root_is_the_owner_configured_workspace()
    test_bounded_runtime_failure_never_falls_back_to_owner_core()
    test_stalled_team_runtime_is_killed_and_publishes_safe_result()
    test_older_claude_fails_closed_before_receiving_team_prompt()
    test_partial_output_then_stall_still_hits_the_deadline()
    test_closes_pipes_then_stalls_still_hits_the_deadline()
    test_installed_claude_enforces_team_credential_and_network_boundary()
    test_bounded_runtime_helper_edges()
    test_claude_creates_then_resumes_the_same_durable_session()
    test_nonzero_provider_stdout_is_never_written_as_a_result()
    test_archived_result_is_not_replayed_on_restart_scan()
    test_result_publish_never_clobbers_an_existing_consumer()
    test_codex_records_reported_uuid_then_uses_exec_resume()
    test_fail_open_validation_and_provider_error_edges()
    test_codex_failures_and_empty_provider_results_are_retryable()
    test_cli_main_delegates_parsed_paths()
    test_watcher_provider_failure_falls_back_without_leaking_stdout()
    test_required_team_handler_failure_never_emits_live_core_event()
    test_required_team_handler_shutdown_never_falls_through()
    test_slow_handler_does_not_block_the_next_task_event()
    test_watcher_bounds_provider_backlog_and_drains_every_receipt_once()
    test_overlapping_watcher_preserves_live_claim_and_owner_shutdown_falls_back()
    test_shutdown_falls_back_without_surviving_workers()
    test_codex_notifier_dispatches_each_isolated_task_once_without_waiting()
    test_codex_notifier_never_submits_a_watcher_claim_to_live_core()
    test_runtime_wiring_is_optional_and_adapter_injected()
    print("task workstream session worker tests passed")
