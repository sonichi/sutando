"""Print the routing fields of every open task file in a task dir as one JSON array.

The Hatch hook script calls this so its wake payload carries structured fields read
with Sparrow's own header parser, never prose scraped by the shell."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ... import local_task_protocol

SNIPPET_MAX = 500
# Matrix user id: @localpart:server_name, where server_name may be a DNS name, IPv4 or
# [IPv6] literal, each with an optional :port.
MXID_RE = re.compile(r"^@[A-Za-z0-9._=+/-]+:(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]{1,5})?$")
EXIT_BAD_AGENT = 3


def valid_mxid(value: str) -> bool:
    return bool(MXID_RE.match(value or ""))


def attested_headers(text: str) -> "local_task_protocol.TaskHeaders":
    """Headers a tier or routing decision may read: the strict task-last parse, or the
    trusted last-wins scan only when `task_layout: mid` sits above `task:`, where no
    body text can claim it. The gateway writes access_tier last, below the body."""
    strict = local_task_protocol.parse_task_headers(text)
    if (strict.headers.get("task_layout") or "").strip() == "mid":
        return local_task_protocol.parse_task_headers_trusted(text)
    return strict


def _field(headers: dict, key: str) -> "str | None":
    value = (headers.get(key) or "").strip()
    return value or None


def task_fields(task_file: Path, result_dir: Path) -> "dict | None":
    tid = task_file.stem
    if not local_task_protocol.valid_task_id(tid):
        return None
    try:
        text = task_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parsed = attested_headers(text)
    h = parsed.headers
    tier = local_task_protocol.canonical_access_tier(h.get("access_tier"))
    if tier not in local_task_protocol.BROKER_WIRE_TIERS:
        tier = "guest"  # absent or unknown: the most restricted tier
    first_line = parsed.body.split("\n", 1)[0].strip()
    return {
        "task_id": tid,
        "room_id": _field(h, "channel_id") or _field(h, "source_room_id"),
        "event_id": _field(h, "source_message_id"),
        "thread_root": _field(h, "thread_root"),
        "sender": _field(h, "user_id"),
        "sender_name": _field(h, "sender_name"),
        "access_tier": tier,
        "collaborator": tier == "team" and (h.get("collaborator") or "").strip() == "true",
        "snippet": first_line[:SNIPPET_MAX],
        "task_file": str(task_file),
        "result_file": str(result_dir / f"{tid}.txt"),
    }


def list_open_tasks(task_dir: Path, result_dir: Path) -> list:
    """Task files with no result yet, oldest first."""
    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:  # archived between glob and stat
            return 0.0

    items = []
    for path in sorted(task_dir.glob("task-*.txt"), key=lambda p: (mtime(p), p.name)):
        if (result_dir / path.name).exists():
            continue
        item = task_fields(path, result_dir)
        if item is not None:
            items.append(item)
    return items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ag2_sparrow.adapters.hatch.task_fields")
    ap.add_argument("--task-dir", required=True, type=Path)
    ap.add_argument("--result-dir", required=True, type=Path)
    ap.add_argument("--agent", help="exit %d unless this is a valid MXID" % EXIT_BAD_AGENT)
    args = ap.parse_args(argv)
    if args.agent is not None and not valid_mxid(args.agent):
        print(f"not an MXID: {args.agent!r}", file=sys.stderr)
        return EXIT_BAD_AGENT
    if not args.task_dir.is_dir():
        print(f"task dir not found: {args.task_dir}", file=sys.stderr)
        return 2
    json.dump(list_open_tasks(args.task_dir, args.result_dir), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
