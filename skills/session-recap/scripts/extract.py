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

sys.path.insert(0, str(REPO / "src"))
from util_paths import claude_project_slug  # noqa: E402


def transcripts_dir() -> Path:
    ws = subprocess.run(
        ["bash", str(REPO / "scripts" / "sutando-config.sh"), "workspace"],
        capture_output=True, text=True, check=True).stdout.strip()
    # Claude Code slugs the project path by dashing every non-alphanumeric
    # character, not just "/" — e.g. ".../Application Support/space.ag2.app/..."
    # becomes "...-Application-Support-space-ag2-app-...". Matching only "/"
    # resolves to a nonexistent dir on any checkout path containing spaces
    # or dots (observed 2026-07-20 on a desktop-bundled engine checkout).
    slug = claude_project_slug(REPO)
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


def is_sidechain_transcript(path: Path) -> bool:
    """True if this transcript is a subagent/sidechain run, not a main session.

    Claude Code writes subagent (Task/Agent) conversations as their own *.jsonl
    in the same project dir; their message events carry ``isSidechain: true``.
    A main session's first user/assistant event is not a sidechain event, so we
    judge by the first *message* event — meta lines (custom-title, last-prompt)
    carry no isSidechain and are skipped. Cheap (stops at the first message) and
    decisive: a sidechain transcript is sidechain from its first message on.
    """
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") in ("user", "assistant"):
                    return bool(d.get("isSidechain"))
    except OSError:
        return False
    return False  # no message events → treat as a (harmless) empty main session


def pick(sessions: list, which: str) -> Path:
    # "current"/"last" must resolve against MAIN sessions only. Subagent/
    # sidechain transcripts share the project dir and can sort newer (by mtime)
    # than the previous main session, so a naive sessions[1] for "last" could
    # return a subagent transcript instead of the prior real session.
    if which in ("current", "last"):
        mains = [s for s in sessions if not is_sidechain_transcript(s)]
        if which == "current":
            if not mains:
                sys.exit("no main session transcript found")
            return mains[0]  # newest-mtime main = the currently-active session
        if len(mains) < 2:
            sys.exit("only one session transcript exists")
        return mains[1]  # second-newest main = the previous session
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
    ap.add_argument("--tail-bytes", type=int, default=0,
                    help="read only the LAST N bytes of the transcript "
                         "(0 = whole file). Bounds dump I/O on multi-GB-class "
                         "transcripts; the boot recap uses 8388608 (8 MiB) so "
                         "boot cost stops scaling with session length. The "
                         "tail keeps the RECENT end — which is what a catchup "
                         "needs (open loops live at the end, not the start).")
    ap.add_argument("--transcripts-dir", default=None,
                    help="read transcripts from this directory instead of the "
                         "resolved workspace project dir. Hook for hermetic "
                         "tests and worktree A/B runs — the supported "
                         "override surface (no env var).")
    args = ap.parse_args()

    tdir = (Path(args.transcripts_dir) if args.transcripts_dir
            else transcripts_dir())
    sessions = sorted(tdir.glob("*.jsonl"),
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
        if args.tail_bytes:
            size = path.stat().st_size
            if size > args.tail_bytes:
                pos = size - args.tail_bytes
                f.seek(pos)
                # Discard the partial line the seek landed in — but only if
                # it IS partial. When pos lands exactly on a record boundary
                # (the previous byte is a newline), an unconditional
                # readline() would silently drop one COMPLETE record.
                with open(path, "rb") as bf:
                    bf.seek(pos - 1)
                    if bf.read(1) != b"\n":
                        f.readline()
                out.append(f"...[tail: last {args.tail_bytes} bytes of "
                           f"{size} — re-run with --tail-bytes 0 for the "
                           "full session]")
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
                    tools = []
                    for b in msg.get("content", []):
                        if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                            continue
                        name = b.get("name", "?")
                        inp = b.get("input") or {}
                        # Surface file artifacts so recaps can list what was
                        # written (owner ask 2026-07-13: notes/files written
                        # belong in the recap, not just conversation).
                        target = inp.get("file_path") or inp.get("notebook_path")
                        if name in ("Write", "Edit", "NotebookEdit") and target:
                            tools.append(f"{name}({target})")
                        else:
                            tools.append(name)
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
