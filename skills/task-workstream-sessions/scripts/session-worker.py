#!/usr/bin/env python3
"""Run an assigned owner task in a durable per-workstream provider session.

Exit 0 means the task was handled (including an already-existing result).
Exit 3 means the caller must use its unchanged legacy live-core path.  Any
other exit means an isolated worker was attempted but failed; callers fall
back to the live core so the durable task is not stranded, with an explicit
at-least-once warning because the provider may already have made changes.

Tradeoff: assigned workstreams run as headless provider sessions, so their live
transcript is not rendered in the canonical core pane.  That keeps provider
session ids resumable across core restarts without multiplying task watchers;
ungrouped work remains on the visible legacy core session.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


UNHANDLED = 3
SCHEMA_VERSION = 1
SESSION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _publish_result(path: Path, body: str) -> None:
    """Atomically publish once; another consumer's result always wins."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _completed_result_exists(results_dir: Path, filename: str) -> bool:
    """Avoid replay when a bridge already archived this task's result."""
    results_dir = Path(results_dir)
    if (results_dir / filename).is_file():
        return True
    stem = Path(filename).stem
    exact_or_gateway = re.compile(rf"^{re.escape(stem)}(?:-[0-9]+)?\.txt$")
    try:
        candidates = list((results_dir / "archive").glob("*.txt"))
        candidates += list((results_dir / "archive").glob("*/*.txt"))
        for retention in results_dir.glob("archive-*"):
            if retention.is_dir():
                candidates += list(retention.glob("*.txt"))
        return any(path.is_file() and exact_or_gateway.fullmatch(path.name) for path in candidates)
    except OSError:
        return False


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _headers(task_file: Path) -> dict[str, str]:
    try:
        content = task_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    headers: dict[str, str] = {}
    for line in content.splitlines():
        if line.startswith("==="):
            break
        key, separator, value = line.partition(":")
        if separator and re.fullmatch(r"[a-z_]+", key):
            headers.setdefault(key, value.strip())
            if key == "task":
                break
    return headers


def resolve_workstream(workspace: Path, task_file: Path) -> Optional[str]:
    """Return a valid assigned owner workstream, otherwise fail open."""
    headers = _headers(task_file)
    # Pre-tier task files retain the repository's legacy owner default.
    if (headers.get("access_tier") or "owner").lower() != "owner":
        return None
    task_id = headers.get("id") or task_file.stem
    if task_id != task_file.stem:
        return None
    store = _read_json(workspace / "data" / "task-workstreams.json")
    if store.get("schema_version") != 1:
        return None
    assignments = store.get("assignments")
    workstreams = store.get("workstreams")
    if not isinstance(assignments, dict) or not isinstance(workstreams, dict):
        return None
    assignment = assignments.get(task_id)
    if not isinstance(assignment, dict):
        return None
    workstream_id = assignment.get("workstream_id")
    if not isinstance(workstream_id, str) or not workstream_id or len(workstream_id) > 200:
        return None
    if not isinstance(workstreams.get(workstream_id), dict):
        return None
    return workstream_id


def _state_path(workspace: Path) -> Path:
    return workspace / "state" / "task-workstream-sessions.json"


def _session_id(workspace: Path, runtime: str, workstream_id: str) -> tuple[str, bool]:
    state_path = _state_path(workspace)
    with _locked(workspace / "state" / "task-workstream-sessions.lock"):
        state = _read_json(state_path)
        if state.get("schema_version") != SCHEMA_VERSION:
            state = {"schema_version": SCHEMA_VERSION, "sessions": {}}
        sessions = state.setdefault("sessions", {})
        runtime_sessions = sessions.setdefault(runtime, {})
        row = runtime_sessions.get(workstream_id)
        if isinstance(row, dict) and SESSION_ID.fullmatch(str(row.get("session_id") or "")):
            return str(row["session_id"]), False
        # Do not persist a provider id until the first launch succeeds.  A
        # failed `claude --session-id` creates no resumable session, so storing
        # it early would make every later attempt resume a nonexistent id.
        return str(uuid.uuid4()), True


def _record_session(workspace: Path, runtime: str, workstream_id: str, session_id: str) -> None:
    if not SESSION_ID.fullmatch(session_id):
        raise ValueError(f"{runtime} returned an invalid session id")
    state_path = _state_path(workspace)
    with _locked(workspace / "state" / "task-workstream-sessions.lock"):
        state = _read_json(state_path)
        if state.get("schema_version") != SCHEMA_VERSION:
            state = {"schema_version": SCHEMA_VERSION, "sessions": {}}
        sessions = state.setdefault("sessions", {}).setdefault(runtime, {})
        old = sessions.get(workstream_id)
        now = datetime.now(timezone.utc).isoformat()
        sessions[workstream_id] = {
            "session_id": session_id,
            "created_at": old.get("created_at", now) if isinstance(old, dict) else now,
            "updated_at": now,
        }
        _atomic_json(state_path, state)


def _prompt(task_file: Path) -> str:
    return (
        f"Sutando task ready: {task_file.name}. Read {task_file}, follow AGENTS.md, "
        "and complete the task. This is an isolated delegated worker: do not create "
        "or write task/result tracking files. Return only the exact result body that "
        "the live core should deliver."
    )


