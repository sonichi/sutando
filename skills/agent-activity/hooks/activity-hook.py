#!/usr/bin/env python3
"""Claude Code hook: turns this session's tool calls and turn-end narration into activity rows.

Reads the hook payload on stdin ({hook_event_name, session_id, transcript_path, tool_name, tool_input}).
- PreToolUse on a Bash call that runs `activity.py append --kind processing` for a task BINDS that
  task to this session (state/agent-activity.sessions.json); every later PreToolUse in this session
  becomes a `working` row for the task bound to it (Read/Glob/Grep/TodoWrite skipped).
- Stop reads the last assistant text of THIS session's transcript (complete lines only) -> `thinking`.
No bound open task -> nothing is written: a row is never attached to a task another session owns.
Always exits 0; a hook that fails must not block the tool it observed.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

WRITER = Path(__file__).resolve().parent.parent / "scripts" / "activity.py"  # lint-workspace-resolution: allow-repo-root (sibling script, not a data root)
SKIP_TOOLS = {"Read", "Glob", "Grep", "TodoWrite"}
MAXLEN = 100
TEXT_MAX = 240
TASK_REF = re.compile(r"\btask-[0-9a-f]{6,}\b")


def paths(workspace: Path | None = None) -> dict:
    ws = workspace or resolve_workspace()
    return {"ws": ws, "log": ws / "state" / "agent-activity.jsonl", "bind": ws / "state" / "agent-activity.sessions.json"}


def load_json(path: Path, default):
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def open_tasks(log: Path) -> dict[str, dict]:
    """{task_id: {"task", "room"}} for tasks with a row and no later done row; malformed rows skipped."""
    out: dict[str, dict] = {}
    done: set[str] = set()
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t = r.get("task") if isinstance(r, dict) else None
        if not isinstance(t, dict) or not isinstance(t.get("id"), str):
            continue
        if r.get("done"):
            done.add(t["id"])
        elif t["id"] not in done and t["id"] not in out:
            out[t["id"]] = {"task": t, "room": r.get("room") if isinstance(r.get("room"), str) else None}
    return out


def bound_task(p: dict, session_id: str) -> tuple[dict | None, str | None]:
    """The open task this session is bound to; None when none is (fail closed)."""
    binds = load_json(p["bind"], {})
    opened = open_tasks(p["log"])
    for tid in reversed(list(opened)):  # newest open task first
        if binds.get(tid) == session_id:
            return opened[tid]["task"], opened[tid]["room"]
    return None, None


def processing_task_id(command: str, ws: Path) -> str | None:
    """The task id a `activity.py append --kind processing` command names, else None."""
    if "activity.py" not in command or "processing" not in command:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    if "--task-id" in argv:
        return argv[argv.index("--task-id") + 1] if argv.index("--task-id") + 1 < len(argv) else None
    if "--task-file" in argv and argv.index("--task-file") + 1 < len(argv):
        path = Path(argv[argv.index("--task-file") + 1])
        if not path.is_absolute():
            path = ws / path
        try:
            for l in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if l.startswith("id:"):
                    return l.split(":", 1)[1].strip()
        except OSError:
            pass
        return path.stem
    m = TASK_REF.search(command)
    return m.group(0) if m else None


def bind(p: dict, task_id: str, session_id: str) -> None:
    binds = load_json(p["bind"], {})
    binds[task_id] = session_id
    p["bind"].parent.mkdir(parents=True, exist_ok=True)
    tmp = p["bind"].with_suffix(".tmp")
    tmp.write_text(json.dumps(binds, indent=1), encoding="utf-8")
    os.replace(tmp, p["bind"])


def working_line(tool_name: str, tool_input) -> str | None:
    if tool_name in SKIP_TOOLS or not isinstance(tool_input, dict):
        return None
    desc = tool_input.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return None
    return re.split(r"(?<=[.;])\s", desc.strip(), 1)[0][:MAXLEN]


def last_narration(transcript: Path) -> str | None:
    """First line of the last assistant text block, from complete lines only (a row mid-write is ignored)."""
    try:
        data = transcript.read_bytes()
    except OSError:
        return None
    if not data.endswith(b"\n"):
        data = data[: data.rfind(b"\n") + 1]
    for raw in reversed(data.decode("utf-8", errors="replace").splitlines()):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in reversed(content):
            if isinstance(b, dict) and b.get("type") == "text":
                t = (b.get("text") or "").strip()
                if t and not t.startswith("```"):
                    return t.split("\n", 1)[0][:TEXT_MAX]
        return None
    return None


def emit(kind: str, line: str, task: dict, room: str | None, ws: Path, run=subprocess.run) -> None:
    cmd = [sys.executable, str(WRITER), "append", line, "--kind", kind, "--task-id", task["id"], "--workspace", str(ws)]
    if isinstance(task.get("from"), str):
        cmd += ["--from", task["from"]]
    if isinstance(task.get("text"), str):
        cmd += ["--text", task["text"]]
    if room:
        cmd += ["--room", room]
    run(cmd, capture_output=True)


def handle(payload: dict, p: dict, run=subprocess.run) -> list[tuple[str, str]]:
    """Process one hook payload; returns the (kind, line) rows it emitted."""
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return []
    event = payload.get("hook_event_name")
    out: list[tuple[str, str]] = []
    if event == "PreToolUse":
        tool, inp = payload.get("tool_name"), payload.get("tool_input")
        cmd = inp.get("command") if isinstance(inp, dict) and isinstance(inp.get("command"), str) else ""
        if tool == "Bash" and cmd:
            tid = processing_task_id(cmd, p["ws"])
            if tid:
                bind(p, tid, sid)
                return out  # the processing row itself is the agent's; no working row for writing it
            if "activity.py" in cmd:
                return out
        line = working_line(tool, inp) if isinstance(tool, str) else None
        if line:
            task, room = bound_task(p, sid)
            if task:
                emit("working", line, task, room, p["ws"], run)
                out.append(("working", line))
    elif event == "Stop":
        tp = payload.get("transcript_path")
        task, room = bound_task(p, sid)
        if task and isinstance(tp, str):
            line = last_narration(Path(tp))
            if line:
                emit("thinking", line, task, room, p["ws"], run)
                out.append(("thinking", line))
    return out


def main(stdin=sys.stdin, workspace: Path | None = None) -> int:
    try:
        payload = json.loads(stdin.read() or "{}")
        if isinstance(payload, dict):
            handle(payload, paths(workspace))
    except Exception:  # noqa: BLE001 — a hook must never block the tool it observed
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
