"""Which task a Claude Code turn is on, from the session's own turn-scoped hooks.

A turn is one `prompt_id`. UserPromptSubmit opens it with the tasks its prompt names (each
`TASK_FILE:` line the task watcher sent), PermissionRequest records the dialog it is parked on,
PreToolUse/PostToolUse clear that dialog, and Stop ends it. The hook also stamps its tmux pane
with the open turn's id (`@sutando_turn`), so a reader holding only the pane can find the turn,
and a fenced key can check, in the same server command, that the turn is still the one it read.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from file_lock import locked_file  # noqa: E402

UNKNOWN = "<unknown>"
PANE_OPTION = "@sutando_turn"
TURNS_KEPT = 64
PANE_TIMEOUT_S = 3
PROMPT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# A Monitor notification wraps each watcher event in <event>…</event>, its lines inside.
# The name may follow any path, spaces included (a worker seat's inbox under "Application Support").
TASK_LINE = re.compile(r"^\s*(?:<event>)?TASK_FILE:\s*(?:.*/)?(task-[^\s/<]+?)\.txt\s*(?:</event>)?\s*$", re.M)


def path(ws: Path) -> Path:
    return ws / "state" / "agent-activity.turns.json"


def prompt_tasks(prompt) -> "list[str]":
    """The tasks a turn's prompt names, in order: each `TASK_FILE: <name>` line the watcher sent."""
    return list(dict.fromkeys(TASK_LINE.findall(prompt))) if isinstance(prompt, str) else []


def read(ws: Path, prompt_id: str):
    """The turn's record; None when no hook recorded it; UNKNOWN when the file cannot be read."""
    try:
        data = json.loads(path(ws).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return UNKNOWN
    turns = data.get("turns") if isinstance(data, dict) else None
    if not isinstance(turns, dict):
        return UNKNOWN
    turn = turns.get(prompt_id)
    return turn if turn is None or isinstance(turn, dict) else UNKNOWN


def open_dialog(ws: Path, task_id: str) -> "dict | None":
    """The newest open permission dialog of an unended turn that names `task_id`: {tool, at}, or None
    (none open, or a record that cannot be read: a reader fails to "not waiting", never to a guess)."""
    try:
        data = json.loads(path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    turns = data.get("turns") if isinstance(data, dict) else None
    if not isinstance(turns, dict):
        return None
    found = [t["dialog"] for t in turns.values()
             if isinstance(t, dict) and t.get("ended") is None and isinstance(t.get("dialog"), dict)
             and isinstance(t.get("tasks"), list) and task_id in t["tasks"]]
    return max(found, key=lambda d: d.get("at") or 0, default=None)


def has_open_tool(ws: Path, task_id: str) -> bool:
    """Whether a task's unended turn has an unmatched tool start."""
    try:
        data = json.loads(path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    turns = data.get("turns") if isinstance(data, dict) else None
    if not isinstance(turns, dict):
        return False
    return any(isinstance(t, dict) and t.get("ended") is None
               and isinstance(t.get("tasks"), list) and task_id in t["tasks"]
               and isinstance(t.get("open_tools"), dict) and bool(t["open_tools"])
               for t in turns.values())


def record(payload: dict, ws: Path, now: "float | None" = None) -> None:
    """Fold one hook payload into its turn's record."""
    event, pid = payload.get("hook_event_name"), payload.get("prompt_id")
    if not isinstance(pid, str) or not PROMPT_ID.match(pid):
        return
    now = time.time() if now is None else now
    f = path(ws)
    f.parent.mkdir(parents=True, exist_ok=True)
    with locked_file(f.with_suffix(".lock"), create_mode=0o600):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"turns": {}}
        turns = data.get("turns") if isinstance(data, dict) else None
        if not isinstance(turns, dict):
            return                              # left damaged, so a reader sees it as unknown
        turn = turns.get(pid) if isinstance(turns.get(pid), dict) else None
        if event == "UserPromptSubmit":
            turn = {"tasks": prompt_tasks(payload.get("prompt")), "started": now, "ended": None, "dialog": None}
        elif turn is None:
            # A turn whose prompt no hook saw: its tasks are unknown, never "none".
            turn = {"tasks": None, "started": None, "ended": None, "dialog": None}
        if event == "PermissionRequest":
            # Only that a dialog is open: its tool input may carry secrets and nothing reads it.
            tool = payload.get("tool_name")
            turn["dialog"] = {"tool": tool if type(tool) is str else None, "at": now}
        elif event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            turn["dialog"] = None
            tool_id = payload.get("tool_use_id")
            tool_name = payload.get("tool_name")
            key = ("id:" + tool_id if isinstance(tool_id, str) and tool_id
                   else "name:" + tool_name if isinstance(tool_name, str) and tool_name else None)
            opened = turn.setdefault("open_tools", {})
            if key and event == "PreToolUse":
                opened[key] = opened.get(key, 0) + 1 if key.startswith("name:") else 1
            elif key and key in opened:
                if opened[key] > 1:
                    opened[key] -= 1
                else:
                    del opened[key]
        elif event == "Stop":
            turn.update(ended=now, dialog=None, open_tools={})
        turn["seen"] = now
        turns[pid] = turn
        for old in sorted(turns, key=lambda k: turns[k].get("seen") or 0)[:-TURNS_KEPT]:
            del turns[old]
        tmp = f.with_name(f".{f.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data))
        os.replace(tmp, f)


def stamp_pane(payload: dict, environ=None, run=subprocess.run) -> None:
    """Mark the hook's own tmux pane with the open turn; Stop clears it only while it is still its own."""
    env = os.environ if environ is None else environ
    event, pid, pane = payload.get("hook_event_name"), payload.get("prompt_id"), env.get("TMUX_PANE")
    if not env.get("TMUX") or not pane or not isinstance(pid, str) or not PROMPT_ID.match(pid):
        return
    if event == "UserPromptSubmit":
        argv = ["tmux", "set-option", "-p", "-t", pane, PANE_OPTION, pid]
    elif event == "Stop":
        argv = ["tmux", "if-shell", "-F", "-t", pane, "#{==:#{%s},%s}" % (PANE_OPTION, pid),
                f"set-option -p -u -t {pane} {PANE_OPTION}"]
    else:
        return
    run(argv, capture_output=True, timeout=PANE_TIMEOUT_S, env=env)
