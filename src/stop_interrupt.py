#!/usr/bin/env python3
"""Interrupt the turn a Stop names: one Escape to this watcher's pane, only when that pane is
mid-turn on exactly the cancelled task. Every other case stays cooperative (the queued
CANCEL_INSTRUCTION is read when the turn ends).

The task is "current" when the newest row the session's own hooks wrote names it and is fresh,
or names it at any age while a terminal dialog holds the session (an open TUI requirement for it,
or a live dialog on the pane): a turn parked on a prompt writes no rows. The pane is mid-turn when
it shows Claude Code's `esc to interrupt` or that live dialog; the Escape answers the dialog, and
the session's open TUI requirements are then expired. A task a pool worker holds
is interrupted only by that worker's own watcher. One Escape per cancelled task, ever; each
decision is logged to the watcher's stderr and as an owner row in the activity log.

The stop is then confirmed: once the core has answered the control task, a stopped task with no
result, or only a no-send or empty one, is CANCELLED; a task the core finished is left to delivery.

Off unless SUTANDO_STOP_INTERRUPT reads as true; off, task-emit.sh marks the task CANCELLED at once.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from activity_bus import ActivityStore, LifecycleTransition
from activity_rows import append as append_row
from activity_rows import task_from_file
from delivery.pane_gate import AWAIT_HINT, live_tail, turn_status
from delivery.task_dispatch import worker_holds
from local_task_protocol import find_archived_result, valid_local_task_id
from result_markers import parse_markers
from skill_config import TRUE
from workspace_default import resolve_workspace

ESC_PROMPT = re.compile(r"esc to interrupt", re.I)
FRESH_S = 1800.0
TAIL_BYTES = 256 * 1024
CONFIRM_POLL_S = 2.0
CONFIRM_MAX_S = 3600.0
MARKER_DIR = ("state", "stop-interrupts")
ENABLE_ENV = "SUTANDO_STOP_INTERRUPT"
MID_TURN_KINDS = ("working", "processing")
CONFIG_SH = Path(__file__).resolve().parents[1] / "scripts" / "sutando-config.sh"


def enabled(environ=None) -> bool:
    """Off unless the switch reads as true; task-emit.sh reads the same values."""
    env = os.environ if environ is None else environ
    return str(env.get(ENABLE_ENV) or "").strip().lower() in TRUE


def current_task(workspace: Path, now: float, fresh_s: float = FRESH_S) -> str | None:
    """The task the newest session-hook row names, when that row is fresh; else None."""
    task, ts, _ = newest_task(workspace)
    return task if ts is not None and 0 <= now - ts <= fresh_s else None


def session_row(rec) -> "tuple[str, float, str] | None":
    """(task id, ts, kind) of a row the session's own hooks wrote, else None. task_attempts reads
    the same rows for its stall watch; a parity test holds the two readers together."""
    # TASK_STATUS rows mark delivery, and owner rows are this module's own: neither is the session's turn.
    if not isinstance(rec, dict) or rec.get("projection") or rec.get("audience"):
        return None
    task, ts = rec.get("task"), rec.get("ts")
    if not isinstance(task, dict) or not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    return task.get("id"), ts, rec.get("kind")


def newest_task(workspace: Path) -> "tuple[str | None, float | None, str | None]":
    """(task, ts, kind) of the newest session-hook row, at any age; all None when there is none."""
    try:
        with open(workspace / "state" / "agent-activity.jsonl", "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None, None, None
    for line in reversed(lines):
        try:
            row = session_row(json.loads(line))
        except ValueError:
            continue
        if row:
            return row
    return None, None, None


def mid_turn(pane_text: str | None) -> bool:
    """A turn is running: the pane's last turn row is a live spinner, and its status line (that row
    or the footer below it) says `esc to interrupt`. The same words printed above it are history."""
    lines = (pane_text or "").splitlines()
    status = turn_status(lines)
    return status is not None and any(ESC_PROMPT.search(ln) for ln in status)


def live_dialog(pane_text: str | None) -> bool:
    """A dialog below the pane's last turn row is waiting for a key."""
    return bool(pane_text) and any(AWAIT_HINT.search(ln) for ln in live_tail(pane_text.splitlines()))


def _session() -> str:
    return os.environ.get("SUTANDO_TMUX_SESSION") or "sutando-core"


