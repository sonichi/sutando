#!/usr/bin/env python3
"""What is each Claude session actually doing? — a digest of live transcripts.

A pool session's transcript is the only complete record of its reasoning and
context, but they run 4-13 MB and grow, so reading one to answer "is core-2
stuck, and on what?" is impractical. `tmux capture-pane` shows only what fits on
screen and dies with the pane; the heartbeat proves the process is alive, not
that the agent is progressing.

This streams each transcript once, keeping a bounded tail, and prints the few
lines that answer the question: what it last said, what it last ran, and how
long ago.

    python3 scripts/pool-session-digest.py                 # every live session
    python3 scripts/pool-session-digest.py -s core-2 -n 20 # one, deeper
    python3 scripts/pool-session-digest.py --thinking      # include reasoning

Read-only. Transcripts are live session state; never write to them.
"""
from __future__ import annotations

import argparse
import calendar
import collections
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from util_paths import claude_home_path  # noqa: E402


def config_dir() -> Path:
    """The Claude home, via the ONE resolver that also honours $CLAUDE_HOME."""
    return claude_home_path()


def live_sessions() -> "tuple[list[dict], str | None]":
    """(sessions, reason). `reason` is None ONLY when the CLI answered cleanly.

    Every failure used to return [], so "no sessions running" and "the CLI is
    gone / hung / unauthenticated / speaking HTML" rendered identically — on a
    tool whose job is telling an operator whether cores are alive.
    """
    try:
        out = subprocess.run(["claude", "agents", "--json"],
                             capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return [], "`claude` is not on PATH"
    except subprocess.TimeoutExpired:
        return [], "`claude agents --json` timed out after 30s"
    except OSError as exc:
        return [], f"could not run `claude`: {exc.strerror or exc}"
    if out.returncode != 0:
        detail = _one_line(out.stderr or out.stdout, 120) or "no output"
        return [], f"`claude agents --json` exited {out.returncode}: {detail}"
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return [], f"`claude agents --json` was not JSON: {_one_line(out.stdout, 120)!r}"
    # Shape matters as much as parseability: {"sessions": []} parses fine and
    # then raises AttributeError on the first .get() downstream.
    if not isinstance(data, list):
        return [], f"expected a JSON list of session objects, got {type(data).__name__}"
    bad = next((d for d in data if not isinstance(d, dict)), None)
    if bad is not None:
        return [], ("expected a JSON list of session OBJECTS, got a list "
                    f"containing {type(bad).__name__}")
    return data, None


_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def find_transcript(session_id: str) -> "Path | None":
    """Locate a transcript by LITERAL filename.

    The id comes from session metadata, so interpolating it into a glob let
    `*` match an unrelated session's transcript. Validate, then match exactly.
    """
    if not _SESSION_ID.match(session_id or ""):
        return None
    name = f"{session_id}.jsonl"
    projects = config_dir() / "projects"
    try:
        dirs = sorted(d for d in projects.iterdir() if d.is_dir())
    except OSError:
        return None
    for d in dirs:
        cand = d / name
        if cand.is_file():
            return cand
    return None


# C0, DEL and C1. Whitespace is collapsed before this runs, so anything left is
# a control the terminal would ACT on (OSC 52 writes the clipboard).
_CTRL = {c: "\ufffd" for c in [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)]}


def _safe(value) -> str:
    """Neutralize terminal controls in untrusted transcript/session content.

    This digest prints straight to a TTY, so an escape that survives here can
    spoof output or drive terminal features. Replaced, not dropped, so a reader
    can see something was removed.
    """
    return str(value if value is not None else "").translate(_CTRL)


def _one_line(text: str, width: int) -> str:
    flat = _safe(" ".join((text or "").split()))
    return flat[:width] + ("…" if len(flat) > width else "")


def _event(rec: dict, block: dict, width: int) -> "tuple[str, str, str] | None":
    """(ts, kind, summary) for a content block worth showing, else None."""
    ts = _safe((rec.get("timestamp") or "")[11:19])
    kind = block.get("type")
    if kind == "text":
        body = _one_line(block.get("text", ""), width)
        return (ts, "SAY", body) if body else None
    if kind == "thinking":
        body = _one_line(block.get("thinking", ""), width)
        return (ts, "THINK", body) if body else None
    if kind == "tool_use":
        name = _safe(block.get("name", "?"))
        inp = block.get("input") or {}
        detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
        if not detail and isinstance(inp, dict) and inp:
            detail = json.dumps(inp)[:width]
        return (ts, name.upper()[:6], _one_line(str(detail), width))
    return None


def digest(path: Path, keep: int, width: int, want_thinking: bool) -> dict:
    records = 0
    blocks: collections.Counter = collections.Counter()
    tail: collections.deque = collections.deque(maxlen=keep)
    last_ts = ""
    with path.open(errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            records += 1
            if rec.get("timestamp"):
                last_ts = rec["timestamp"]
            content = (rec.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                blocks[block.get("type", "?")] += 1
                if block.get("type") == "thinking" and not want_thinking:
                    continue
                ev = _event(rec, block, width)
                if ev:
                    tail.append(ev)
    return {"records": records, "blocks": blocks, "tail": list(tail), "last_ts": last_ts}


def age(iso: str) -> str:
    if not iso:
        return "?"
    try:
        # timegm, not mktime: transcripts stamp UTC, and mktime reads local —
        # correcting with time.timezone ignores DST and lands an hour out.
        t = calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return "?"
    delta = int(time.time() - t)
    # Clamping a future stamp to 0 renders skew as "0s ago" — identical to a
    # genuinely fresh core, on the column operators read as the wedge signal.
    if delta < -2:
        return "clock skew"
    secs = max(0, delta)
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-s", "--session", help="name or id substring; default all")
    ap.add_argument("-n", type=int, default=8, help="events to show (default 8)")
    ap.add_argument("-w", "--width", type=int, default=110, help="summary width")
    ap.add_argument("--thinking", action="store_true", help="include reasoning")
    a = ap.parse_args()

    sessions, reason = live_sessions()
    if reason is not None:
        print(f"could not list sessions: {reason}", file=sys.stderr)
        return 2                      # distinct from "asked, answered, none"
    if not sessions:
        print("no live sessions (the CLI answered, none are running)",
              file=sys.stderr)
        return 1
    if a.session:
        needle = a.session.lower()
        sessions = [s for s in sessions
                    if needle in s.get("name", "").lower()
                    or needle in s.get("sessionId", "").lower()]
        if not sessions:
            print(f"no session matching {a.session!r}", file=sys.stderr)
            return 1

    for sess in sessions:
        name, sid = sess.get("name", "?"), sess.get("sessionId", "")
        head = _safe(f"{name}  [{sess.get('status','?')}]  pid={sess.get('pid','?')}")
        path = find_transcript(sid)
        if path is None:
            print(f"\n{head}\n  no transcript for {_safe(sid)}")
            continue
        d = digest(path, a.n, a.width, a.thinking)
        mb = path.stat().st_size / 1048576
        counts = " · ".join(f"{_safe(k)} {v}" for k, v in d["blocks"].most_common(5))
        print(f"\n{head}")
        print(f"  {mb:.1f}M · {d['records']} records · last {age(d['last_ts'])}")
        print(f"  {counts}")
        for ts, kind, body in d["tail"]:
            print(f"    {ts}  {kind:<6} {body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
