#!/usr/bin/env python3
"""Re-deliver a task whose prompt an API-error turn consumed.

When an API error (a 502, an overload) ends a core turn, Claude Code fires
`StopFailure` instead of `Stop`, and the prompt the standby notifier typed is
gone. The notifier's in-flight marker still says "submitted, awaiting its
result", so without this the task waits out the whole completion timeout.

This module is the one owner of that recovery state and its policy:

* the `UserPromptSubmit` hook (`src/turn-start.sh`) records which task, if
  any, the turn's prompt delivered;
* the `StopFailure` hook (`src/stop-failure.sh`) records the failure against
  that turn's task;
* the `Stop` hook (`src/check-pending-tasks.sh`) ends the turn and records it
  as the recovery;
* the notifier asks `retry-due` whether an in-flight task should be re-typed,
  and `retry-note` counts each re-delivery.

State (per instance: `util_paths.turn_failure_path`):

    <state>/core-turn-failure/<key|core>.json
        {"failed_at", "error", "session_id", "task", "recovered_at"}
    <state>/core-turn-failure/<key|core>.turn.json
        {"task", "session_id", "started_at"}    -- the turn now running
    <state>/task-notifier-retry/<filename>.json
        {"attempts", "last_retry_at", "gave_up_at"}

Only transient errors are retryable (`RETRYABLE_ERRORS`). Authentication,
billing, account and request errors are not: re-typing cannot fix them and
would only spend turns, so those tasks keep the notifier's ordinary timeout.
`max_output_tokens` is excluded too -- the same prompt hits the same ceiling.

A failure counts against a task only when the failed turn is the one that
task's notifier prompt started (`TASK_PROMPT_PREFIX`) and it is NEWER than
the task's submission (the in-flight marker's mtime). A later turn -- another
prompt, a Monitor event, a turn after a successful Stop -- is not blamed. A successful turn newer than the
failure means the API is back: re-deliver now. Otherwise wait out the backoff
(`BACKOFF_SECONDS`, by attempts already made, capped at the last entry),
measured from the later of the failure and the previous re-delivery. After
`MAX_ATTEMPTS` re-deliveries of one task it stops: `retry-due` reports the
give-up once (exit 3) and the task keeps the ordinary completion timeout.

CLI:

    turn_failure.py hook-turn-start --state <state_dir>       # hook JSON on stdin; always exit 0
    turn_failure.py hook-stop-failure --state <state_dir>     # hook JSON on stdin; always exit 0
    turn_failure.py record-recovery --state <state_dir>        # ends the turn too; always exit 0
    turn_failure.py retry-due --state <state_dir> --inflight-dir <dir> --results-dir <dir> \\
        --payload <task_path> [--deliveries-dir <dir>] <filename>
        # exit 0 due (prints reason), 3 gave up just now (prints why), 1 not due
    turn_failure.py retry-note --state <state_dir> <filename>
    turn_failure.py retry-clear --state <state_dir> <filename>
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # lint-workspace-resolution: allow-repo-root

__all__ = [
    "RETRYABLE_ERRORS", "BACKOFF_SECONDS", "MAX_ATTEMPTS", "TASK_PROMPT_PREFIX", "backoff_delay",
    "decide_retry", "gives_up", "retry_verdict", "task_of_prompt", "record_turn_start", "read_turn", "end_turn",
    "record_failure", "record_recovery", "read_failure",
    "read_retry", "note_retry", "clear_retry", "retry_due",
]

RETRYABLE_ERRORS = frozenset({"server_error", "overloaded", "rate_limit", "unknown"})
BACKOFF_SECONDS = (30, 60, 120, 300, 600)
# Re-deliveries per task; past this the task keeps the notifier's ordinary completion timeout.
MAX_ATTEMPTS = 3
# How the notifier's task_prompt (agent/claude/cli/task-notifier.sh) begins; a test pins the two together.
TASK_PROMPT_PREFIX = "Sutando task ready: "

_USAGE = (
    "usage: turn_failure.py hook-turn-start --state DIR\n"
    "       turn_failure.py hook-stop-failure --state DIR\n"
    "       turn_failure.py record-recovery --state DIR\n"
    "       turn_failure.py retry-due --state DIR --inflight-dir DIR --results-dir DIR "
    "--payload PATH [--deliveries-dir DIR] FILENAME\n"
    "       turn_failure.py retry-note --state DIR FILENAME\n"
    "       turn_failure.py retry-clear --state DIR FILENAME"
)


def backoff_delay(attempts: int) -> int:
    """Seconds to wait before re-delivery number `attempts + 1`."""
    return BACKOFF_SECONDS[min(max(attempts, 0), len(BACKOFF_SECONDS) - 1)]


def task_of_prompt(prompt) -> "str | None":
    """The task filename a notifier prompt delivers, or None for any other prompt."""
    if not isinstance(prompt, str) or not prompt.startswith(TASK_PROMPT_PREFIX):
        return None
    words = prompt[len(TASK_PROMPT_PREFIX):].split(maxsplit=1)
    name = words[0].rstrip(".") if words else ""
    return name if name and "/" not in name and ".." not in name else None


def decide_retry(failure: "dict | None", filename: str, submitted_at: float, attempts: int,
                 last_retry_at: float, now: float) -> "str | None":
    """Why the in-flight task should be re-delivered now, or None to keep waiting.

    Pure: every input is a value, so the policy is testable without files.
    """
    error = _lost_to(failure, filename, submitted_at)
    if error is None or attempts >= MAX_ATTEMPTS:
        return None
    failed_at = failure["failed_at"]
    recovered_at = _num(failure.get("recovered_at"))
    if recovered_at is not None and recovered_at > failed_at:
        return f"a turn ended in {error} after the submit and a later turn succeeded"
    base = max(failed_at, last_retry_at or 0.0)
    delay = backoff_delay(attempts)
    if now - base >= delay:
        return f"a turn ended in {error} after the submit; backoff {delay}s elapsed (attempt {attempts + 1})"
    return None


def gives_up(failure: "dict | None", filename: str, submitted_at: float, attempts: int) -> bool:
    """The failed turn lost this task's prompt, but MAX_ATTEMPTS re-deliveries are spent."""
    return attempts >= MAX_ATTEMPTS and _lost_to(failure, filename, submitted_at) is not None


