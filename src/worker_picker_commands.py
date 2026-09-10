#!/usr/bin/env python3
"""The worker picker's buttons arrive as ordinary tasks; this reads their intent.

The broker turns each button into a normal task addressed to this instance —
`source: worker-picker`, an id prefixed `worker-add-` or `worker-pin-`, and a
sentence of English. So the intent has to be recovered, and two rules keep that
honest:

  * the ROOM comes from the `channel_id` header, never from the sentence;
  * `source` must be the header the gateway stamped — prose that merely says
    "worker picker" grants nothing, or anyone who can send a message could
    create workers.

It returns intent. Acting on one is the caller's, so a misparse cannot spawn.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_task_protocol as ltp  # noqa: E402

SOURCE = "worker-picker"

_ADD = re.compile(r"^Add a new worker to the pool\b", re.I)
_LABEL = re.compile(r"Preferred label for the new worker:\s*(.+?)\.?\s*$", re.I)
_UNPIN = re.compile(r"^Unpin room\s+(\S+)", re.I)
_DEDICATE = re.compile(r"^Dedicate room\s+(\S+)\s+to\s+(.+?)\s+—", re.I)
_PIN_SET = re.compile(r"^Pin room\s+(\S+)\s+to workers\s+(.+?)\s+—", re.I)
_PIN_ONE = re.compile(r"^Pin room\s+(\S+)\s+to\s+(\S+)\s*\(", re.I)


def _names(blob: str) -> list:
    return [w for w in blob.split() if w]


def parse(headers: dict, body: str) -> "dict | None":
    """The intent behind one task, or None when it is not the picker's.

    An unrecognised sentence from a genuine picker task returns None rather
    than a guess: a wrong intent here creates or re-routes a worker.
    """
    if (headers.get("source") or "").strip() != SOURCE:
        return None
    text = " ".join((body or "").split())
    room = (headers.get("channel_id") or "").strip()

    if _ADD.search(text):
        m = _LABEL.search(text)
        return {"action": "add", "label": m.group(1).strip() if m else None}

    m = _UNPIN.search(text)
    if m:
        return {"action": "unpin", "room": room or m.group(1)}
    m = _DEDICATE.search(text)
    if m:
        return {"action": "pin", "room": room or m.group(1),
                "workers": _names(m.group(2)), "dedicated": True}
    m = _PIN_SET.search(text)
    if m:
        return {"action": "pin", "room": room or m.group(1),
                "workers": _names(m.group(2)), "dedicated": False}
    m = _PIN_ONE.search(text)
    if m:
        return {"action": "pin", "room": room or m.group(1),
                "workers": [m.group(2)], "dedicated": False}
    return None


def parse_task_file(path) -> "dict | None":
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    parsed = ltp.parse_task_headers_lenient(text)
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
