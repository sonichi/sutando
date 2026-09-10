#!/usr/bin/env python3
"""The worker picker's buttons arrive as ordinary tasks; this reads their intent.

The broker turns each button into a normal task addressed to this instance —
`source: worker-picker`, an id prefixed `worker-add-` or `worker-pin-`, and a
sentence of English. So the intent has to be recovered, and two rules keep that
honest:

  * the ROOM comes from the `channel_id` header, never from the sentence —
    a room-scoped button with no stamped room is REFUSED, not guessed;
  * `source` must be the header the gateway stamped — prose that merely says
    "worker picker" grants nothing, or anyone who can send a message could
    create workers.

Both rules rest on one mechanism: every field this module trusts is read with
the STRICT parser, which stops at `task:`, so the body cannot supply any of
them. `parse_task_headers_lenient` scans the whole file and would let a body
line supply a key the file legitimately lacks, which is exactly the forgery
these two rules exist to prevent — it must never be used here. The producer
side of that bargain: `source` and `channel_id` must be written ABOVE `task:`,
the slot `requested_worker` and `priority` already occupy, or this reader
sees neither and refuses.

It returns intent. Acting on one is the caller's, so a misparse cannot spawn.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Sibling skill scripts resolve from this directory; core helpers (the task
# protocol) from the repo root (parents[3] of skills/<name>/scripts/<file>.py).
_SCRIPTS = Path(__file__).resolve().parent
for _p in (str(_SCRIPTS), str(_SCRIPTS.parents[2] / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import local_task_protocol as ltp  # noqa: E402

SOURCE = "worker-picker"

# A header the broker may stamp instead of relying on the sentence. Read from
# ABOVE `task:` only, like `requested_worker`, so a body cannot forge one.
COMMAND_HEADER = "picker_command"
COMMAND_ARGS_HEADER = "picker_args"
COMMANDS = ("add", "pin", "unpin")

_ADD = re.compile(r"^Add a new worker to the pool\b", re.I)
_LABEL = re.compile(r"Preferred label for the new worker:\s*(.+?)\.?\s*$", re.I)
_UNPIN = re.compile(r"^Unpin room\s+(\S+)", re.I)
_DEDICATE = re.compile(r"^Dedicate room\s+(\S+)\s+to\s+(.+?)\s+—", re.I)
_PIN_SET = re.compile(r"^Pin room\s+(\S+)\s+to workers\s+(.+?)\s+—", re.I)
_PIN_ONE = re.compile(r"^Pin room\s+(\S+)\s+to\s+(\S+)\s*\(", re.I)


def _names(blob: str) -> list:
    return [w for w in blob.split() if w]


def _refuse_no_room(action: str) -> None:
    """Say why a room-scoped button was dropped: silence here reads as 'the
    sentence did not match', which is a different and much less alarming fault."""
    print(f"worker-picker: refusing {action} — no channel_id header",
          file=sys.stderr)


def _structured(headers: dict, room: str) -> "dict | None":
    """The intent from a stamped command, or None when there is no usable one.

    An UNKNOWN command returns None rather than falling through to the prose:
    a broker that names a verb we do not implement must not be answered by
    guessing from a sentence written for a different one.
    """
    name = (headers.get(COMMAND_HEADER) or "").strip()
    if not name:
        return None
    if name not in COMMANDS:
        return {"action": "unsupported", "command": name}
    args = {}
    raw = (headers.get(COMMAND_ARGS_HEADER) or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            args = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            # A stamped command we cannot read is a refusal, not a reason to
            # trust the sentence: nothing proves the two say the same thing.
            return {"action": "malformed", "command": name}
    if name == "add":
        label = args.get("label")
        return {"action": "add", "label": str(label) if label else None}
    workers = args.get("workers") or ([args["worker"]] if args.get("worker") else [])
    at = room or str(args.get("room") or "")
    if name == "unpin":
        return {"action": "unpin", "room": at}
    return {"action": "pin", "room": at,
            "workers": [str(w) for w in workers],
            "dedicated": bool(args.get("dedicated"))}


def parse(headers: dict, body: str) -> "dict | None":
    """The intent behind one task, or None when it is not the picker's.

    `headers` must come from the strict parser (see module docstring): every
    key read below is an authorization field, and a lenient scan would let the
    body supply one. A stamped command wins over the sentence; the sentence is
    the fallback for a broker that stamps none. An unrecognised sentence
    returns None rather than a guess, and so does a room-scoped sentence with
    no stamped room: a wrong intent creates or re-routes a worker.
    """
    if (headers.get("source") or "").strip() != SOURCE:
        return None
    text = " ".join((body or "").split())
    room = (headers.get("channel_id") or "").strip()

    structured = _structured(headers, room)
    if structured is not None:
        return structured

    if _ADD.search(text):
        m = _LABEL.search(text)
        return {"action": "add", "label": m.group(1).strip() if m else None}

    if _UNPIN.search(text):
        if not room:
            _refuse_no_room("unpin")
            return None
        return {"action": "unpin", "room": room}
    m = _DEDICATE.search(text)
    if m:
        if not room:
            _refuse_no_room("dedicate")
            return None
        return {"action": "pin", "room": room,
                "workers": _names(m.group(2)), "dedicated": True}
    m = _PIN_SET.search(text)
    if m:
        if not room:
            _refuse_no_room("pin")
            return None
        return {"action": "pin", "room": room,
                "workers": _names(m.group(2)), "dedicated": False}
    m = _PIN_ONE.search(text)
    if m:
        if not room:
            _refuse_no_room("pin")
            return None
        return {"action": "pin", "room": room,
                "workers": [m.group(2)], "dedicated": False}
    return None


def parse_task_file(path) -> "dict | None":
    """Read one task file. The STRICT parser is the whole trust boundary here:
    it stops at `task:`, so no line of body text reaches `parse` as a header."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    parsed = ltp.parse_task_headers(text)
    return parse(parsed.headers, parsed.body or "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="read a worker-picker task's intent")
    ap.add_argument("--task-file", required=True)
    a = ap.parse_args(argv)
    intent = parse_task_file(a.task_file)
    if intent is None:
        print("worker-picker: not a picker command", file=sys.stderr)
        return 3
    print(json.dumps(intent, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