def _lost_to(failure: "dict | None", filename: str, submitted_at: float) -> "str | None":
    """The retryable error of a failed turn that this task's submitted prompt started, else None."""
    if not failure or failure.get("task") != filename:
        return None
    failed_at = _num(failure.get("failed_at"))
    if failed_at is None or failed_at <= submitted_at:
        return None
    error = failure.get("error")
    return error if error in RETRYABLE_ERRORS else None


def _num(v) -> "float | None":
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> "dict | None":
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _failure_path(state_dir) -> Path:
    from util_paths import turn_failure_path
    return turn_failure_path(state_dir)


def _turn_path(state_dir) -> Path:
    path = _failure_path(state_dir)
    return path.with_name(f"{path.stem}.turn.json")


def _retry_path(state_dir, filename: str) -> Path:
    if not filename or "/" in filename or ".." in filename:
        raise ValueError(f"not a task filename: {filename!r}")
    return Path(state_dir) / "task-notifier-retry" / f"{filename}.json"


def record_turn_start(state_dir, prompt, session_id: str = "", now: "float | None" = None) -> None:
    """A turn began from `prompt`: remember which task it delivers (None for any other prompt)."""
    _write_json(_turn_path(state_dir), {
        "task": task_of_prompt(prompt), "session_id": session_id,
        "started_at": time.time() if now is None else now,
    })


def read_turn(state_dir) -> "dict | None":
    return _read_json(_turn_path(state_dir))


def end_turn(state_dir) -> None:
    _turn_path(state_dir).unlink(missing_ok=True)


def record_failure(state_dir, error: str, session_id: str = "", now: "float | None" = None,
                   task: "str | None" = None) -> bool:
    """Record a retryable API-error turn and the task its prompt delivered; False for any other error."""
    if error not in RETRYABLE_ERRORS:
        return False
    _write_json(_failure_path(state_dir), {
        "failed_at": time.time() if now is None else now,
        "error": error, "session_id": session_id, "task": task, "recovered_at": None,
    })
    return True


def record_recovery(state_dir, now: "float | None" = None) -> bool:
    """Stamp the first successful turn after a recorded failure; False when there is none."""
    path = _failure_path(state_dir)
    rec = _read_json(path)
    if rec is None or rec.get("recovered_at") is not None:
        return False
    rec["recovered_at"] = time.time() if now is None else now
    _write_json(path, rec)
    return True


def read_failure(state_dir) -> "dict | None":
    return _read_json(_failure_path(state_dir))


def read_retry(state_dir, filename: str) -> "tuple[int, float]":
    rec = _read_json(_retry_path(state_dir, filename)) or {}
    attempts = rec.get("attempts")
    return (attempts if isinstance(attempts, int) and attempts >= 0 else 0,
            _num(rec.get("last_retry_at")) or 0.0)


def note_retry(state_dir, filename: str, now: "float | None" = None) -> int:
    attempts, _ = read_retry(state_dir, filename)
    attempts += 1
    _write_json(_retry_path(state_dir, filename),
                {"attempts": attempts, "last_retry_at": time.time() if now is None else now})
    return attempts


def _note_give_up(state_dir, filename: str, now: float) -> bool:
    """Stamp the give-up once; False when it was already stamped."""
    path = _retry_path(state_dir, filename)
    rec = _read_json(path) or {}
    if rec.get("gave_up_at") is not None:
        return False
    rec["gave_up_at"] = now
    _write_json(path, rec)
    return True


