#!/usr/bin/env python3
"""The owner-mention playbook's only writer of its result: dedupes, batches, quotes, publishes.

One notice per mentioned room per window (default 10 minutes). A mention inside the window is
kept as pending and goes out with the next notice that is sent, for that room or any other; a
pending line is never sent twice. A message already noted is skipped.

The backend (s.space `owner.mentioned_by_*`) queues both of its shapes in the owner DM, so the
result posts there and carries no redirect. The gateway folds the briefing onto one line
(`task: ...`), so the message is read from the LAST matrix.to link in the body: everything a
sender controls (their name, the room's name, the JSON-quoted excerpt) comes before the
server's own link, and only fixed text after it.

Lines leave state only with a durable result. The lines a notice drains are kept in a
`publishing` record until the publisher returns 0. A failure (non-zero) marks it `failed`, and
its lines go out with this task's retry or, folded in, with the next notice. A record a crash
left `publishing` is settled, never blindly replayed: the publisher writes atomically, so its
result at its path (or in the archive) means published, and none means failed. Every line is
made one line with nothing hidden in it, the message opens with `Mention notice:` (what the
owner's client reads as notices), and it is quoted with `result_markers.neutralize_markers`. When the
state cannot be written (a read-only turn) the note is sent unbatched rather than lost.

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
from urllib.parse import unquote

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
import file_lock, local_task_protocol, result_markers  # noqa: E401,E402

PUBLISHER = REPO / "src" / "result_publish.py"
SKIP_BODY = "[no-send]"
WINDOW_MIN = 10
PENDING_MAX = 20
SEEN_MAX = 500
# quote(safe=''): a room id is %21..., an event id %24..., only unreserved chars and %XX.
_LINK_RE = re.compile(r"https://matrix\.to/#/(%21[A-Za-z0-9._~%-]+)/(%24[A-Za-z0-9._~%-]+)")


def mention_of(task_text: str) -> dict:
    """{room, event} from the server's own link, the last in the body; None when absent."""
    body = local_task_protocol.parse_task_headers(task_text).body
    links = _LINK_RE.findall(body)
    if not links:
        return {"room": None, "event": None}
    return {"room": unquote(links[-1][0]), "event": unquote(links[-1][1])}


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


# The owner's client reads a message as notices only when it opens with this (cinny-webclient
# #4463); every later notice in it is an "Also:" line.
MARKER = "Mention notice:"
# Control, zero-width and bidi characters: a quoted name or excerpt never breaks or reorders a line.
_HIDDEN = re.compile("[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2028\u2029\u2066-\u2069]")
_OWN_MARKER = re.compile(r"^\s*mention notice:\s*", re.I)


def clean(note: str) -> str:
    """One line, nothing hidden in it, and no marker of its own: compose() adds that."""
    return _OWN_MARKER.sub("", " ".join(_HIDDEN.sub(" ", note).split()))


def compose(lines: list) -> str:
    head, rest = lines[0], lines[1:]
    body = f"{MARKER} {head}"
    if rest:
        body += "\n" + "\n".join(f"Also: {x}" for x in rest)
    return result_markers.neutralize_markers(body.strip()) + "\n"


def published(result: str) -> bool:
    """Whether `result` was written: still at its path, or already moved to the archive."""
    path = Path(result)
    if path.exists():
        return True
    archive = path.parent / "archive"
    return archive.is_dir() and any(archive.rglob(f"{path.stem}*"))


def publish(result: str, text: str, extra: list) -> int:
    return subprocess.run([sys.executable, str(PUBLISHER), *extra, result], input=text,
                          text=True).returncode


def _workspace(arg: "str | None") -> Path:
    if arg:
        return Path(arg)
    from workspace_default import resolve_workspace
    return resolve_workspace(migrate=False)


def _settle(inflight: dict) -> None:
    """A record left `publishing` by a crash: the publisher writes atomically, so its result is
    either at its path (or archived), published, or nowhere, a definite failure."""
    for key, record in list(inflight.items()):
        if record.get("status") == "publishing":
            if published(record.get("result", "")):
                del inflight[key]
            else:
                record["status"] = "failed"


def run(task_text: str, note: str, result: str, extra: list, *, workspace: Path, now: float,
        window_s: float, publisher=publish) -> "tuple[str, int]":
    """(verdict, publisher return code). A drained line stays in a `publishing` record until
    the publisher returns 0; a failed one goes out with this task's retry or the next notice."""
    m = mention_of(task_text)
    note = clean(note)
    room, key = m["room"] or "unknown", m["event"] or f"result:{result}"
    path = _state_path(workspace)
    try:
        with file_lock.locked_file(path.with_suffix(".lock"), create_mode=0o600):
            state = _read(path)
            inflight = state.setdefault("publishing", {})
            _settle(inflight)
            retry = inflight.pop(key, None)
            if retry is not None:
                lines = retry["lines"]
            else:
                verdict, lines = decide(state, room, m["event"], note, now, window_s)
                if verdict != "sent":
                    _write(path, state)
                    rc = publisher(result, SKIP_BODY + "\n", extra)
                    return ("skip: already noted" if verdict == "skip" else "batched"), rc
            for other in [k for k, r in inflight.items() if r.get("status") == "failed"]:
                lines = lines + inflight.pop(other)["lines"]
            inflight[key] = {"status": "publishing", "result": result, "lines": lines}
            _write(path, state)
            rc = publisher(result, compose(lines), extra)
            if rc == 0:
                del inflight[key]
            else:
                inflight[key]["status"] = "failed"
            _write(path, state)
            return "sent", rc
    except OSError as e:
        print(f"owner_notice: state unwritable ({type(e).__name__}); sending unbatched",
              file=sys.stderr)
        return "sent", publisher(result, compose([note]), extra)


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
    extra = [x for flag in ("title", "outcome") if getattr(args, flag)
             for x in (f"--{flag}", getattr(args, flag))]
    verdict, rc = run(task_text, note, args.result, extra, workspace=_workspace(args.workspace),
                      now=time.time(), window_s=args.window_min * 60)
    print(verdict)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
