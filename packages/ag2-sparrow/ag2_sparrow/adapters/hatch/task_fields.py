"""Print the routing fields of every open task file in a task dir as one JSON array.

The Hatch hook script calls this so its wake payload carries structured fields read
with Sparrow's own header parser, never prose scraped by the shell. A hook that tells its
worker what `held: true` means runs this with HELD_AWARE_ENV=1; for any other hook (one
registered before the held branch, which a package upgrade does not rewrite) a held task is
left out of the list, so that hook never wakes a worker for it."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from pathlib import Path

from ... import gateway_instance, local_task_protocol, task_hold
from ...task_io import attested_headers

SNIPPET_MAX = 500
REPLY_CONTEXT_MAX = 2000
# Matrix user id: @localpart:server_name (DNS name, IPv4 or [IPv6], optional :port).
# fullmatch only: `$` would also match before a trailing newline.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
MXID_RE = re.compile(r"@[A-Za-z0-9._=+/-]+:(?:\[(?P<ipv6>[0-9A-Fa-f:.]+)\]|"
                     rf"{_LABEL}(?:\.{_LABEL})*)(?::(?P<port>[0-9]{{1,5}}))?")
EXIT_BAD_AGENT = 3
HELD_AWARE_ENV = "HATCH_HOOK_HELD_AWARE"
CHANNEL_KINDS = ("dm", "room")
_SENDER_PREFIX = re.compile(r"\[AG2Space\s+[^\]]+\]\s*")
# The gateway folds a message onto one line, so a reply fallback ("> <@sender> quoted"
# lines, a blank line, the reply) arrives as "> <@sender> quoted  reply".
_REPLY_FALLBACK = re.compile(r">\s?<[^>\s]+>.*?\s{2,}(?=\S)")


def valid_mxid(value: str) -> bool:
    m = MXID_RE.fullmatch(value or "")
    if m is None:
        return False
    if m["ipv6"] is not None:
        try:
            ipaddress.IPv6Address(m["ipv6"])
        except ValueError:
            return False
    return m["port"] is None or 0 < int(m["port"]) <= 65535


def _split(text: str) -> "tuple[str, str]":
    """(sender prefix, the message after it without a Matrix reply fallback, which is
    the quoted message replied to, not the reply)."""
    prefix = _SENDER_PREFIX.match(text)
    head = prefix.group(0) if prefix else ""
    rest = text[len(head):]
    fallback = _REPLY_FALLBACK.match(rest)
    return head, rest[fallback.end():] if fallback else rest


def snippet(body: str) -> str:
    """The message's first line, sender prefix kept, without a reply fallback."""
    head, rest = _split(body.split("\n", 1)[0].strip())
    return (head + rest)[:SNIPPET_MAX]


def _field(headers: dict, key: str) -> "str | None":
    value = (headers.get(key) or "").strip()
    return value or None


def _reply_and_message(parsed: "local_task_protocol.TaskHeaders") -> "tuple[dict | None, str]":
    # Only an attested reply gets a reply_context; a block a sender typed stays their text.
    if _field(parsed.headers, "reply_to_event"):
        return local_task_protocol.split_reply_context(parsed.body)
    return None, parsed.body


def item_id(tid: str, instance: str = "") -> "str | None":
    """The id `instance`'s bridge delivers this task's result under, or None when the
    task is not that instance's or not a valid task id."""
    if not gateway_instance.owns(tid, instance):
        return None
    broker = gateway_instance.broker_tid(tid, instance)
    return broker if local_task_protocol.valid_task_id(broker) else None


def task_fields(task_file: Path, result_dir: Path, instance: "str | None" = "") -> "dict | None":
    """`instance` None reads every instance's task."""
    tid = task_file.stem
    if instance is not None and item_id(tid, instance) is None:
        return None
    try:
        text = task_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parsed = attested_headers(text)
    h = parsed.headers
    reply, message = _reply_and_message(parsed)
    kind = _field(h, "channel_kind")
    tier = local_task_protocol.canonical_access_tier(h.get("access_tier"))
    if tier not in local_task_protocol.BROKER_WIRE_TIERS:
        tier = "guest"  # absent or unknown: the most restricted tier
    item = {
        "task_id": tid,
        "room_id": _field(h, "channel_id") or _field(h, "source_room_id"),
        "room_name": (_field(h, "room_name") or "")[:SNIPPET_MAX] or None,
        "channel_kind": kind if kind in CHANNEL_KINDS else None,
        "event_id": _field(h, "source_message_id"),
        "thread_root": _field(h, "thread_root"),
        "sender": _field(h, "user_id"),
        "sender_name": _field(h, "sender_name"),
        "access_tier": tier,
        "collaborator": tier == "team" and (h.get("collaborator") or "").strip() == "true",
        "snippet": snippet(message),
        "task_file": str(task_file),
        "result_file": str(result_dir / f"{tid}.txt"),
    }
    if reply is not None:
        label = reply["label"]
        item["reply_context"] = {"sender": reply["sender"][:SNIPPET_MAX],
                                 "body": reply["body"][:REPLY_CONTEXT_MAX],
                                 "label": label[:REPLY_CONTEXT_MAX] if label else None}
    # A parked item is flagged on every wake, so a fresh worker session never answers it.
    if task_hold.read(result_dir, tid) is not None:
        item["held"] = True
    return item


def list_open_tasks(task_dir: Path, result_dir: Path, instance: "str | None" = "") -> list:
    """Task files with no result yet, oldest first; `instance` None lists every instance's."""
    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:  # archived between glob and stat
            return 0.0

    items = []
    for path in sorted(task_dir.glob("task-*.txt"), key=lambda p: (mtime(p), p.name)):
        if (result_dir / path.name).exists():
            continue
        item = task_fields(path, result_dir, instance)
        if item is not None:
            items.append(item)
    return items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ag2_sparrow.adapters.hatch.task_fields")
    ap.add_argument("--task-dir", required=True, type=Path)
    ap.add_argument("--result-dir", required=True, type=Path)
    ap.add_argument("--agent", help="exit %d unless this is a valid MXID" % EXIT_BAD_AGENT)
    ap.add_argument("--gateway-instance", default="", help="list only this instance's tasks")
    args = ap.parse_args(argv)
    if args.agent is not None and not valid_mxid(args.agent):
        print(f"not an MXID: {args.agent!r}", file=sys.stderr)
        return EXIT_BAD_AGENT
    if not args.task_dir.is_dir():
        print(f"task dir not found: {args.task_dir}", file=sys.stderr)
        return 2
    items = list_open_tasks(args.task_dir, args.result_dir, args.gateway_instance)
    if os.environ.get(HELD_AWARE_ENV) != "1":
        items = [item for item in items if not item.get("held")]
    json.dump(items, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
