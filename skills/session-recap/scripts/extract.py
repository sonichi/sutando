#!/usr/bin/env python3
"""Extract a readable event stream from Claude Code session transcripts.

Modes:
  list                    — table of sessions (file, start, end, msgs, first user line)
  dump --session <uuid|last|current> [--filter user|dialog|all] [--max-chars N]

"last" = the most recent session file that is NOT the currently-active one
(active = newest by mtime). Timestamps are per-event ISO from the transcript.
The output is designed to be fed to a cheap summarizer model, or grepped
directly for verbatim quotes. Read-only; never modifies transcripts.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_PARENT = Path(__file__).resolve().parent
REPO = SCRIPT_PARENT.parents[2]


def transcripts_dir() -> Path:
    ws = subprocess.run(
        ["bash", str(REPO / "scripts" / "sutando-config.sh"), "workspace"],
        capture_output=True, text=True, check=True).stdout.strip()
    slug = str(REPO).replace("/", "-")
    return Path(ws) / ".claude-sutando" / "projects" / slug


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def session_meta(path: Path) -> dict:
    start = end = first_user = None
    users = assistants = 0
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = d.get("timestamp")
            if ts:
                start = start or ts
                end = ts
            t = d.get("type")
            if t == "user":
                users += 1
                if not first_user:
                    txt = text_of(d.get("message", {}).get("content"))
                    if txt.strip():
                        first_user = " ".join(txt.split())[:100]
            elif t == "assistant":
                assistants += 1
    return {"file": path.name, "start": start, "end": end,
            "user_msgs": users, "assistant_msgs": assistants,
            "size_kb": path.stat().st_size // 1024, "first_user": first_user}


def pick(sessions: list, which: str) -> Path:
    # newest-mtime = the currently-active session
    if which == "current":
        return sessions[0]
    if which == "last":
        if len(sessions) < 2:
            sys.exit("only one session transcript exists")
        return sessions[1]
    for p in sessions:
        if p.name.startswith(which):
            return p
    sys.exit(f"no session matching {which!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["list", "dump"])
    ap.add_argument("--session", default="last")
    ap.add_argument("--filter", default="dialog",
                    choices=["user", "dialog", "all"],
                    help="user: owner/user messages only (verbatim); "
                         "dialog: user + assistant text; "
                         "all: dialog + tool-call names + system lines")
    ap.add_argument("--max-chars", type=int, default=200_000)
    args = ap.parse_args()

    sessions = sorted(transcripts_dir().glob("*.jsonl"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    if not sessions:
        sys.exit("no transcripts found")

    if args.mode == "list":
        for p in sessions[:15]:
            m = session_meta(p)
            print(json.dumps(m, ensure_ascii=False))
        return

    path = pick(sessions, args.session)
    out: list = []
    total = 0
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t, ts = d.get("type"), (d.get("timestamp") or "")[:19]
            piece = None
            if t == "user":
                txt = text_of(d.get("message", {}).get("content")).strip()
                if txt:
                    piece = f"[{ts}] USER: {txt}"
            elif t == "assistant" and args.filter in ("dialog", "all"):
                msg = d.get("message", {})
                txt = text_of(msg.get("content")).strip()
                if txt:
                    piece = f"[{ts}] ASSISTANT: {txt}"
                elif args.filter == "all":
                    tools = [b.get("name") for b in msg.get("content", [])
                             if isinstance(b, dict) and b.get("type") == "tool_use"]
                    if tools:
                        piece = f"[{ts}] TOOLS: {', '.join(tools)}"
            elif t == "system" and args.filter == "all":
                txt = (d.get("content") or d.get("message") or "")
                if isinstance(txt, str) and txt.strip():
                    piece = f"[{ts}] SYSTEM: {' '.join(txt.split())[:200]}"
            if piece:
                total += len(piece)
                if args.max_chars and total > args.max_chars:
                    out.append(f"...[truncated at {args.max_chars} chars; "
                               "re-run with --max-chars 0 for no cap]")
                    break
                out.append(piece)
    print("\n".join(out))


if __name__ == "__main__":
    main()