def _tui_requirements(workspace: Path, session: str) -> tuple:
    """The manager and the session's open TUI requirements; (None, []) without a readable store."""
    try:
        from hitl.cards import manager_for
        manager = manager_for(workspace)
        return manager, [r for r in manager.active()
                         if (r.subject or {}).get("source") == "tui" and (r.subject or {}).get("session") == session]
    except Exception:
        return None, []


def _expire(manager, reqs: list) -> int:
    n = 0
    for r in reqs:
        try:
            manager.expire(r.id)
            n += 1
        except Exception:
            pass
    return n


def decide(target: str, *, current: str | None, held_elsewhere: bool,
           pane_text: str | None, finished: str | None = None) -> tuple[bool, str]:
    """(send Escape?, why), before the at-most-once claim. `finished` says why the target's own
    turn is already over: an Escape then could only land in whatever turn followed it."""
    if finished:
        return False, finished
    if held_elsewhere:
        return False, "a pool worker holds it; only that worker's watcher interrupts it"
    if current != target:
        return False, f"the session is on {current or 'no task'}, not {target}"
    if pane_text is None:
        return False, "the pane could not be read"
    if live_dialog(pane_text):
        return True, "a dialog in the pane holds this task"
    if not mid_turn(pane_text):
        return False, "no turn is running in the pane"
    return True, "the pane is mid-turn on this task"


def _held_elsewhere(workspace: Path, tasks_dir: Path, target: str) -> bool:
    deliveries = workspace / "deliveries"
    try:
        if tasks_dir.parent == deliveries:  # a worker's own inbox: it holds what it was handed
            return not any(p.name.startswith(target + ".") for p in tasks_dir.iterdir())
        return worker_holds(deliveries, f"{target}.txt")
    except Exception:
        return True                         # unreadable pool state: never Escape a pane that may not be on it


