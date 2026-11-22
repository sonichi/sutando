#!/usr/bin/env python3
"""The core CLI's own word on whether it is blocked on the owner, from its hook events.

The core supervisor (core-input-watch.py) otherwise infers a dialog by reading pane text,
which quoted hint text and menus can fool. Claude Code and Codex both fire hook events
that say it directly: a permission request or a question dialog opens (blocked), or the
turn moves on (clear). This module owns the one record those hooks write and the
supervisor reads.

Writer contract (one writer: `write`):
  path    <state_dir>/core-block-signal/<socket-key>.<pane-id>.json
  record  {"schema": 1, "state": "blocked"|"clear", "reason": "permission"|"question"|null,
           "event": <hook event>, "runtime": "claude"|"codex", "session": <tmux session>,
           "pane_id": "%N", "pane_pid": <int>, "ts": <unix seconds>, "source": "hook"}
  writes  whole-record replace via temp file + os.replace; last writer wins, never a merge.
  failure every hook path fails open and silent: no record is a fallback, never a verdict.

Keyed by tmux pane, and bound to the pane's process: a relaunched CLI is a new pane process,
so a record from the previous one can never speak for it.

Usage:
  core_block_signal.py hook --runtime claude|codex [--state-dir DIR]   (hook stdin = event JSON)
  core_block_signal.py codex-args <repo>                                (NUL-separated codex argv)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

SCHEMA = 1
SUBDIR = "core-block-signal"
#: A clear record vouches for this long. A turn in flight fires a hook on every tool call and
#: at its end; past this the record no longer proves the hooks still fire for this process.
CLEAR_TTL_S = 900.0

BLOCKED, CLEAR = "blocked", "clear"
_CLEAR_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "Stop"}
# Claude's Notification types that mean a dialog waits on a person. idle_prompt is not one:
# it is the empty composer at rest, the state the task notifier delivers into.
_BLOCKING_NOTIFICATIONS = {"permission_prompt": "permission", "elicitation_dialog": "question"}
#: The events each runtime's hook registration subscribes; Codex has no Notification event.
CLAUDE_EVENTS = ("Notification", "PermissionRequest", "PreToolUse", "PostToolUse",
                 "PostToolUseFailure", "UserPromptSubmit", "Stop")
CODEX_EVENTS = ("PermissionRequest", "PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")


def event_state(payload: dict) -> "Optional[tuple[str, Optional[str]]]":
    """(state, reason) a hook event implies, or None when it says nothing about a dialog."""
    name = payload.get("hook_event_name") if isinstance(payload, dict) else None
    if name == "PermissionRequest":
        return BLOCKED, "permission"
    if name == "Notification":
        reason = _BLOCKING_NOTIFICATIONS.get(payload.get("notification_type"))
        return (BLOCKED, reason) if reason else None
    if name in _CLEAR_EVENTS:
        return CLEAR, None
    return None


def socket_key(socket: str) -> str:
    """A short stable name for a tmux server: pane ids are only unique within one."""
    return hashlib.sha1(os.path.realpath(socket).encode()).hexdigest()[:10]


def record_path(state_dir, socket: str, pane_id: str) -> Path:
    return Path(state_dir) / SUBDIR / f"{socket_key(socket)}.{pane_id.lstrip('%')}.json"


def write(state_dir, socket: str, pane_id: str, pane_pid: int, state: str,
          reason: Optional[str], event: str, runtime: str, session: str,
          now: Optional[float] = None) -> Path:
    """Replace the pane's record atomically. Raises on an invalid state or an I/O failure."""
    if state not in (BLOCKED, CLEAR):
        raise ValueError(f"state must be blocked or clear, not {state!r}")
    path = record_path(state_dir, socket, pane_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"schema": SCHEMA, "state": state, "reason": reason if state == BLOCKED else None,
           "event": str(event)[:64], "runtime": str(runtime)[:32], "session": str(session)[:128],
           "pane_id": pane_id, "pane_pid": int(pane_pid),
           "ts": time.time() if now is None else float(now), "source": "hook"}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read(state_dir, socket: str, pane_id: str) -> Optional[dict]:
    """The pane's record, or None when absent or unreadable."""
    try:
        rec = json.loads(record_path(state_dir, socket, pane_id).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("schema") == SCHEMA else None


def verdict(rec: Optional[dict], pane_pid: Optional[int], now: float,
            ttl: float = CLEAR_TTL_S) -> "Optional[tuple[str, Optional[str]]]":
    """("blocked", reason) / ("clear", None) when the record speaks for this pane process
    now, else None: the caller falls back to reading the screen.

    A blocked record does not age out: a dialog can wait on its owner for hours, and the
    pane-process binding already retires it when the CLI goes. A clear one does (CLEAR_TTL_S)."""
    if not rec or pane_pid is None or rec.get("pane_pid") != pane_pid:
        return None
    try:
        age = now - float(rec.get("ts"))
    except (TypeError, ValueError):
        return None
    if rec.get("state") == BLOCKED:
        return BLOCKED, rec.get("reason") or "permission"
    if rec.get("state") == CLEAR and -60.0 <= age <= ttl:
        return CLEAR, None
    return None


def pane_identity(socket: str, target: str, tmux_bin: str = "tmux",
                  runner=subprocess.run) -> "Optional[tuple[str, str, int]]":
    """(session name, pane id, pane pid) of a tmux target, or None when tmux cannot say."""
    try:
        proc = runner([tmux_bin, "-S", socket, "display-message", "-p", "-t", target,
                       "#{session_name}\t#{pane_id}\t#{pane_pid}"],
                      capture_output=True, text=True, timeout=3)
    except Exception:  # noqa: BLE001 — a failed probe is no identity, never a verdict
        return None
    parts = (getattr(proc, "stdout", "") or "").strip().split("\t")
    if getattr(proc, "returncode", 1) != 0 or len(parts) != 3 or not parts[2].isdigit():
        return None
    return parts[0], parts[1], int(parts[2])


def _hook(runtime: str, state_dir: Optional[str]) -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    implied = event_state(payload)
    tmux_env, pane = os.environ.get("TMUX", ""), os.environ.get("TMUX_PANE", "")
    if implied is None or not tmux_env or not pane:
        return
    socket = tmux_env.split(",", 1)[0]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tmux_probe
    ident = pane_identity(socket, pane, tmux_probe.tmux_binary())
    if ident is None:
        return
    if state_dir is None:
        from workspace_default import resolve_workspace
        state_dir = str(Path(resolve_workspace(migrate=False)) / "state")
    session, pane_id, pane_pid = ident
    write(state_dir, socket, pane_id, pane_pid, implied[0], implied[1],
          payload.get("hook_event_name", ""), runtime, session)


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def codex_args(repo: str) -> list:
    """The codex argv that registers the block-signal hooks for one invocation. Session hooks
    need persisted trust, so the list ends with the flag that runs them without it."""
    cmd = f"bash {shlex.quote(str(Path(repo).resolve() / 'src' / 'core-block-hook.sh'))} codex"
    out = []
    for ev in CODEX_EVENTS:
        out += ["-c", f"hooks.{ev}=[{{hooks=[{{type=\"command\",command={_toml_str(cmd)},timeout=10}}]}}]"]
    return out + ["--dangerously-bypass-hook-trust"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook")
    h.add_argument("--runtime", required=True, choices=("claude", "codex"))
    h.add_argument("--state-dir")
    c = sub.add_parser("codex-args")
    c.add_argument("repo")
    a = ap.parse_args(argv)
    if a.cmd == "codex-args":
        sys.stdout.write("".join(arg + "\0" for arg in codex_args(a.repo)))
        return 0
    try:
        _hook(a.runtime, a.state_dir)
    except Exception:  # noqa: BLE001 — a hook must never block the tool it observes
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