def clear_retry(state_dir, filename: str) -> None:
    _retry_path(state_dir, filename).unlink(missing_ok=True)


def retry_due(state_dir, inflight_dir, results_dir, payload, filename: str,
              deliveries_dir=None, now: "float | None" = None) -> "str | None":
    """Why to re-deliver now (`retry_verdict` "due"), or None."""
    verdict = retry_verdict(state_dir, inflight_dir, results_dir, payload, filename, deliveries_dir, now)
    return verdict[1] if verdict and verdict[0] == "due" else None


def retry_verdict(state_dir, inflight_dir, results_dir, payload, filename: str,
                  deliveries_dir=None, now: "float | None" = None) -> "tuple[str, str] | None":
    """("due", reason), ("give_up", why) the first time the cap stops it, or None.

    `decide_retry` over the on-disk state, behind the notifier's own guards: never
    due when the task has a ready result, is gone from its inbox (archived), is held
    by a pool worker (or that cannot be read), or has no in-flight marker.
    """
    # Imported here: the hooks run on every turn end and need none of it.
    from delivery.task_dispatch import has_ready_result, worker_holds
    if has_ready_result(results_dir, filename) or not Path(payload).is_file():
        return None
    if deliveries_dir is not None:
        try:
            if worker_holds(deliveries_dir, filename):
                return None
        except OSError:
            return None
    try:
        submitted_at = (Path(inflight_dir) / filename).stat().st_mtime
    except OSError:
        return None
    now = time.time() if now is None else now
    attempts, last_retry_at = read_retry(state_dir, filename)
    failure = read_failure(state_dir)
    reason = decide_retry(failure, filename, submitted_at, attempts, last_retry_at, now)
    if reason is not None:
        return ("due", reason)
    if gives_up(failure, filename, submitted_at, attempts) and _note_give_up(state_dir, filename, now):
        return ("give_up", f"not re-delivering {filename}: {attempts} re-deliveries already lost to "
                           f"{failure.get('error')}; waiting out the completion timeout")
    return None


def _hook_payload(event: str) -> "dict | None":
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("hook_event_name", event) != event:
        return None
    return payload


def _hook_turn_start(state_dir: str) -> None:
    payload = _hook_payload("UserPromptSubmit")
    if payload is not None:
        session = payload.get("session_id")
        record_turn_start(state_dir, payload.get("prompt"), session if isinstance(session, str) else "")


def _hook_stop_failure(state_dir: str) -> None:
    payload = _hook_payload("StopFailure")
    if payload is None:
        return
    error = payload.get("error")
    session = payload.get("session_id")
    session = session if isinstance(session, str) else ""
    turn = read_turn(state_dir) or {}
    same_session = not session or not turn.get("session_id") or turn.get("session_id") == session
    task = turn.get("task") if same_session else None
    end_turn(state_dir)
    if isinstance(error, str):
        record_failure(state_dir, error, session, task=task if isinstance(task, str) else None)


def _take(args: list, flag: str) -> "str | None":
    if flag in args:
        i = args.index(flag)
        if i + 1 >= len(args):
            raise ValueError(_USAGE)
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return None


def _main(argv: list) -> int:
    if not argv:
        print(_USAGE, file=sys.stderr)
        return 2
    cmd, args = argv[0], list(argv[1:])
    hook = cmd in ("hook-turn-start", "hook-stop-failure", "record-recovery")
    try:
        state = _take(args, "--state")
        if state is None:
            raise ValueError(_USAGE)
        if cmd == "hook-turn-start" and not args:
            _hook_turn_start(state)
            return 0
        if cmd == "hook-stop-failure" and not args:
            _hook_stop_failure(state)
            return 0
        if cmd == "record-recovery" and not args:
            end_turn(state)
            record_recovery(state)
            return 0
        if cmd == "retry-due":
            inflight, results = _take(args, "--inflight-dir"), _take(args, "--results-dir")
            payload, deliveries = _take(args, "--payload"), _take(args, "--deliveries-dir")
            if None in (inflight, results, payload) or len(args) != 1:
                raise ValueError(_USAGE)
            verdict = retry_verdict(state, inflight, results, payload, args[0], deliveries)
            if verdict is None:
                return 1
            print(verdict[1])
            return 0 if verdict[0] == "due" else 3
        if cmd in ("retry-note", "retry-clear") and len(args) == 1:
            if cmd == "retry-note":
                print(note_retry(state, args[0]))
            else:
                clear_retry(state, args[0])
            return 0
        raise ValueError(_USAGE)
    except Exception as exc:  # noqa: BLE001 -- hooks must never fail a turn; retry-due fails to "not due"
        print(f"turn_failure.py {cmd}: {exc}", file=sys.stderr)
        return 0 if hook else 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
