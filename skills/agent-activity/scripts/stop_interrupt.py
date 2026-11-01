#!/usr/bin/env python3
"""Interrupt the turn a Stop names: one Escape to the core's pane, only when that pane's open turn
is on exactly the stopped task. Every other case stays cooperative (the queued CANCEL_INSTRUCTION
is read when the turn ends).

The pane names its open turn (`turns.PANE_OPTION`, stamped by this skill's hook), and the turn's
record says which tasks its prompt named and which dialog it is parked on. The Escape goes when
the turn is on the stopped task alone and the pane is mid-turn or showing a dialog. Anything
unreadable or unrecorded sends nothing. The key is fenced on the same turn id, so a turn that
ended meanwhile gets no key.
One Escape per task, ever; each decision is logged to stderr and as an owner row.

The stop is then ruled, from a durable record this skill's hook re-checks at every turn end: once
the core has answered the control task, a stopped task with no result is CANCELLED, one with a
ready result is left to delivery, and one whose result is still pending waits. Unanswered for an
hour, it is CANCELLED as it would be with the switch off. A task a pool worker holds gets its stop
routed to that worker's inbox, where the worker's own watcher runs this.

The skill's `on_stop_requested` handler: exit 0 takes the stop, 1 leaves it to the watcher.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import turns
from activity_bus import ActivityStore, LifecycleTransition
from activity_rows import append as append_row
from activity_rows import task_from_file
from delivery.pane_gate import AWAIT_HINT, live_tail, turn_status
from delivery.task_dispatch import has_ready_result, is_wake_turn, worker_holds
from file_lock import locked_file
from local_task_protocol import iter_result_candidates, valid_local_task_id
from skill_config import enabled as config_enabled
from tmux_pane_keys import fence_keys_argv
from workspace_default import resolve_workspace

ESC_PROMPT = re.compile(r"esc to interrupt", re.I)
CONFIRM_POLL_S = 2.0
CONFIRM_MAX_S = 3600.0
MARKER_DIR = ("state", "stop-interrupts")
RULING = ".ruling.json"
ENABLE_KEY = "SUTANDO_STOP_INTERRUPT"
MANIFEST = Path(__file__).resolve().parents[1] / "manifest.json"
CONFIG_SH = Path(__file__).resolve().parents[3] / "scripts" / "sutando-config.sh"


def enabled(environ=None) -> bool:
    """The skill's switch: environment, then this manifest; off when unreadable."""
    return config_enabled(ENABLE_KEY, MANIFEST, environ)


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


def decide(target: str, *, turn, pane_text: str | None, held_elsewhere: bool = False,
           finished: str | None = None) -> tuple[bool, str]:
    """(send Escape?, why), before the at-most-once claim. `turn` is the pane's open turn record:
    None for no open turn, turns.UNKNOWN when it cannot be read or no hook recorded it."""
    if finished:
        return False, finished
    if held_elsewhere:
        return False, "a pool worker holds it; the stop is routed to that worker, whose watcher interrupts it"
    if turn is None:
        return False, "no turn is open in the pane"
    if not isinstance(turn, dict) or not isinstance(turn.get("tasks"), list):
        return False, "the pane's turn cannot be read"
    if turn.get("ended"):
        return False, "the pane's turn has ended"
    if pane_text is None:
        return False, "the pane could not be read"
    tasks = turn["tasks"]
    others = [t for t in tasks if t != target]
    if others:
        return False, f"the turn is on {others[0]}" + (" too" if target in tasks else "")
    if not tasks:
        return False, "the turn is on no task"  # a dialog naming the file may be any turn's
    if live_dialog(pane_text):
        return True, "a dialog in the pane holds this task's turn"
    if mid_turn(pane_text):
        return True, "the pane is mid-turn on this task"
    return False, "no turn is running in the pane"


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
    session = _session()
    window = os.environ.get("SUTANDO_TMUX_WINDOW") or ""
    if window.isdigit():                    # the launcher names the core's window: a heal may leave a sibling lower
        return f"={session}:{window}"
    out = run(["tmux", "-S", sock, "list-windows", "-t", f"={session}", "-F", "#{window_index}"])
    idx = sorted(int(x) for x in (out or "").split() if x.isdigit())
    return f"={session}:{idx[0]}" if idx else None


def _pane_turn(sock: str, pane: str, run: Callable):
    """The pane's open turn id; "" when none is open; None when the pane cannot be read."""
    out = run(["tmux", "-S", sock, "display-message", "-p", "-t", pane, "#{%s}" % turns.PANE_OPTION])
    return None if out is None else out.strip()


def _turn(workspace: Path, pid):
    if pid == "":
        return None
    return turns.read(workspace, pid) or turns.UNKNOWN if pid and turns.PROMPT_ID.match(pid) else turns.UNKNOWN


def _run(argv: list) -> str | None:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
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
    """Decide, send at most one fenced Escape, and log the decision. True when Escape was sent."""
    now = time.time() if now is None else now
    sock = _socket(run)
    if not sock:
        log(f"stop-interrupt: {target}: no tmux socket resolved; left the turn to finish")
        return False
    finished = ("it already has its result, or one being written"
                if _result_state(workspace, target) is not None else None)
    held = _held_elsewhere(workspace, tasks_dir, target)
    pane = _pane_target(sock, run) if not (finished or held) else None
    pid = _pane_turn(sock, pane, run) if pane else None
    turn = _turn(workspace, pid) if pane else None
    text = run(["tmux", "-S", sock, "capture-pane", "-p", "-J", "-t", pane]) if pane else None
    send, why = decide(target, turn=turn, pane_text=text, held_elsewhere=held, finished=finished)
    if send and (_turn(workspace, pid) != turn or _result_state(workspace, target) is not None):
        send, why = False, "its turn changed while the pane was read"
    if send and not _claim(workspace, target):
        send, why = False, "already interrupted once for this task"
    if send:
        manager, reqs = _tui_requirements(workspace, _session())
        fence = "#{==:#{%s},%s}" % (turns.PANE_OPTION, pid)
        sent = run(fence_keys_argv(sock, pane, fence, "Escape")) is not None
        why = why if sent else f"{why}, but the key was not sent (the turn moved on, or tmux failed)"
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


