#!/usr/bin/env python3
"""Run opted-in AG2 Space owner rooms in durable provider sessions."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


UNHANDLED = 3
SCHEMA_VERSION = 1
SESSION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

REPO_ROOT = next(
    (parent for parent in Path(__file__).resolve().parents
     if (parent / "src" / "local_task_protocol.py").is_file()),
    None,
)
if REPO_ROOT is None:
    raise SystemExit("session-worker: Sutando checkout not found")
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from local_task_protocol import find_result, parse_task_headers_trusted
from result_ready import read_ready_result
from team_result_guard import resolve_access_tier


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
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


def _publish_once(path: Path, body: str) -> bool:
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
            return read_ready_result(path) is not None
        return True
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _completed_result_exists(results_dir: Path, filename: str) -> bool:
    found = find_result(results_dir, Path(filename).stem)
    return found is not None and read_ready_result(found) is not None


def resolve_room_key(task_file: Path) -> Optional[str]:
    if resolve_access_tier(task_file) != "owner":
        return None
    try:
        content = task_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    headers = parse_task_headers_trusted(content).headers
    if (headers.get("source") or "").lower() != "ag2space":
        return None
    if headers.get("session_scope") != "room":
        return None
    room_id = headers.get("channel_id") or ""
    if not re.fullmatch(r"!\S{1,400}", room_id):
        return None
    return hashlib.sha256(room_id.encode("utf-8")).hexdigest()


def _state_path(workspace: Path) -> Path:
    return workspace / "state" / "ag2space-room-sessions.json"


def _session_id(workspace: Path, runtime: str, room_key: str) -> tuple[str, bool]:
    with _locked(workspace / "state" / "ag2space-room-sessions.lock"):
        state = _read_json(_state_path(workspace))
        if state.get("schema_version") != SCHEMA_VERSION:
            state = {"schema_version": SCHEMA_VERSION, "sessions": {}}
        row = ((state.get("sessions") or {}).get(runtime) or {}).get(room_key)
        if isinstance(row, dict) and SESSION_ID.fullmatch(str(row.get("session_id") or "")):
            return str(row["session_id"]), False
        return str(uuid.uuid4()), True


def _record_session(workspace: Path, runtime: str, room_key: str, session_id: str) -> None:
    if not SESSION_ID.fullmatch(session_id):
        raise ValueError(f"{runtime} returned an invalid session id")
    with _locked(workspace / "state" / "ag2space-room-sessions.lock"):
        state = _read_json(_state_path(workspace))
        if state.get("schema_version") != SCHEMA_VERSION:
            state = {"schema_version": SCHEMA_VERSION, "sessions": {}}
        sessions = state.setdefault("sessions", {}).setdefault(runtime, {})
        previous = sessions.get(room_key)
        now = datetime.now(timezone.utc).isoformat()
        sessions[room_key] = {
            "session_id": session_id,
            "created_at": previous.get("created_at", now) if isinstance(previous, dict) else now,
            "updated_at": now,
        }
        _atomic_text(_state_path(workspace), json.dumps(state, indent=2, sort_keys=True) + "\n")


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def _manifest_config(key: str) -> str:
    try:
        manifest = json.loads((Path(__file__).resolve().parents[1] / "manifest.json").read_text())
        value = (manifest.get("config") or {}).get(key)
        return value if isinstance(value, str) else ""
    except (OSError, TypeError, ValueError):
        return ""


def _timeout(key: str, cli_value: Optional[float]) -> float:
    raw = cli_value if cli_value is not None else os.environ.get(key, _manifest_config(key))
    value = float(raw)
    if value <= 0:
        raise ValueError("provider timeouts must be positive")
    return value


def _run_bounded(
    command: list[str],
    cwd: Path,
    hard_timeout_override: Optional[float] = None,
    stall_timeout_override: Optional[float] = None,
    heartbeat_interval_override: Optional[float] = None,
    on_heartbeat: Optional[Callable[[float], None]] = None,
) -> tuple[int, str, str]:
    # hard_timeout is a safety ceiling, not a completion boundary — this runs off the
    # watcher's clock. stall_timeout (no output at all) is the real "stuck" signal.
    hard_timeout = _timeout("SUTANDO_TIER_HARD_TIMEOUT", hard_timeout_override)
    stall_timeout = _timeout("SUTANDO_TIER_STALL_TIMEOUT", stall_timeout_override)
    try:
        heartbeat_interval = _timeout("SUTANDO_TIER_HEARTBEAT_INTERVAL", heartbeat_interval_override)
    except ValueError:
        heartbeat_interval = 0.0
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    streams = {process.stdout.fileno(): "stdout", process.stderr.fileno(): "stderr"}
    selector = selectors.DefaultSelector()
    output = {"stdout": [], "stderr": []}
    for fd, name in streams.items():
        os.set_blocking(fd, False)
        selector.register(fd, selectors.EVENT_READ, name)
    started = last_progress = time.monotonic()
    next_heartbeat = started + heartbeat_interval if heartbeat_interval > 0 else None

    def _maybe_heartbeat(now: float) -> None:
        nonlocal next_heartbeat
        if next_heartbeat is None or now < next_heartbeat or on_heartbeat is None:
            return
        # Skip ahead past any interval(s) a slow select()/sleep() overran, rather than
        # firing a burst of catch-up pings once the loop resumes.
        while next_heartbeat is not None and now >= next_heartbeat:
            next_heartbeat += heartbeat_interval
        # Off-thread: on_heartbeat can block (_notify's subprocess call has its own
        # 15s timeout) and must never delay this loop's own deadline checks.
        threading.Thread(target=on_heartbeat, args=(now - started,), daemon=True).start()

    try:
        while selector.get_map():
            now = time.monotonic()
            if now - started >= hard_timeout:
                raise TimeoutError(f"provider exceeded hard timeout ({hard_timeout:g}s)")
            if now - last_progress >= stall_timeout:
                raise TimeoutError(f"provider made no progress for {stall_timeout:g}s")
            _maybe_heartbeat(now)
            for key, _ in selector.select(timeout=min(0.2, stall_timeout)):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if chunk:
                    output[key.data].append(chunk)
                    last_progress = time.monotonic()
                else:
                    selector.unregister(key.fd)
        while process.poll() is None:
            now = time.monotonic()
            if now - started >= hard_timeout:
                raise TimeoutError(f"provider exceeded hard timeout ({hard_timeout:g}s)")
            if now - last_progress >= stall_timeout:
                raise TimeoutError(f"provider made no progress for {stall_timeout:g}s")
            _maybe_heartbeat(now)
            time.sleep(min(0.05, stall_timeout))
        return (
            int(process.returncode or 0),
            b"".join(output["stdout"]).decode("utf-8", "replace"),
            b"".join(output["stderr"]).decode("utf-8", "replace"),
        )
    except BaseException:
        _terminate(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _task_view(task_file: Path) -> str:
    parsed = parse_task_headers_trusted(task_file.read_text(encoding="utf-8", errors="replace"))
    body = parsed.body.rstrip()
    task_id = parsed.headers.get("id") or task_file.stem
    publication = f"Process and write the result to results/{task_id}.txt"
    body = "\n".join(
        line for line in body.splitlines()
        if not re.fullmatch(rf"\d+\. {re.escape(publication)}", line.strip())
    ).rstrip()
    context = {
        key: parsed.headers[key]
        for key in (
            "source", "channel_id", "room_name", "sender_name", "reply_to_sender",
            "addressed_to",
        )
        if parsed.headers.get(key)
    }
    return json.dumps({"context": context, "task": body}, ensure_ascii=False)


def _prompt(task_file: Path) -> str:
    return (
        "Handle the owner task in this persistent AG2 Space room session. Follow "
        "AGENTS.md for repository and safety policy. Do not read the original task "
        "file or create or modify any tasks/results tracking file; the parent worker "
        "exclusively owns result publication. Return only the exact result body.\n\n"
        f"Trusted task view:\n{_task_view(task_file)}"
    )


def _working_dir(repo: Path) -> Path:
    return Path(os.environ.get("SUTANDO_ISOLATED_WORKING_DIR", str(repo))).expanduser()


def _notify(task_file: Path, message: str) -> None:
    """Best-effort progress ping via the shared task-progress notifier — the same
    delivery path the NOTIFY FIRST convention already uses everywhere else in this
    codebase. Never raises: a broken or slow notify path only costs visibility."""
    try:
        headers = parse_task_headers_trusted(
            task_file.read_text(encoding="utf-8", errors="replace")
        ).headers
    except OSError:
        return
    source = headers.get("source") or ""
    channel_id = headers.get("channel_id") or ""
    if not source or not channel_id:
        return
    notifier = REPO_ROOT / "skills" / "task-progress" / "scripts" / "notify.py"
    if not notifier.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(notifier), "--source", source, "--channel-id", channel_id,
             "--message", message],
            timeout=15, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _claim_path(workspace: Path, runtime: str, room_key: str) -> Path:
    return workspace / "state" / "ag2space-room-session-locks" / f"{runtime}-{room_key}.active"


def _claim_is_live(path: Path) -> bool:
    """Informational only — the flock in run_detached() is what actually prevents two
    workers touching the same provider session; a crashed holder's flock releases on
    its own when the OS closes that process's file descriptors. This just answers
    'should the ack say queued or working' and 'did the previous attempt crash', so a
    wrong answer here costs a slightly-off status message, never correctness."""
    data = _read_json(path)
    pid = data.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_claim(path: Path, task_id: str) -> None:
    _atomic_text(path, json.dumps({
        "pid": os.getpid(),
        "task_id": task_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }))


def _clear_claim(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _claude_command(session_id: str, resume: bool, prompt: str) -> list[str]:
    command = ["claude", "-p", "--resume" if resume else "--session-id", session_id]
    command += ["--output-format", "text", "--dangerously-skip-permissions", "--add-dir", str(Path.home())]
    model = os.environ.get("SUTANDO_CORE_MODEL", "").strip()
    settings = os.environ.get("SUTANDO_ISOLATED_CLAUDE_SETTINGS", "").strip()
    if model:
        command += ["--model", model]
    if settings:
        command += ["--settings", settings]
    return command + ["--", prompt]


def _codex_command(session_id: Optional[str], prompt: str, output_file: Path, repo: Path) -> list[str]:
    model = os.environ.get("SUTANDO_CORE_MODEL", "").strip()
    if session_id:
        command = [
            "codex", "--search", "exec", "resume", "--json", "-o", str(output_file),
            "--dangerously-bypass-approvals-and-sandbox",
        ]
    else:
        command = [
            "codex", "--search", "exec", "--json", "-o", str(output_file),
            "-C", str(_working_dir(repo)), "--add-dir", str(Path.home()),
            "--dangerously-bypass-approvals-and-sandbox",
        ]
    if model:
        command += ["-m", model]
    return command + ([session_id, prompt] if session_id else [prompt])


def _run_claude(
    workspace: Path,
    room_key: str,
    prompt: str,
    repo: Path,
    hard_timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
    on_heartbeat: Optional[Callable[[float], None]] = None,
) -> str:
    session_id, created = _session_id(workspace, "claude", room_key)
    return_code, stdout, stderr = _run_bounded(
        _claude_command(session_id, not created, prompt),
        _working_dir(repo),
        hard_timeout,
        stall_timeout,
        on_heartbeat=on_heartbeat,
    )
    if return_code:
        raise RuntimeError(stderr.strip() or f"claude exited {return_code}")
    if created:
        _record_session(workspace, "claude", room_key, session_id)
    return stdout


def _run_codex(
    workspace: Path,
    room_key: str,
    prompt: str,
    repo: Path,
    hard_timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
    on_heartbeat: Optional[Callable[[float], None]] = None,
) -> str:
    state = _read_json(_state_path(workspace))
    row = ((state.get("sessions") or {}).get("codex") or {}).get(room_key)
    session_id = str(row.get("session_id") or "") if isinstance(row, dict) else ""
    if session_id and not SESSION_ID.fullmatch(session_id):
        session_id = ""
    (workspace / "state").mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".room-result.", suffix=".txt", dir=workspace / "state")
    os.close(fd)
    output_file = Path(name)
    try:
        return_code, stdout, stderr = _run_bounded(
            _codex_command(session_id or None, prompt, output_file, repo),
            _working_dir(repo),
            hard_timeout,
            stall_timeout,
            on_heartbeat=on_heartbeat,
        )
        if return_code:
            raise RuntimeError(stderr.strip() or f"codex exited {return_code}")
        discovered = ""
        if not session_id:
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if event.get("type") == "thread.started":
                    candidate = str(event.get("thread_id") or "")
                    if SESSION_ID.fullmatch(candidate):
                        discovered = candidate
        if not session_id and not discovered:
            raise RuntimeError("codex did not report a valid thread.started session id")
        if discovered:
            _record_session(workspace, "codex", room_key, discovered)
        return output_file.read_text(encoding="utf-8")
    finally:
        output_file.unlink(missing_ok=True)


def probe(runtime: str, workspace: Path, task_file: Path) -> int:
    if runtime not in {"claude", "codex"}:
        return UNHANDLED
    try:
        task_file = task_file.resolve(strict=True)
        tasks_dir = (workspace / "tasks").resolve(strict=True)
    except OSError:
        return UNHANDLED
    if task_file.parent != tasks_dir or task_file.suffix != ".txt":
        return UNHANDLED
    return 0 if resolve_room_key(task_file) else UNHANDLED


def run_detached(
    runtime: str,
    workspace: Path,
    task_file: Path,
    results_dir: Path,
    repo: Path,
    hard_timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
) -> None:
    """The actual provider run, off the watcher's request/response cycle entirely —
    invoked only via `--run-detached` in a process `handle()` spawned and did not
    wait for. A long-but-progressing run is expected here; nothing upstream is
    blocked on this function returning."""
    room_key = resolve_room_key(task_file)
    if room_key is None or _completed_result_exists(results_dir, task_file.name):
        return
    lock = workspace / "state" / "ag2space-room-session-locks" / f"{runtime}-{room_key}.lock"
    claim = _claim_path(workspace, runtime, room_key)
    try:
        with _locked(lock):
            if _completed_result_exists(results_dir, task_file.name):
                return
            _write_claim(claim, task_file.stem)
            _notify(task_file, "On it — working on this now.")
            heartbeat = lambda elapsed: _notify(  # noqa: E731
                task_file, f"Still working ({elapsed:.0f}s so far)…"
            )
            try:
                body = (
                    _run_claude(workspace, room_key, _prompt(task_file), repo,
                                hard_timeout, stall_timeout, on_heartbeat=heartbeat)
                    if runtime == "claude"
                    else _run_codex(workspace, room_key, _prompt(task_file), repo,
                                     hard_timeout, stall_timeout, on_heartbeat=heartbeat)
                )
                if not body.strip():
                    raise RuntimeError(f"{runtime} returned an empty result")
            except Exception as exc:
                # No watcher call left to fall back to (it was already told "0,
                # handled") — publish an honest failure rather than go silent.
                print(f"AG2 Space room-session worker (detached): {exc}", file=sys.stderr)
                _publish_once(
                    results_dir / task_file.name,
                    "This room session hit an error and couldn't complete: "
                    f"{exc}\n\nResend your message for a fresh attempt.",
                )
                return
            if not _publish_once(results_dir / task_file.name, body):
                # Lost the publish race — never clobber; make the loss visible instead.
                print(
                    f"AG2 Space room-session worker (detached): {runtime} completed "
                    "but lost the publish race for this task's result path",
                    file=sys.stderr,
                )
    finally:
        _clear_claim(claim)


def _spawn_detached(
    runtime: str,
    workspace: Path,
    task_file: Path,
    results_dir: Path,
    repo: Path,
    hard_timeout: Optional[float],
    stall_timeout: Optional[float],
) -> None:
    logs = workspace / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"ag2space-room-session-{task_file.stem}.log"
    command = [
        sys.executable, str(Path(__file__).resolve()), "--run-detached",
        "--runtime", runtime, "--workspace", str(workspace),
        "--task-file", str(task_file), "--results-dir", str(results_dir), "--repo", str(repo),
    ]
    if hard_timeout is not None:
        command += ["--hard-timeout", str(hard_timeout)]
    if stall_timeout is not None:
        command += ["--stall-timeout", str(stall_timeout)]
    with log_path.open("a", encoding="utf-8") as log_file:
        # start_new_session=True (setsid) survives the parent exiting; not waited
        # on — that's the entire point of detaching.
        subprocess.Popen(
            command, cwd=repo, env=os.environ.copy(),
            stdin=subprocess.DEVNULL, stdout=log_file, stderr=log_file,
            start_new_session=True,
        )


def handle(
    runtime: str,
    workspace: Path,
    task_file: Path,
    results_dir: Path,
    repo: Path,
    hard_timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
    spawn: Optional[Callable[..., None]] = None,
) -> int:
    """Fast foreground path only: validate, ack, hand off, return — never runs the
    provider inline. The actual work happens in run_detached(), off this call
    entirely, so a long-running room turn never occupies a watcher worker slot."""
    if probe(runtime, workspace, task_file) != 0:
        return UNHANDLED
    task_file = task_file.resolve()
    room_key = resolve_room_key(task_file)
    assert room_key is not None
    if _completed_result_exists(results_dir, task_file.name):
        return 0
    claim = _claim_path(workspace, runtime, room_key)
    if _claim_is_live(claim):
        _notify(task_file, "Queued behind an earlier message in this room — "
                            "you'll get a reply as soon as it's your turn.")
    else:
        _notify(task_file, "On it — working on this now.")
    launcher = spawn or _spawn_detached
    try:
        launcher(runtime, workspace, task_file, results_dir, repo, hard_timeout, stall_timeout)
    except OSError as exc:
        print(f"AG2 Space room-session worker: failed to start detached worker: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--results-dir", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--hard-timeout", type=float)
    parser.add_argument("--stall-timeout", type=float)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--run-detached", action="store_true")
    args = parser.parse_args()
    if args.probe:
        return probe(args.runtime, args.workspace, args.task_file)
    if args.run_detached:
        run_detached(
            args.runtime, args.workspace, args.task_file, args.results_dir, args.repo,
            args.hard_timeout, args.stall_timeout,
        )
        return 0
    return handle(
        args.runtime,
        args.workspace,
        args.task_file,
        args.results_dir,
        args.repo,
        args.hard_timeout,
        args.stall_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
