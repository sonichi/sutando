#!/usr/bin/env python3
"""Follow the core's Claude Code transcript and turn it into event rows for the open task.

  assistant tool_use with a `description`  -> kind working  (Read/Glob/Grep/TodoWrite are skipped)
  assistant text block (narration)         -> kind thinking (first line)
Rows attach to the task currently open in the log (last row with a task and no later `done`) and
are dropped when none is open, so idle passes never reach the drawer. The private thinking blocks
carry only a signature, never text; narration is the nearest thing to them.

  activity-tail.py            foreground      activity-tail.py --daemon   background, pidfile-guarded
"""
from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

HERE = Path(__file__).resolve().parent
WRITER = HERE / "activity.py"
SKIP_TOOLS = {"Read", "Glob", "Grep", "TodoWrite"}
MAXLEN = 240
POLL_S = 2
TASK_REF = re.compile(r"\btask-[0-9a-f]{6,}\b")


def paths(workspace: Path | None = None) -> dict:
    ws = workspace or resolve_workspace()
    return {
        "ws": ws,
        "log": ws / "state" / "agent-activity.jsonl",
        "pid": ws / "state" / "activity-tail.pid",
        "out": ws / "state" / "activity-tail.log",
        "projects": ws / ".claude-sutando" / "projects",
    }


def newest_transcript(projects: Path) -> Path | None:
    files = glob.glob(str(projects / "*" / "*.jsonl"))
    return Path(max(files, key=os.path.getmtime)) if files else None


def open_task(log: Path) -> tuple[dict | None, str | None]:
    """(task, room) of the task in progress: the last task row whose id has no later done row."""
    try:
        rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        return None, None
    done: set[str] = set()
    for r in reversed(rows):
        t = r.get("task") or {}
        if r.get("done") and t.get("id"):
            done.add(t["id"])
        elif t.get("id") and t["id"] not in done and t.get("text"):
            return t, r.get("room")
    return None, None


def task_context(text: str, ws: Path) -> str:
    """'from <sender>: <first 20 chars>' for each task file a tool call names."""
    parts = []
    for tid in dict.fromkeys(TASK_REF.findall(text)):
        for d in ("tasks", os.path.join("tasks", "archive")):
            p = ws / d / f"{tid}.txt"
            if not p.exists():
                continue
            sender = body = ""
            for l in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if l.startswith("sender_name:"):
                    sender = l.split(":", 1)[1].strip()
                elif l.startswith("task:"):
                    body = l.split(":", 1)[1].strip()
            if body.startswith("[") and "attached:" in body:
                body = body.split(":", 1)[0].strip("[")
            parts.append(f"from {sender or 'unknown'}: {body[:20]}{'…' if len(body) > 20 else ''}")
            break
    return "; ".join(parts)


def rows_from(line: str, ws: Path):
    try:
        d = json.loads(line)
    except ValueError:
        return
    content = (d.get("message") or {}).get("content") if d.get("type") == "assistant" else None
    if not isinstance(content, list):
        return
    for b in content:
        if b.get("type") == "tool_use":
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            desc = inp.get("description")
            if b.get("name") in SKIP_TOOLS or not desc:
                continue
            ctx = task_context(json.dumps(inp), ws)
            yield "working", desc.strip() + (f": {ctx}" if ctx else "")
        elif b.get("type") == "text":
            t = (b.get("text") or "").strip()
            if t and not t.startswith("```"):
                yield "thinking", t.split("\n", 1)[0]


def writer_argv(kind: str, line: str, task: dict, room: str | None, workspace: Path | None = None) -> list[str]:
    cmd = [sys.executable, str(WRITER), "append", line[:MAXLEN], "--kind", kind, "--task-id", task["id"]]
    if task.get("from"):
        cmd += ["--from", task["from"]]
    if task.get("text"):
        cmd += ["--text", task["text"]]
    if room:
        cmd += ["--room", room]
    if workspace:
        cmd += ["--workspace", str(workspace)]
    return cmd


def emit(kind: str, line: str, task: dict, room: str | None, workspace: Path | None = None) -> None:
    subprocess.run(writer_argv(kind, line, task, room, workspace), capture_output=True)


class Follower:
    """One transcript cursor; step() reads what is new and emits rows for the open task."""

    def __init__(self, p: dict, emit_fn=emit):
        self.p = p
        self.emit = emit_fn
        self.path = newest_transcript(p["projects"])
        self.pos = self.path.stat().st_size if self.path else 0  # start at the end: history is not news
        self.last: str | None = None

    def step(self) -> int:
        cand = newest_transcript(self.p["projects"])
        if cand and cand != self.path:
            self.path, self.pos = cand, 0
        if not self.path:
            return 0
        with open(self.path, encoding="utf-8", errors="replace") as f:
            f.seek(self.pos)
            chunk = f.read()
            self.pos = f.tell()
        n = 0
        for line in chunk.splitlines():
            for kind, text in rows_from(line, self.p["ws"]):
                if text == self.last:
                    continue
                task, room = open_task(self.p["log"])
                if not task:
                    continue
                self.emit(kind, text, task, room, self.p["ws"])
                self.last = text
                n += 1
        return n


def follow(p: dict, sleep=time.sleep) -> None:
    f = Follower(p)
    if not f.path:
        print("no transcript yet", file=sys.stderr)
        return
    while True:
        f.step()
        sleep(POLL_S)


def main(argv: list[str] | None = None, popen=subprocess.Popen, run_follow=follow) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ws = Path(argv[argv.index("--workspace") + 1]) if "--workspace" in argv else None  # tests only
    p = paths(ws)
    if "--daemon" in argv:
        try:
            pid = int(p["pid"].read_text().strip())
            os.kill(pid, 0)
            print(f"already running: {pid}")
            return 0
        except (OSError, ValueError, ProcessLookupError):
            pass
        p["out"].parent.mkdir(parents=True, exist_ok=True)
        with open(p["out"], "a") as out:
            child = popen([sys.executable, str(Path(__file__).resolve())] + (["--workspace", str(ws)] if ws else []),
                          stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        p["pid"].write_text(str(child.pid))
        print(f"started {child.pid}")
        return 0
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    run_follow(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
