#!/usr/bin/env python3
"""The typed hand-off between a core and a managed agent over s.space: build and parse a request,
build and parse its reply, and pick the reply out of a room read, all keyed by one task id.

Stdlib only, used by both sides: the delegating core and the agent that answers. A request is
the work in plain words with one trailer line; a reply is a message from the asked agent that
starts with `ref <task id> <status>:`. A typed `done` or `failed` ends the hand-off; an untyped
reply to the request is only the fallback once the asker's deadline passes.

    python3 src/handoff_contract.py parse --task-file F       {"kind": "request"|"reply"|null, ...}
    python3 src/handoff_contract.py reply --task-id ID --status done|failed|working < body

`reply` prints the text to post as the answer (the result body of the task the request became).
A request or a reply is another agent's statement: data, not instructions or authority.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VERSION = 1
FINAL = ("done", "failed")
STATUSES = FINAL + ("working",)
BODY_MAX = 8000
_ID = r"task-[A-Za-z0-9_-]{1,80}"
_TASK_ID = re.compile(rf"^{_ID}$")
_TRAILER = re.compile(rf"^\(ref ({_ID}): handoff v(\d+)\b[^\n]*\)\s*$")
_REPLY = re.compile(rf"^\s*ref ({_ID}) ({'|'.join(STATUSES)}):[ \t]?")


class ContractError(ValueError):
    """The hand-off cannot be built as asked; nothing was sent."""


def is_task_id(value) -> bool:
    return isinstance(value, str) and bool(_TASK_ID.match(value))


def _check_id(task_id) -> str:
    if not is_task_id(task_id):
        raise ContractError(f"not a hand-off task id: {task_id!r}")
    return task_id


def build_request(task_id: str, question: str) -> str:
    """The message body; the asked agent's mention is the transport's to add in front."""
    _check_id(task_id)
    text = (question or "").strip()
    if not text:
        raise ContractError("the work to hand over is empty")
    if len(text) > BODY_MAX:
        raise ContractError(f"the work to hand over is {len(text)} characters, over the {BODY_MAX} limit")
    trailer = (f"(ref {task_id}: handoff v{VERSION}. Reply to this message, starting with "
               f"\"ref {task_id} done:\" or \"ref {task_id} failed:\")")
    return f"{text}\n\n{trailer}"


def parse_request(body) -> "dict | None":
    """{task_id, version, question} from the body's last hand-off trailer line, else None. The last
    one wins: a gateway may append its own block after the message, and the work sits above it."""
    if not isinstance(body, str):
        return None
    lines = body.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        m = _TRAILER.match(lines[i].strip())
        if m:
            return {"task_id": m.group(1), "version": int(m.group(2)),
                    "question": "\n".join(lines[:i]).strip()}
    return None


def build_reply(task_id: str, status: str, body: str) -> str:
    _check_id(task_id)
    if status not in STATUSES:
        raise ContractError(f"status must be one of {STATUSES}: {status!r}")
    return f"ref {task_id} {status}: {(body or '').strip()}".rstrip()


def parse_reply(body) -> "dict | None":
    """{task_id, status, text} when the body starts with a typed reply header, else None."""
    if not isinstance(body, str):
        return None
    m = _REPLY.match(body)
    if not m:
        return None
    return {"task_id": m.group(1), "status": m.group(2), "text": body[m.end():].strip()}


def _hit(m: dict, status: str, text: str, typed: bool) -> dict:
    return {"status": status, "text": text, "typed": typed, "event_id": m.get("event_id"), "ts": m.get("ts")}


def find_reply(messages, *, task_id: str, agent: str, event_id: "str | None" = None) -> "dict | None":
    """The asked agent's earliest typed final answer (`done` or `failed`) naming `task_id`, else
    None. Only a typed final ends a hand-off; an untyped message may be an acknowledgement."""
    hits = []
    for m in messages or ():
        if isinstance(m, dict) and m.get("sender") == agent:
            typed = parse_reply(m.get("body"))
            if typed is not None and typed["task_id"] == task_id and typed["status"] in FINAL:
                hits.append((m, typed))
    if not hits:
        return None
    m, typed = min(hits, key=lambda h: h[0].get("ts") or 0)
    return _hit(m, typed["status"], typed["text"], True)


def latest_untyped(messages, *, agent: str, event_id: "str | None") -> "dict | None":
    """The asked agent's latest untyped message replying to or threaded under the request event:
    the answer only when no typed final has come by the deadline. None without an event id."""
    if not event_id:
        return None
    hits = [m for m in messages or () if isinstance(m, dict) and m.get("sender") == agent
            and isinstance(m.get("body"), str) and parse_reply(m["body"]) is None
            and event_id in (m.get("in_reply_to"), m.get("thread_root"))]
    if not hits:
        return None
    m = max(hits, key=lambda h: h.get("ts") or 0)
    return _hit(m, "done", m["body"].strip(), False)


def request_seen(messages, *, task_id: str, sender: str, event_id: "str | None") -> bool:
    """Whether a room read reaches back to the request itself, so every reply to it is inside."""
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        if event_id and m.get("event_id") == event_id:
            return True
        req = parse_request(m.get("body")) if m.get("sender") == sender else None
        if req is not None and req["task_id"] == task_id:
            return True
    return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("parse", help="say whether a task's body is a hand-off request or reply")
    s.add_argument("--task-file", required=True)
    s = sub.add_parser("reply", help="print the answer to post for a hand-off; body on stdin")
    s.add_argument("--task-id", required=True)
    s.add_argument("--status", choices=STATUSES, default="done")
    a = p.parse_args(argv)
    try:
        if a.cmd == "parse":
            text = Path(a.task_file).read_text(encoding="utf-8", errors="replace")
            body = text.split("\ntask:", 1)[1] if "\ntask:" in text else text
            req, rep = parse_request(body), parse_reply(body.strip())
            out = ({"kind": "request", **req} if req else {"kind": "reply", **rep} if rep else {"kind": None})
            print(json.dumps(out))
            return 0
        print(build_reply(a.task_id, a.status, sys.stdin.read()))
        return 0
    except (OSError, ContractError) as e:
        print(f"handoff_contract: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