def _claude_command(session_id: str, resume: bool, prompt: str, repo: Path) -> list[str]:
    command = ["claude", "-p"]
    command += ["--resume" if resume else "--session-id", session_id]
    command += ["--output-format", "text", "--dangerously-skip-permissions", "--add-dir", str(Path.home())]
    model = os.environ.get("SUTANDO_CORE_MODEL", "").strip()
    if model:
        command += ["--model", model]
    settings = os.environ.get("SUTANDO_ISOLATED_CLAUDE_SETTINGS", "").strip()
    if settings:
        command += ["--settings", settings]
    command += ["--", prompt]
    return command


def _codex_command(
    session_id: Optional[str],
    prompt: str,
    repo: Path,
    output_file: Path,
) -> list[str]:
    model = os.environ.get("SUTANDO_CORE_MODEL", "").strip()
    if session_id:
        command = [
            "codex", "--search", "exec", "resume", "--json", "-o", str(output_file),
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if model:
            command += ["-m", model]
        return command + [session_id, prompt]
    working_dir = Path(os.environ.get("SUTANDO_ISOLATED_WORKING_DIR", str(repo))).expanduser()
    command = [
        "codex", "--search", "exec", "--json", "-o", str(output_file), "-C", str(working_dir),
        "--add-dir", str(Path.home()), "--dangerously-bypass-approvals-and-sandbox",
    ]
    if model:
        command += ["-m", model]
    return command + [prompt]


def _run_claude(workspace: Path, workstream_id: str, prompt: str, repo: Path) -> str:
    session_id, created = _session_id(workspace, "claude", workstream_id)
    result = subprocess.run(
        _claude_command(session_id, not created, prompt, repo),
        cwd=os.environ.get("SUTANDO_ISOLATED_WORKING_DIR", str(repo)),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"claude exited {result.returncode}")
    if created:
        _record_session(workspace, "claude", workstream_id, session_id)
    return result.stdout


def _run_codex(workspace: Path, workstream_id: str, prompt: str, repo: Path) -> str:
    state = _read_json(_state_path(workspace))
    row = ((state.get("sessions") or {}).get("codex") or {}).get(workstream_id)
    session_id = str(row.get("session_id") or "") if isinstance(row, dict) else ""
    if session_id and not SESSION_ID.fullmatch(session_id):
        session_id = ""
    (workspace / "state").mkdir(parents=True, exist_ok=True)
    fd, output_name = tempfile.mkstemp(prefix=".workstream-result.", suffix=".txt", dir=workspace / "state")
    os.close(fd)
    output_file = Path(output_name)
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(
                _codex_command(session_id or None, prompt, repo, output_file),
                cwd=os.environ.get("SUTANDO_ISOLATED_WORKING_DIR", str(repo)),
                text=True,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
            )
            discovered = ""
            assert process.stdout is not None
            for line in process.stdout:
                if session_id:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if event.get("type") == "thread.started":
                    candidate = str(event.get("thread_id") or "")
                    if SESSION_ID.fullmatch(candidate):
                        discovered = candidate
            return_code = process.wait()
            stderr_file.seek(0)
            stderr = stderr_file.read()
        if return_code:
            raise RuntimeError(stderr.strip() or f"codex exited {return_code}")
        if not session_id and not discovered:
            raise RuntimeError("codex did not report a valid thread.started session id")
        if discovered:
            _record_session(workspace, "codex", workstream_id, discovered)
        return output_file.read_text(encoding="utf-8")
    finally:
        output_file.unlink(missing_ok=True)


def probe(runtime: str, workspace: Path, task_file: Path) -> int:
    """Quickly decide whether this task belongs to an isolated session."""
    if runtime not in {"claude", "codex"}:
        return UNHANDLED
    try:
        task_file = task_file.resolve(strict=True)
        tasks_dir = (workspace / "tasks").resolve(strict=True)
    except OSError:
        return UNHANDLED
    if task_file.parent != tasks_dir or task_file.suffix != ".txt":
        return UNHANDLED
    workstream_id = resolve_workstream(workspace, task_file)
    if not workstream_id:
        return UNHANDLED
    return 0


def handle(runtime: str, workspace: Path, task_file: Path, results_dir: Path, repo: Path) -> int:
    if probe(runtime, workspace, task_file) != 0:
        return UNHANDLED
    task_file = task_file.resolve()
    workstream_id = resolve_workstream(workspace, task_file)
    assert workstream_id is not None
    result_path = results_dir / task_file.name
    if _completed_result_exists(results_dir, task_file.name):
        return 0
    lock_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{runtime}-{workstream_id}")[:180]
    try:
        with _locked(workspace / "state" / "task-workstream-session-locks" / f"{lock_name}.lock"):
            if _completed_result_exists(results_dir, task_file.name):
                return 0
            body = (
                _run_claude(workspace, workstream_id, _prompt(task_file), repo)
                if runtime == "claude"
                else _run_codex(workspace, workstream_id, _prompt(task_file), repo)
            )
            if not body.strip():
                raise RuntimeError(f"{runtime} returned an empty result")
            _publish_result(result_path, body)
            return 0
    except Exception as exc:
        print(f"workstream session worker: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--results-dir", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.probe:
        return probe(args.runtime, args.workspace, args.task_file)
    return handle(args.runtime, args.workspace, args.task_file, args.results_dir, args.repo)


if __name__ == "__main__":
    raise SystemExit(main())
