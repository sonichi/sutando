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
from typing import Optional


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
) -> tuple[int, str, str]:
    hard_timeout = _timeout("SUTANDO_TIER_HARD_TIMEOUT", hard_timeout_override)
    stall_timeout = _timeout("SUTANDO_TIER_STALL_TIMEOUT", stall_timeout_override)
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
    try:
        while selector.get_map():
            now = time.monotonic()
            if now - started >= hard_timeout:
                raise TimeoutError(f"provider exceeded hard timeout ({hard_timeout:g}s)")
            if now - last_progress >= stall_timeout:
                raise TimeoutError(f"provider made no progress for {stall_timeout:g}s")
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
    """Best-effort progress ping via the shared task-progress notifier. Never
    raises: a broken or slow notify path only costs visibility."""
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


def _tmux_socket(workspace: Path) -> Path:
    # AF_UNIX paths cap at ~104 bytes on macOS, so the socket lives in a short
    # per-user dir keyed by a hash of the workspace, never inside the workspace.
    base = Path(os.environ.get("SUTANDO_ROOM_TMUX_DIR") or f"/tmp/sutando-room-tmux-{os.getuid()}")
    digest = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:12]
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base / f"{digest}.sock"


def _tmux(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    # TMUX is dropped so this works from inside the core's own tmux session —
    # with it set, tmux refuses to start nested sessions even on another socket.
    env = os.environ.copy()
    env.pop("TMUX", None)
    return subprocess.run(
        ["tmux", "-S", str(_tmux_socket(workspace)), *args],
        capture_output=True, text=True, timeout=15, env=env,
    )


def _pane_name(runtime: str, room_key: str) -> str:
    return f"room-{runtime}-{room_key[:12]}"


def _pane_alive(workspace: Path, name: str) -> bool:
    try:
        return _tmux(workspace, "has-session", "-t", f"={name}").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _pane_content(workspace: Path, name: str) -> str:
    try:
        # "=name:" = exact-match session, its active pane — pane-targeting verbs
        # (capture-pane, send-keys) reject the bare "=name" session form.
        done = _tmux(workspace, "capture-pane", "-p", "-t", f"={name}:")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _standing_launch_command(session_id: str, resume: bool) -> list[str]:
    command = ["claude", "--resume" if resume else "--session-id", session_id,
               "--dangerously-skip-permissions", "--add-dir", str(Path.home())]
    model = os.environ.get("SUTANDO_CORE_MODEL", "").strip()
    settings = os.environ.get("SUTANDO_ISOLATED_CLAUDE_SETTINGS", "").strip()
    if model:
        command += ["--model", model]
    if settings:
        command += ["--settings", settings]
    return command


def _ensure_standing_session(workspace: Path, runtime: str, room_key: str, repo: Path) -> str:
    """Ensure the room's standing provider pane exists and answered with output;
    a respawn after crash/reboot resumes the recorded provider session id."""
    name = _pane_name(runtime, room_key)
    if _pane_alive(workspace, name):
        return name
    session_id, created = _session_id(workspace, runtime, room_key)
    done = _tmux(workspace, "new-session", "-d", "-s", name, "-c", str(_working_dir(repo)),
                 *_standing_launch_command(session_id, resume=not created))
    if done.returncode != 0:
        raise RuntimeError(f"tmux new-session failed: {done.stderr.strip()}")
    if created:
        _record_session(workspace, runtime, room_key, session_id)
    deadline = time.monotonic() + _timeout("SUTANDO_ROOM_SPAWN_WAIT", None)
    while time.monotonic() < deadline:
        if _pane_content(workspace, name).strip():
            return name
        if not _pane_alive(workspace, name):
            raise RuntimeError("standing session exited during startup")
        time.sleep(0.1)
    raise RuntimeError("standing session produced no output during startup")


def _spool_dir(workspace: Path) -> Path:
    return workspace / "state" / "ag2space-room-sessions" / "spool"


def _write_spool_prompt(workspace: Path, task_file: Path, results_dir: Path) -> Path:
    # Publication ownership flips here versus the inline path: the standing
    # session writes the result itself, so the view must name the result path.
    task_id = task_file.stem
    prompt = (
        "Handle the owner task below in this persistent AG2 Space room session. Follow "
        "AGENTS.md for repository and safety policy, and keep this room's conversation "
        "context across tasks. Do not read the original task file and do not touch any "
        "other tasks/results tracking file. When done, write the exact result body - "
        f"nothing else - to {results_dir / (task_id + '.txt')} in a single write.\n\n"
        f"Trusted task view:\n{_task_view(task_file)}\n"
    )
    path = _spool_dir(workspace) / f"{task_id}.prompt.txt"
    _atomic_text(path, prompt)
    return path


def _inject(workspace: Path, name: str, line: str) -> None:
    for keys in (["-l", line], ["Enter"]):
        done = _tmux(workspace, "send-keys", "-t", f"={name}:", *keys)
        if done.returncode != 0:
            raise RuntimeError(f"tmux send-keys failed: {done.stderr.strip()}")


def _fail(task_file: Path, result: Path, reason: str) -> None:
    _publish_once(
        result,
        f"This room session couldn't complete this message: {reason}.\n\n"
        "Resend your message to continue - the conversation itself is preserved.",
    )
    _notify(task_file, f"Hit a problem: {reason}. Resend to continue.")


def monitor(
    runtime: str,
    workspace: Path,
    task_file: Path,
    results_dir: Path,
    hard_timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
) -> None:
    """Watchdog for one injected turn, in a detached process: heartbeats while it
    runs, stall detection on frozen pane content, honest failure publishing."""
    room_key = resolve_room_key(task_file)
    if room_key is None:
        return
    name = _pane_name(runtime, room_key)
    hard = _timeout("SUTANDO_TIER_HARD_TIMEOUT", hard_timeout)
    stall = _timeout("SUTANDO_TIER_STALL_TIMEOUT", stall_timeout)
    try:
        interval = _timeout("SUTANDO_TIER_HEARTBEAT_INTERVAL", None)
    except ValueError:
        interval = 0.0
    started = last_change = time.monotonic()
    next_heartbeat = started + interval if interval > 0 else None
    fingerprint = ""
    result = results_dir / task_file.name
    while True:
        if read_ready_result(result) is not None:
            return
        now = time.monotonic()
        if not _pane_alive(workspace, name):
            time.sleep(2)  # grace: a final result write may still be landing
            if read_ready_result(result) is None:
                _fail(task_file, result, "the room's standing session exited before finishing")
            return
        content = _pane_content(workspace, name)
        if content != fingerprint:
            fingerprint = content
            last_change = now
        if now - last_change >= stall:
            # A frozen pane is a hung process (a live provider at least animates its
            # spinner). Kill BEFORE publishing so the session can't write after us.
            _tmux(workspace, "kill-session", "-t", f"={name}")
            if read_ready_result(result) is None:
                _fail(task_file, result,
                      f"the room's standing session froze for {stall:g}s and was stopped; "
                      "the next message resumes the conversation")
            return
        if now - started >= hard:
            # Safety ceiling bounds the WATCHDOG, not the work: a still-active turn
            # is left running and its result still lands whenever it finishes.
            _notify(task_file,
                    f"This turn passed the {hard:g}s safety ceiling and is still running - "
                    "the result will follow whenever it completes.")
            return
        if next_heartbeat is not None and now >= next_heartbeat:
            while now >= next_heartbeat:
                next_heartbeat += interval
            # Off-thread: _notify can block up to 15s and must never delay the
            # stall/ceiling checks (the exact bug qingyun's reviewer caught in #3259).
            threading.Thread(
                target=_notify,
                args=(task_file, f"Still working ({now - started:.0f}s so far)..."),
                daemon=True,
            ).start()
        time.sleep(min(1.0, stall / 4))


def _spawn_monitor(
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
    command = [
        sys.executable, str(Path(__file__).resolve()), "--monitor",
        "--runtime", runtime, "--workspace", str(workspace),
        "--task-file", str(task_file), "--results-dir", str(results_dir), "--repo", str(repo),
    ]
    if hard_timeout is not None:
        command += ["--hard-timeout", str(hard_timeout)]
    if stall_timeout is not None:
        command += ["--stall-timeout", str(stall_timeout)]
    with (logs / f"ag2space-room-monitor-{task_file.stem}.log").open("a", encoding="utf-8") as log:
        # setsid + not waited on: the monitor outlives this short-lived handler.
        subprocess.Popen(
            command, cwd=repo, env=os.environ.copy(),
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,
        )


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


def _run_codex(
    workspace: Path,
    room_key: str,
    prompt: str,
    repo: Path,
    hard_timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
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


def _handle_inline_codex(
    workspace: Path,
    room_key: str,
    task_file: Path,
    results_dir: Path,
    repo: Path,
    hard_timeout: Optional[float],
    stall_timeout: Optional[float],
) -> int:
    """Per-message fallback executor (codex): the pre-standing-session design,
    kept until interactive codex resume-id discovery is verified live."""
    lock = workspace / "state" / "ag2space-room-session-locks" / f"codex-{room_key}.lock"
    try:
        with _locked(lock):
            if _completed_result_exists(results_dir, task_file.name):
                return 0
            body = _run_codex(workspace, room_key, _prompt(task_file), repo,
                              hard_timeout, stall_timeout)
            if not body.strip():
                raise RuntimeError("codex returned an empty result")
            if not _publish_once(results_dir / task_file.name, body):
                raise RuntimeError("result destination exists but is not ready")
        return 0
    except Exception as exc:
        print(f"AG2 Space room-session worker: {exc}", file=sys.stderr)
        return 1


def handle(
    runtime: str,
    workspace: Path,
    task_file: Path,
    results_dir: Path,
    repo: Path,
    hard_timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
) -> int:
    if probe(runtime, workspace, task_file) != 0:
        return UNHANDLED
    task_file = task_file.resolve()
    room_key = resolve_room_key(task_file)
    assert room_key is not None
    if _completed_result_exists(results_dir, task_file.name):
        return 0
    if runtime != "claude":
        return _handle_inline_codex(workspace, room_key, task_file, results_dir, repo,
                                    hard_timeout, stall_timeout)
    lock = workspace / "state" / "ag2space-room-session-locks" / f"{runtime}-{room_key}.lock"
    try:
        # The lock guards only spawn+inject ordering (seconds), never a whole
        # provider turn - the standing session serializes its own turns natively.
        with _locked(lock):
            if _completed_result_exists(results_dir, task_file.name):
                return 0
            name = _ensure_standing_session(workspace, runtime, room_key, repo)
            spool = _write_spool_prompt(workspace, task_file, results_dir)
            _inject(workspace, name, f'Read the file "{spool}" and do exactly what it says.')
        _spawn_monitor(runtime, workspace, task_file, results_dir, repo,
                       hard_timeout, stall_timeout)
        _notify(task_file, "On it - picked up in this room's standing session.")
        return 0
    except Exception as exc:
        print(f"AG2 Space room-session worker: {exc}", file=sys.stderr)
        return 1


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
    parser.add_argument("--monitor", action="store_true")
    args = parser.parse_args()
    if args.probe:
        return probe(args.runtime, args.workspace, args.task_file)
    if args.monitor:
        monitor(args.runtime, args.workspace, args.task_file, args.results_dir,
                args.hard_timeout, args.stall_timeout)
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
