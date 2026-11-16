#!/usr/bin/env python3
"""The owner-mention playbook's only writer of its result: dedupes, batches, quotes, publishes.

One notice per mentioned room per window (default 10 minutes). A mention inside the window is
kept as pending and goes out with the next notice that is sent, for that room or any other; a
pending line is never sent twice. A message already noted is skipped.

The backend (s.space `owner.mentioned_by_*`) queues both of its shapes in the owner DM, so the
result posts there and carries no redirect: a notice (no message text) and an engage task (a
quoted excerpt). The room and message are read only from the server's own briefing lines; the
excerpt is one JSON-quoted line that starts with the sender, so it cannot forge one.
Every line is quoted with `result_markers.neutralize_markers`, so a name or excerpt the note
repeats cannot act as a result marker. State lives in `<workspace>/state/owner-mention/`; when
it cannot be written (a sandboxed turn) the note is sent unbatched rather than lost.

Usage:
  owner_notice.py publish --task-file <task> <result path> [--window-min N] [--workspace DIR]
      the one-line note on stdin; prints `sent`, `batched` or `skip: <why>`
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

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
import file_lock, local_task_protocol, result_markers  # noqa: E401,E402

PUBLISHER = REPO / "src" / "result_publish.py"
SKIP_BODY = "[no-send]"
WINDOW_MIN = 10
PENDING_MAX = 20
SEEN_MAX = 500
_ROOM_ID = r"![A-Za-z0-9._=\-]+:[A-Za-z0-9.\-]+(?::\d+)?"
_ENGAGE_LINE_RE = re.compile(
    rf"^2\. You may answer the message in the room ({_ROOM_ID}) \(reply to (\$[^)\s]+)\)", re.M)
_NOTICE_LINE_RE = re.compile(r"^.* is addressing your owner .*$", re.M)
_LINK_RE = re.compile(r"https://matrix\.to/#/(%21[^/\s]+)/(%24\S+?)(?=[\s,]|$)")


def mention_of(task_text: str) -> dict:
    """{mode, room, event}: engage when the server's reply line is there, else notice; room
    and event None when the task names none."""
    body = local_task_protocol.parse_task_headers(task_text).body
    engage = _ENGAGE_LINE_RE.search(body)
    if engage:
        return {"mode": "engage", "room": engage.group(1), "event": engage.group(2)}
    from urllib.parse import unquote
    line = _NOTICE_LINE_RE.search(body)
    links = _LINK_RE.findall(line.group(0)) if line else []
    room, event = (unquote(links[-1][0]), unquote(links[-1][1])) if links else (None, None)
    return {"mode": "notice", "room": room, "event": event}


def _state_path(workspace: Path) -> Path:
    return workspace / "state" / "owner-mention" / "notices.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def decide(state: dict, room: str, event: "str | None", note: str, now: float,
           window_s: float) -> "tuple[str, list[str]]":
    """('skip'|'batched'|'sent', lines to send). Mutates `state`."""
    seen = state.setdefault("seen", [])
    rooms = state.setdefault("rooms", {})
    if event and event in seen:
        return "skip", []
    if event:
        seen.append(event)
        del seen[:-SEEN_MAX]
    entry = rooms.setdefault(room, {"last": 0, "pending": []})
    due = []
    for other, rec in rooms.items():
        if other != room and rec.get("pending") and now - rec.get("last", 0) >= window_s:
            due += rec["pending"]
            rec.update(pending=[], last=now)
    if now - entry.get("last", 0) < window_s:
        entry["pending"] = (entry.get("pending") or []) + [note]
        del entry["pending"][:-PENDING_MAX]
        return ("sent", due) if due else ("batched", [])
    lines = [note] + (entry.get("pending") or []) + due
    entry.update(pending=[], last=now)
    return "sent", lines


def compose(lines: list) -> str:
    head, rest = lines[0], lines[1:]
    body = head if not rest else head + "\n" + "\n".join(f"Also: {x}" for x in rest)
    return result_markers.neutralize_markers(body.strip()) + "\n"


def publish(result: str, text: str, extra: list) -> int:
    return subprocess.run([sys.executable, str(PUBLISHER), *extra, result], input=text,
                          text=True).returncode


def _workspace(arg: "str | None") -> Path:
    if arg:
        return Path(arg)
    from workspace_default import resolve_workspace
    return resolve_workspace(migrate=False)


def run(task_text: str, note: str, *, workspace: Path, now: float,
        window_s: float) -> "tuple[str, str | None]":
    """(verdict, result body or None for [no-send]); the state write is best-effort."""
    m = mention_of(task_text)
    note = " ".join(note.split())
    room = m["room"] or "unknown"
    path = _state_path(workspace)
    try:
        with file_lock.locked_file(path.with_suffix(".lock"), create_mode=0o600):
            state = _read(path)
            verdict, lines = decide(state, room, m["event"], note, now, window_s)
            _write(path, state)
    except OSError as e:
        print(f"owner_notice: state unwritable ({type(e).__name__}); sending unbatched",
              file=sys.stderr)
        verdict, lines = "sent", [note]
    if verdict != "sent":
        return ("skip: already noted" if verdict == "skip" else "batched"), None
    return "sent", compose(lines)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish")
    p.add_argument("--task-file", required=True, type=Path)
    p.add_argument("result")
    p.add_argument("--window-min", type=float, default=WINDOW_MIN)
    p.add_argument("--workspace")
    for flag in ("--title", "--outcome"):
        p.add_argument(flag)
    args = ap.parse_args(argv)
    note = sys.stdin.read()
    if not note.strip():
        print("owner_notice: empty note on stdin; nothing published", file=sys.stderr)
        return 2
    task_text = args.task_file.read_text(encoding="utf-8", errors="replace")
    verdict, body = run(task_text, note, workspace=_workspace(args.workspace), now=time.time(),
                        window_s=args.window_min * 60)
    print(verdict)
    extra = [x for flag in ("title", "outcome") if getattr(args, flag)
             for x in (f"--{flag}", getattr(args, flag))]
    return publish(args.result, body if body is not None else SKIP_BODY + "\n", extra)


if __name__ == "__main__":
    raise SystemExit(main())
