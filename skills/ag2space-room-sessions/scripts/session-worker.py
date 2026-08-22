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


def _pane_alive(workspace: Path, name: str) -> Optional[bool]:
    """True/False = tmux answered. None = the probe itself failed (socket error or
    timeout) — callers must treat None as unknown, never as a confirmed exit."""
    try:
        done = _tmux(workspace, "has-session", "-t", f"={name}")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.returncode == 0


def _pane_dead(workspace: Path, name: str) -> bool:
    try:
        done = _tmux(workspace, "display-message", "-p", "-t", f"={name}:", "#{pane_dead}")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and done.stdout.strip() == "1"


def _pane_content(workspace: Path, name: str) -> str:
    try:
        # "=name:" = exact-match session, its active pane — pane-targeting verbs
        # (capture-pane, send-keys) reject the bare "=name" session form.
        done = _tmux(workspace, "capture-pane", "-p", "-t", f"={name}:")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _standing_launch_command(runtime: str, session_id: str, resume: bool) -> list[str]:
    model = os.environ.get("SUTANDO_CORE_MODEL", "").strip()
    if runtime == "codex":
        # Codex chooses its own session id at first turn (discovered from the
        # rollout file afterwards), so create-mode launches with no id at all.
        command = ["codex", "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            command += ["-m", model]
        if resume:
            command += ["resume", session_id]
        return command
    command = ["claude", "--resume" if resume else "--session-id", session_id,
               "--dangerously-skip-permissions", "--add-dir", str(Path.home())]
    settings = os.environ.get("SUTANDO_ISOLATED_CLAUDE_SETTINGS", "").strip()
    if model:
        command += ["--model", model]
    if settings:
        command += ["--settings", settings]
    return command


def _codex_sessions_root() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "sessions"


def _discover_codex_session(since: float, expect_cwd: Path) -> Optional[str]:
    """The codex TUI names its own session: a rollout-<ts>-<uuid>.jsonl appears
    under the sessions tree at the FIRST turn, its meta line carrying the cwd."""
    candidates = []
    root = _codex_sessions_root()
    if not root.is_dir():
        return None
    for path in root.rglob("rollout-*.jsonl"):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        matched = re.search(r"rollout-.*-([0-9a-f-]{36})\.jsonl$", path.name, re.IGNORECASE)
        if not matched or not SESSION_ID.fullmatch(matched.group(1)):
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                meta = json.loads(handle.readline() or "{}")
        except (OSError, ValueError):
            continue
        # cwd filter: other codex runs (e.g. exec) also write rollouts; only a
        # session started in OUR working dir can be this room's pane.
        cwd = str((meta.get("payload") or {}).get("cwd") or meta.get("cwd") or "")
        # Resolve BOTH sides: macOS reports /var/... and /private/var/... for the
        # same directory depending on who recorded it.
        if cwd and Path(cwd).resolve() != expect_cwd.resolve():
            continue
        candidates.append((path.stat().st_mtime, matched.group(1)))
    if not candidates:
        return None
    return max(candidates)[1]


def _startup_failure(workspace: Path, name: str, reason: str) -> RuntimeError:
    # Post-mortem before cleanup: remain-on-exit kept the dying pane, so its last
    # screen is the actual diagnostic (auth error, traceback, bad flag...).
    tail = "\n".join(line for line in _pane_content(workspace, name).splitlines() if line.strip())[-500:]
    _tmux(workspace, "kill-session", "-t", f"={name}")
    return RuntimeError(f"standing session {reason} during startup"
                        + (f"; last pane output:\n{tail}" if tail else ""))


def _spawn_marker(workspace: Path, name: str) -> Path:
    return _spool_dir(workspace) / f"{name}.spawned"


def _ensure_standing_session(workspace: Path, runtime: str, room_key: str, repo: Path) -> str:
    """Ensure the room's standing provider pane exists and proved itself with
    output; a respawn after crash/reboot resumes the recorded provider session id."""
    name = _pane_name(runtime, room_key)
    if _pane_alive(workspace, name) is True:
        return name
    session_id, created = _session_id(workspace, runtime, room_key)
    done = _tmux(workspace, "new-session", "-d", "-s", name, "-c", str(_working_dir(repo)),
                 *_standing_launch_command(runtime, session_id, resume=not created))
    if done.returncode != 0:
        raise RuntimeError(f"tmux new-session failed: {done.stderr.strip()}")
    # Keep a dying startup pane around for post-mortem capture (turned off again
    # once startup is confirmed, so a later natural exit still cleans up).
    _tmux(workspace, "set-option", "-w", "-t", f"={name}:", "remain-on-exit", "on")
    trust_answered = False
    deadline = time.monotonic() + _timeout("SUTANDO_ROOM_SPAWN_WAIT", None)
    while time.monotonic() < deadline:
        if _pane_dead(workspace, name) or _pane_alive(workspace, name) is False:
            raise _startup_failure(workspace, name, "exited")
        content = _pane_content(workspace, name)
        if not trust_answered and "Do you trust the contents of this directory" in content:
            # Codex's first-use-in-a-directory dialog; Enter selects "Yes,
            # continue" (verified live on 0.149.0). Answered at most once.
            _tmux(workspace, "send-keys", "-t", f"={name}:", "Enter")
            trust_answered = True
            time.sleep(0.5)
            continue
        if content.strip():
            # Record only after the pane proves itself: an earlier record poisons
            # every later --resume with a session that may never have existed.
            if created and runtime == "claude":
                _record_session(workspace, runtime, room_key, session_id)
            if created and runtime == "codex":
                # Codex's self-chosen id is discovered by the monitor from the
                # rollout file the FIRST TURN creates; the marker bounds the scan.
                _atomic_text(_spawn_marker(workspace, name), f"{time.time():.3f}\n")
            _tmux(workspace, "set-option", "-w", "-t", f"={name}:", "remain-on-exit", "off")
            return name
        time.sleep(0.1)
    raise _startup_failure(workspace, name, "produced no output")


def _spool_dir(workspace: Path) -> Path:
    return workspace / "state" / "ag2space-room-sessions" / "spool"


def _out_path(workspace: Path, task_id: str) -> Path:
    return _spool_dir(workspace) / f"{task_id}.out.txt"


def _write_spool_prompt(workspace: Path, task_file: Path) -> Path:
    # The session writes only its PRIVATE output file; the trusted monitor is the
    # sole publisher of the shared results path (validated + atomic, no clobber).
    task_id = task_file.stem
    prompt = (
        "Handle the owner task below in this persistent AG2 Space room session. Follow "
        "AGENTS.md for repository and safety policy, and keep this room's conversation "
        "context across tasks. Do not read the original task file and do not create or "
        "modify any tasks/results tracking file; the trusted monitor owns delivery. "
        "When done, write the exact result body - nothing else - to "
        f"{_out_path(workspace, task_id)} in a single write.\n\n"
        f"Trusted task view:\n{_task_view(task_file)}\n"
    )
    path = _spool_dir(workspace) / f"{task_id}.prompt.txt"
    _atomic_text(path, prompt)
    return path


def _stable_output(path: Path, delay: float = 0.5) -> Optional[str]:
    """The provider's private out-file, but only once it is non-empty and stops
    changing — an ordinary write is not atomic and must never be published mid-way."""
    try:
        first = path.read_bytes()
    except OSError:
        return None
    if not first.strip():
        return None
    time.sleep(delay)
    try:
        second = path.read_bytes()
    except OSError:
        return None
    if first != second:
        return None
    return second.decode("utf-8", "replace")


def _inject(workspace: Path, name: str, line: str) -> None:
    for keys in (["-l", line], ["Enter"]):
        done = _tmux(workspace, "send-keys", "-t", f"={name}:", *keys)
        if done.returncode != 0:
            raise RuntimeError(f"tmux send-keys failed: {done.stderr.strip()}")
        # Paste-guard: an Enter arriving in the same burst as the text is folded
        # into the composer, not submitted (verified live on the codex TUI).
        time.sleep(0.5)


def _fail(task_file: Path, result: Path, reason: str) -> None:
    _publish_once(
        result,
        f"This room session couldn't complete this message: {reason}.\n\n"
        "Resend your message to continue - the conversation itself is preserved.",
    )
    _notify(task_file, f"Hit a problem: {reason}. Resend to continue.")


def _settle(task_file: Path, result: Path, out: Path, reason: str) -> None:
    # Terminal disposition: a finished private out-file beats the failure verdict
    # (the turn may have completed right before the pane died or was killed).
    body = _stable_output(out)
    if body is not None:
        _publish_once(result, body)
        return
    _fail(task_file, result, reason)


def monitor(
    runtime: str,
    workspace: Path,
    task_file: Path,
    results_dir: Path,
    repo: Path = REPO_ROOT,
    hard_timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
) -> None:
    """Sole publisher of this task's shared result, in a detached process: delivers
    the session's validated private output, heartbeats, stall/exit supervision.
    Never abandons ownership before a terminal state - result or failure."""
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
    ceiling_notified = False
    fingerprint = ""
    result = results_dir / task_file.name
    out = _out_path(workspace, task_file.stem)
    marker = _spawn_marker(workspace, name)
    while True:
        if marker.is_file():
            # Codex-create: the first turn just named its own session; persist it
            # so the NEXT respawn can resume, then retire the marker.
            try:
                since = float(marker.read_text().strip())
            except (OSError, ValueError):
                since = started
            discovered = _discover_codex_session(since, _working_dir(repo))
            if discovered is not None:
                _record_session(workspace, runtime, room_key, discovered)
                marker.unlink(missing_ok=True)
        if read_ready_result(result) is not None:
            return
        body = _stable_output(out)
        if body is not None:
            _publish_once(result, body)
            return
        now = time.monotonic()
        alive = _pane_alive(workspace, name)
        if alive is False:
            time.sleep(2)  # grace: a final out-file write may still be landing
            _settle(task_file, result, out, "the room's standing session exited before finishing")
            return
        # alive None = probe failure, NOT a confirmed exit: fall through so it
        # degrades into stall handling (content unreadable -> clock keeps running).
        content = _pane_content(workspace, name) if alive else ""
        if content and content != fingerprint:
            fingerprint = content
            last_change = now
        if now - last_change >= stall:
            # Fence first: kill the pane BEFORE the terminal verdict so nothing can
            # keep writing the out-file while we decide result-vs-failure.
            _tmux(workspace, "kill-session", "-t", f"={name}")
            _settle(task_file, result, out,
                    f"the room's standing session froze for {stall:g}s and was stopped; "
                    "the next message resumes the conversation")
            return
        if not ceiling_notified and now - started >= hard:
            # The ceiling changes the message, never the ownership: supervision
            # continues to a terminal state, the work is never abandoned or killed.
            ceiling_notified = True
            threading.Thread(
                target=_notify,
                args=(task_file, f"This turn passed the {hard:g}s safety ceiling and is "
                                 "still running - the result will follow when it completes."),
                daemon=True,
            ).start()
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
    lock = workspace / "state" / "ag2space-room-session-locks" / f"{runtime}-{room_key}.lock"
    try:
        # The lock guards only spawn+inject ordering (seconds), never a whole
        # provider turn - the standing session serializes its own turns natively.
        with _locked(lock):
            if _completed_result_exists(results_dir, task_file.name):
                return 0
            name = _ensure_standing_session(workspace, runtime, room_key, repo)
            spool = _write_spool_prompt(workspace, task_file)
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
                args.repo, args.hard_timeout, args.stall_timeout)
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