def _mtime(path) -> "float | None":
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _result_state(workspace: Path, task_id: str) -> "str | None":
    """"ready" when the task has a deliverable result (the shared readiness walk, live or archived,
    a wake turn's own only), "pending" when one of its turn exists but is not ready yet (blank,
    being written, or unreadable), None when it has none."""
    results, task_file = workspace / "results", workspace / "tasks" / f"{task_id}.txt"
    if has_ready_result(results, f"{task_id}.txt", task_file=task_file):
        return "ready"
    since = _mtime(task_file) if is_wake_turn(task_file) else None
    for c in iter_result_candidates(results, task_id):
        written = _mtime(c)
        if since is None or written is None or written >= since:
            return "pending"
    return None


def _rulings(workspace: Path) -> Path:
    return workspace.joinpath(*MARKER_DIR)


def open_ruling(workspace: Path, target: str, control_id: str, now: float | None = None) -> None:
    """Record that the stop waits on its ruling; it outlives this process."""
    d = _rulings(workspace)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"target": target, "control": control_id, "at": time.time() if now is None else now}
    tmp = d / f".{target}{RULING}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, d / f"{target}{RULING}")


def _valid_ruling(rec, f: Path) -> bool:
    """Exactly the record open_ruling writes, under its own task's name."""
    return (type(rec) is dict and set(rec) == {"target", "control", "at"}
            and type(rec["target"]) is str and valid_local_task_id(rec["target"])
            and type(rec["control"]) is str and valid_local_task_id(rec["control"])
            and type(rec["at"]) in (int, float) and f.name == f"{rec['target']}{RULING}")


def rule(workspace: Path, rec: dict, now: float,
         log: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> "str | None":
    """"cancelled" (CANCELLED on the bus), "finished" (left to delivery), or None to wait."""
    target = rec["target"]
    state = _result_state(workspace, target)
    if state == "ready":
        log(f"stop-interrupt: {target}: the core finished it despite the Stop")
        return "finished"
    late = now - rec["at"] >= CONFIRM_MAX_S
    if state == "pending" and not late:
        return None                         # an answer may be on its way: never ruled a stop yet
    if _result_state(workspace, rec["control"]) != "ready" and not late:
        return None
    if not late and _held_elsewhere(workspace, workspace / "tasks", target):
        return None                         # a pool worker may still answer it
    reason = "stopped" if not late else "stopped; the stop was not answered within the hour"
    ActivityStore(workspace).apply(LifecycleTransition(target, "CANCELLED", reason=reason))
    log(f"stop-interrupt: {target}: {reason}")
    return "cancelled"


def sweep(workspace: Path, now: float | None = None, only: str | None = None,
          log: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> "dict[str, str]":
    """Rule every waiting stop (or only one); a ruled stop's record is removed."""
    now = time.time() if now is None else now
    d, out = _rulings(workspace), {}
    if not d.is_dir():
        return out
    with locked_file(d / ".rulings.lock", create_mode=0o600):
        for f in sorted(d.glob(f"*{RULING}")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                if not _valid_ruling(rec, f):
                    raise ValueError("not a ruling this skill wrote")
                if only is not None and rec["target"] != only:
                    continue
                verdict = rule(workspace, rec, now, log)
            except (OSError, ValueError) as e:
                log(f"stop-interrupt: {f.name}: not ruled, left as it is ({e})")
                continue
            if verdict:
                out[rec["target"]] = verdict
                f.unlink(missing_ok=True)
    return out


def confirm(target: str, *, workspace: Path, sleep: Callable = time.sleep, clock: Callable = time.monotonic,
            max_s: float = CONFIRM_MAX_S) -> "str | None":
    """Poll this stop's ruling for a while; past that, the hook's sweep at each turn end rules it."""
    deadline = clock() + max_s
    while True:
        verdict = sweep(workspace, only=target).get(target)
        if verdict or clock() >= deadline:
            return verdict
        sleep(CONFIRM_POLL_S)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stop_interrupt.py")
    ap.add_argument("target")
    ap.add_argument("--tasks-dir", required=True)
    ap.add_argument("--workspace")
    ap.add_argument("--control-file", required=True, help="the CANCEL_INSTRUCTION task: its answer rules the stop")
    ap.add_argument("--work", action="store_true", help="the detached half: interrupt, then rule")
    a = ap.parse_args(argv)
    workspace = Path(a.workspace) if a.workspace else resolve_workspace()
    if a.work:
        try:
            interrupt(a.target, workspace=workspace, tasks_dir=Path(a.tasks_dir))
        except Exception as e:              # advisory: a Stop still completes cooperatively
            print(f"stop-interrupt: {a.target}: skipped ({e})", file=sys.stderr)
        confirm(a.target, workspace=workspace)
        return 0
    if not enabled() or not valid_local_task_id(a.target):
        return 1
    try:
        open_ruling(workspace, a.target, task_from_file(Path(a.control_file))[0]["id"])
    except (OSError, KeyError, ValueError) as e:
        print(f"stop-interrupt: {a.target}: not taken ({e})", file=sys.stderr)
        return 1
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *(argv or sys.argv[1:]), "--work",
                      "--workspace", str(workspace)], start_new_session=True, stdin=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
