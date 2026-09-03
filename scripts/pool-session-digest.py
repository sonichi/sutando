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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))  # lint-workspace-resolution: allow-repo-root

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
    if any(not isinstance(d, dict) for d in data):
        # `next(..., None)` cannot work here: None is itself an invalid member,
        # so a [null] list would report "no bad member found".
        kinds = sorted({type(d).__name__ for d in data if not isinstance(d, dict)})
        return [], ("expected a JSON list of session OBJECTS, got a list "
                    f"containing {', '.join(kinds)}")
    for d in data:
        for field in ("name", "sessionId"):
            if field in d and not isinstance(d[field], str):
                return [], (f"session {field!r} must be a string, got "
                            f"{type(d[field]).__name__}")
    return data, None


_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class TranscriptLookupError(OSError):
    """The search root could not be read, so nothing is known about the transcript."""


def find_transcript(session_id: str) -> "Path | None":
    """Locate a transcript by LITERAL filename.

    The id comes from session metadata, so interpolating it into a glob let
    `*` match an unrelated session's transcript. Validate, then match exactly.

    None means the root was searched and holds no match. An unreadable or
    missing root raises TranscriptLookupError: it is an unknown, not an absence.
    """
    if not _SESSION_ID.match(session_id or ""):
        return None
    name = f"{session_id}.jsonl"
    projects = config_dir() / "projects"
    try:
        for d in sorted(d for d in projects.iterdir() if d.is_dir()):
            cand = d / name
            if cand.is_file():
                return cand
    except OSError as exc:
        raise TranscriptLookupError(
            f"cannot search {projects}: {exc.strerror or exc}") from exc
    return None


# C0, DEL and C1. Whitespace is collapsed before this runs, so anything left is
# a control the terminal would ACT on (OSC 52 writes the clipboard).
_CTRL = {c: "\ufffd" for c in [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)]}
# A lone surrogate survives json.loads but not a UTF-8 stdout; one bad string
# would end the sweep and hide every later session.
_CTRL.update({c: "\ufffd" for c in range(0xD800, 0xE000)})


def _safe(value) -> str:
    """Neutralize terminal controls in untrusted transcript/session content.

    This digest prints straight to a TTY, so an escape that survives here can
    spoof output or drive terminal features. Replaced, not dropped, so a reader
    can see something was removed.
    """
    return str(value if value is not None else "").translate(_CTRL)


def _one_line(text, width: int) -> str:
    # Leaf values are as untrusted as the record: a dict where a string belongs
    # must render empty, not raise and take the rest of the transcript with it.
    if not isinstance(text, str):
        return ""
    flat = _safe(" ".join(text.split()))
    return flat[:width] + ("…" if len(flat) > width else "")


def _event(rec: dict, block: dict, width: int) -> "tuple[str, str, str] | None":
    """(ts, kind, summary) for a content block worth showing, else None."""
    raw_ts = rec.get("timestamp")
    ts = _safe(raw_ts[11:19]) if isinstance(raw_ts, str) else ""
    kind = block.get("type")
    if kind == "text":
        body = _one_line(block.get("text"), width)
        return (ts, "SAY", body) if body else None
    if kind == "thinking":
        body = _one_line(block.get("thinking"), width)
        return (ts, "THINK", body) if body else None
    if kind == "tool_use":
        name = block.get("name")
        name = _safe(name) if isinstance(name, str) else "?"
        inp = block.get("input")
        if isinstance(inp, dict):
            detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
            if not detail and inp:
                detail = json.dumps(inp, default=str)[:width]
        else:
            detail = "" if inp is None else str(inp)[:width]
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
            # A transcript is an external artifact: every decoded level is
            # untrusted, and one bad record must not end the whole digest.
            if not isinstance(rec, dict):
                continue
            records += 1
            if isinstance(rec.get("timestamp"), str):
                last_ts = rec["timestamp"]
            msg = rec.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "?")
                if not isinstance(btype, str):
                    btype = "?"          # an unhashable type would raise here
                blocks[btype] += 1
                if btype == "thinking" and not want_thinking:
                    continue
                try:
                    ev = _event(rec, block, width)
                except Exception:  # noqa: BLE001
                    # Backstop for future _event() edits; no JSON value reaches it today.
                    ev = None
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

    failed = 0
    for sess in sessions:
        name, sid = sess.get("name", "?"), sess.get("sessionId", "")
        head = _safe(f"{name}  [{sess.get('status','?')}]  pid={sess.get('pid','?')}")
        try:
            path = find_transcript(sid)
        except TranscriptLookupError as e:
            # Rendered apart from no-match: "could not look" is not "looked, none".
            print(f"\n{head}\n  transcript lookup failed: {_one_line(str(e), 120)}")
            failed += 1
            continue
        if path is None:
            print(f"\n{head}\n  no transcript for {_safe(sid)}")
            continue
        try:
            d = digest(path, a.n, a.width, a.thinking)
            mb = path.stat().st_size / 1048576
        except Exception as e:      # noqa: BLE001 — one session must not end the sweep
            print(f"\n{head}\n  unreadable transcript: "
                  f"{_safe(type(e).__name__)}: {_safe(_one_line(str(e), 90))}")
            failed += 1
            continue
        counts = " · ".join(f"{_safe(k)} {v}" for k, v in d["blocks"].most_common(5))
        print(f"\n{head}")
        print(f"  {mb:.1f}M · {d['records']} records · last {age(d['last_ts'])}")
        print(f"  {counts}")
        for ts, kind, body in d["tail"]:
            print(f"    {ts}  {kind:<6} {body}")
    if failed and failed == len(sessions):
        return 2                  # every session unreadable is a failure, not a report
    return 0


if __name__ == "__main__":
    sys.exit(main())