def _claim(workspace: Path, target: str) -> bool:
    """True the first time for this task: the marker makes the Escape at-most-once."""
    d = workspace.joinpath(*MARKER_DIR)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(d / target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        return True
    except FileExistsError:
        return False


def _pane_target(sock: str, run: Callable) -> str | None:
    if os.environ.get("SUTANDO_TMUX_PANE"):
        return os.environ["SUTANDO_TMUX_PANE"]
    session = os.environ.get("SUTANDO_TMUX_SESSION") or "sutando-core"
    window = os.environ.get("SUTANDO_TMUX_WINDOW") or ""
    if window.isdigit():                    # the launcher names the core's window: a heal may leave a sibling lower
        return f"={session}:{window}"
    out = run(["tmux", "-S", sock, "list-windows", "-t", f"={session}", "-F", "#{window_index}"])
    idx = sorted(int(x) for x in (out or "").split() if x.isdigit())
    return f"={session}:{idx[0]}" if idx else None


def _run(argv: list) -> str | None:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _socket(run: Callable) -> str | None:
    """The core's tmux socket, resolved as the launcher resolves it: the canonical
    SUTANDO_TMUX_SOCKET, then the legacy SUTANDO_TMUX_SOCK, then the config helper."""
    for name in ("SUTANDO_TMUX_SOCKET", "SUTANDO_TMUX_SOCK"):
        if os.environ.get(name):
            return os.environ[name]
    out = run(["bash", str(CONFIG_SH), "tmux-socket"])
    return (out.strip() or None) if out else None


def interrupt(target: str, *, workspace: Path, tasks_dir: Path, run: Callable = _run,
              log: Callable[[str], None] = lambda m: print(m, file=sys.stderr),
              now: float | None = None) -> bool:
    """Decide, send at most one Escape, and log the decision. True when Escape was sent."""
    now = time.time() if now is None else now
    sock = _socket(run)
    if not sock:
        log(f"stop-interrupt: {target}: no tmux socket resolved; left the turn to finish")
        return False
    snapshot = newest_task(workspace)
    newest, _, kind = snapshot
    current = current_task(workspace, now)
    # Only a row written mid-turn says the pane may still be on T: a Stop-hook `thinking` or a `done`
    # row closes T's turn, so a later turn in the pane is not T's.
    finished = (f"its newest row ({kind}) was not written mid-turn" if newest == target and kind not in MID_TURN_KINDS
                else "it already has its result" if _result_body(workspace / "results", target) is not None
                else None)
    manager, reqs = _tui_requirements(workspace, _session()) if newest == target else (None, [])
    held = _held_elsewhere(workspace, tasks_dir, target)
    pane = _pane_target(sock, run) if newest == target and not held else None
    text = run(["tmux", "-S", sock, "capture-pane", "-p", "-J", "-t", pane]) if pane else None
    if newest == target and (reqs or live_dialog(text)):
        current = target                    # parked on a prompt: no rows are written, so age is moot
    send, why = decide(target, current=current, held_elsewhere=held, pane_text=text, finished=finished)
    if send and (newest_task(workspace) != snapshot or _result_body(workspace / "results", target) is not None):
        # Rows or a result written while the pane was read: the turn may have moved on.
        send, why = False, "its turn changed while the pane was read"
    if send and not _claim(workspace, target):
        send, why = False, "already interrupted once for this task"
    if send:
        py = os.environ.get("SUTANDO_PY_BIN") or sys.executable
        sent = run(["bash", str(Path(__file__).resolve().parent / "tmux-pane-keys.sh"), "--python", py,
                    "-S", sock, "-t", pane, "--", "Escape"]) is not None
        why = why if sent else f"{why}, but the key could not be sent"
        send = sent
        if send and reqs:
            why = f"{why}; {_expire(manager, reqs)} terminal prompt card(s) expired"
    line = f"Stop: {'interrupted the running turn' if send else 'left the turn to finish'} ({why})"
    log(f"stop-interrupt: {target}: {line}")
    _log_row(workspace, tasks_dir, target, line, now)
    return send


def _log_row(workspace: Path, tasks_dir: Path, target: str, line: str, now: float) -> None:
    task, room = {"id": target}, None
    try:
        task, room = task_from_file(tasks_dir / f"{target}.txt")
    except OSError:
        pass
    try:
        append_row(line, kind="notice", room=room, task=task, workspace=workspace, audience="owner", ts=now)
    except (OSError, ValueError):
        pass


def _result_body(results_dir: Path, task_id: str) -> "str | None":
    path = results_dir / f"{task_id}.txt"
    if not path.exists():
        path = find_archived_result(results_dir, task_id)
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path else None
    except OSError:
        return None


def _stopped(body: "str | None") -> bool:
    """No result, or one with nothing to deliver: the core stopped rather than finished."""
    if body is None or not body.strip():
        return True
    parsed = parse_markers(body)
    return any(a.kind == "skip" and a.value == "no-send" for a in parsed.actions) and not parsed.body.strip()


def confirm_stop(target: str, control_id: str, *, workspace: Path, sleep: Callable = time.sleep,
                 clock: Callable = time.monotonic, max_s: float = CONFIRM_MAX_S,
                 log: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> "str | None":
    """Wait for the core to answer the control task, then rule: "cancelled" (and CANCELLED on the
    bus) or "finished" (left to delivery). None when it never answered within max_s."""
    results = workspace / "results"
    deadline = clock() + max_s
    while _result_body(results, control_id) is None:
        if clock() >= deadline:
            log(f"stop-interrupt: {target}: the control task {control_id} was not answered; no ruling")
            return None
        sleep(CONFIRM_POLL_S)
    if not _stopped(_result_body(results, target)):
        log(f"stop-interrupt: {target}: the core finished it despite the Stop")
        return "finished"
    if _held_elsewhere(workspace, workspace / "tasks", target):
        log(f"stop-interrupt: {target}: a pool worker still holds it; its own answer decides, no ruling")
        return None
    ActivityStore(workspace).apply(LifecycleTransition(target, "CANCELLED", reason="stopped"))
    log(f"stop-interrupt: {target}: stop confirmed")
    return "cancelled"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stop_interrupt.py")
    ap.add_argument("target")
    ap.add_argument("--tasks-dir", required=True)
    ap.add_argument("--workspace")
    ap.add_argument("--control-file", help="the CANCEL_INSTRUCTION task: its answer confirms the stop")
    a = ap.parse_args(argv)
    if not enabled() or not valid_local_task_id(a.target):
        return 0
    tasks_dir = Path(a.tasks_dir)
    workspace = Path(a.workspace) if a.workspace else resolve_workspace()
    try:
        interrupt(a.target, workspace=workspace, tasks_dir=tasks_dir)
    except Exception as e:                  # advisory: a Stop still completes cooperatively
        print(f"stop-interrupt: {a.target}: skipped ({e})", file=sys.stderr)
    if a.control_file:
        try:
            control_id = task_from_file(Path(a.control_file))[0]["id"]
            confirm_stop(a.target, control_id, workspace=workspace)
        except Exception as e:
            print(f"stop-interrupt: {a.target}: stop not confirmed ({e})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
