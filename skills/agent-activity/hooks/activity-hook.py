#!/usr/bin/env python3
"""Claude Code hook: turns this session's tool calls and turn-end narration into activity rows.

Reads the hook payload on stdin ({hook_event_name, session_id, transcript_path, tool_name, tool_input}).
- PreToolUse whose input names a task file (tasks/task-….txt) BINDS that task to this session
  (state/agent-activity.sessions.json) and writes its `processing` row from the task's own headers;
  every later PreToolUse in this session becomes a `working` row for the task bound to it
  (Read/Glob/Grep/TodoWrite skipped). A PostToolUse whose input named results/task-….txt writes
  `done` — only after the tool ran and only if the result file now exists (a denied or failed write
  closes nothing).
  The agent's own `activity.py append --kind processing` still binds (and is never a row).
- Stop reads the last assistant text of THIS session's transcript (complete lines only) -> `thinking`.
No bound open task -> nothing is written: a row is never attached to a task another session owns.
Always exits 0; a hook that fails must not block the tool it observed.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
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
TASK_ID = r"task-(?!cron-|bench-|workstream-|project-grouping-)[A-Za-z0-9][\w-]*"  # chat tasks in, bookkeeping out
TASK_REF = re.compile(rf"\b{TASK_ID}\b")


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
    for tid in opened:  # open_tasks() yields newest first; the newest open task wins
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
    """One writer at a time (flock on a sidecar), a per-process temp name, atomic replace; entries of
    tasks that have a done row are pruned so the file does not grow forever."""
    p["bind"].parent.mkdir(parents=True, exist_ok=True)
    lock = p["bind"].with_suffix(".lock")
    with open(lock, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        binds = load_json(p["bind"], {})
        binds[task_id] = session_id
        alive = open_tasks(p["log"])
        binds = {t: sid for t, sid in binds.items() if t in alive or t == task_id}
        tmp = p["bind"].with_name(f".{p['bind'].name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        tmp.write_text(json.dumps(binds, indent=1), encoding="utf-8")
        os.replace(tmp, p["bind"])


TASK_FILE = re.compile(rf"tasks/({TASK_ID})\.txt\b")  # tasks/archive/… has no direct match
RESULT_FILE = re.compile(rf"results/({TASK_ID})\.txt\b")


def task_file_refs(text: str) -> list[str]:
    """Task ids of task files a tool input names (tasks/<id>.txt, not results/ or archive paths)."""
    return list(dict.fromkeys(TASK_FILE.findall(text)))


def result_file_refs(text: str) -> list[str]:
    return list(dict.fromkeys(RESULT_FILE.findall(text)))


def task_header(ws: Path, task_id: str) -> tuple[dict, str | None] | None:
    """({id, from, text}, room) from the task file, or None when it is not readable."""
    for d in ("tasks", os.path.join("tasks", "archive")):
        p = ws / d / f"{task_id}.txt"
        if not p.exists():
            continue
        fields: dict = {}
        for l in p.read_text(encoding="utf-8", errors="replace").splitlines():
            k, _, v = l.partition(":")
            if k in ("user_id", "task", "channel_id", "sender_name") and k not in fields:
                fields[k] = v.strip()
        task = {"id": task_id}
        if fields.get("user_id"):
            task["from"] = fields["user_id"]
        if fields.get("sender_name"):
            task["sender"] = fields["sender_name"]
        if fields.get("task"):
            task["text"] = fields["task"][:160]
        return task, fields.get("channel_id") or None
    return None


def pickup_line(task: dict) -> str:
    """The lifecycle's first row as the owner reads it: which agent, whose message, its start."""
    agent = os.environ.get("AGENT_DISPLAY_NAME") or "Your agent"
    who = task.get("sender") or task.get("from") or "unknown"
    text = task.get("text") or ""
    return f"{agent} is working on a task from {who}: {text[:20]}{'…' if len(text) > 20 else ''}"


def answered(ws: Path, task_id: str) -> bool:
    """A result already exists: the task is finished, whatever the log says."""
    return any((ws / d / f"{task_id}.txt").exists() for d in ("results", os.path.join("results", "archive")))


def working_line(tool_name: str, tool_input) -> str | None:
    if tool_name in SKIP_TOOLS or not isinstance(tool_input, dict):
        return None
    desc = tool_input.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return None
    return re.split(r"(?<=[.;])\s", desc.strip(), maxsplit=1)[0][:MAXLEN]


TAIL_BYTES = 64 * 1024


def last_narration(transcript: Path) -> str | None:
    """First line of the last assistant text block, read from the file's tail only (a transcript grows
    to tens of MB; Stop fires per turn), from complete lines only (a row mid-write is ignored)."""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            data = f.read()
    except OSError:
        return None
    if size > TAIL_BYTES:
        data = data[data.find(b"\n") + 1:]  # drop the partial first line of the window
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


NO_SEND = re.compile(r"\[(no-send|REPLIED|deduped:[^\]]*)\]")


def done_text(tool_input_json: str) -> str:
    return "closed, no message sent from here" if NO_SEND.search(tool_input_json) else "replied"


def emit(kind: str, line: str, task: dict, room: str | None, ws: Path, run=subprocess.run) -> None:
    if kind == "done":
        cmd = [sys.executable, str(WRITER), "done", line, "--task-id", task["id"], "--workspace", str(ws)]
    else:
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
        blob = json.dumps(inp, ensure_ascii=False) if isinstance(inp, dict) else ""
        binds = load_json(p["bind"], {})
        # First touch of a task file by this session: bind it and write its processing row from the
        # task's own headers — the agent never has to remember to.
        for tid in task_file_refs(blob):
            # A late touch of an answered task (a dedup check, a re-read) must not reopen it.
            if tid in binds or answered(p["ws"], tid):
                continue
            found = task_header(p["ws"], tid)
            if not found:
                continue
            task, room = found
            line = pickup_line(task)
            bind(p, tid, sid)
            emit("processing", line, task, room, p["ws"], run)
            out.append(("processing", line))
        if out:
            return out
        if result_file_refs(blob):
            return out  # the result write itself is not "working"; PostToolUse decides whether it closed the task
        line = working_line(tool, inp) if isinstance(tool, str) else None
        if line:
            task, room = bound_task(p, sid)
            if task:
                emit("working", line, task, room, p["ws"], run)
                out.append(("working", line))
    elif event == "PostToolUse":
        inp = payload.get("tool_input")
        blob = json.dumps(inp, ensure_ascii=False) if isinstance(inp, dict) else ""
        binds = load_json(p["bind"], {})
        opened = open_tasks(p["log"])
        # The result write closes the task only once the file exists: the tool ran, was not denied,
        # and produced the artifact. The text says what the result did (a marker body reached nobody).
        for tid in result_file_refs(blob):
            if tid in opened and binds.get(tid) == sid and (p["ws"] / "results" / f"{tid}.txt").exists():
                task, room = opened[tid]["task"], opened[tid]["room"]
                text = done_text(blob)
                emit("done", text, task, room, p["ws"], run)
                out.append(("done", text))
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
