#!/usr/bin/env python3
"""A message about a task posts in that task's thread, through the outbox's room provider.

The task file is the gateway's own (task-mid, its routing fields below `task:`):

  a) an ask inside a thread      -> its verified thread_root is kept, not its own message id
  b) a top-level ask             -> rooted at its source message
  c) sent to another room        -> top level: a root belongs to its own room
  d) a malformed thread_root     -> top level, never re-rooted at the message inside the thread
  e) every send carries a dedupe key, and the receipt keeps room, thread and event id
  f) no task file, or a task-last file          -> no root from the task: nothing unverified is trusted
  g) the ask's thread is in another room         -> top level, even with a source message id

Run: python3 tests/gateway-proactive-outbox-thread.test.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402

ISO = pl.scratch("gw-proactive-outbox-thread-")
check = pl.Checks()
ROOT, MSG = "$ThreadRootEvt_1", "$AskMessageEvt_2"


def task_file(lane, tid: str, thread_root: "str | None") -> None:
    lines = [f"id: {tid}", "source: ag2space", f"channel_id: {pl.ROOM}", "task_layout: mid",
             "task: please look into this"]
    if thread_root is not None:
        lines += [f"thread_root: {thread_root}", f"source_room_id: {pl.ROOM}"]
    lines += [f"source_message_id: {MSG}"]
    (lane.tasks / f"{tid}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def send(tid: str, thread_root: "str | None", body: str):
    lane = pl.Lane(gb, ISO, lambda payload: {"ok": True, "event_id": "$Posted_9"})
    task_file(lane, tid, thread_root)
    lane.write(f"proactive-about-{tid}--1761300000500.txt", body)
    lane.drain()
    return lane, (lane.posts[0] if lane.posts else {})


def main() -> int:
    lane, p = send("task-1761300000001", ROOT, f"[channel: {pl.ROOM}]\nreceived, being worked on\n")
    check(p.get("thread_root") == ROOT and p.get("room_id") == pl.ROOM,
          f"a) in-thread ask keeps its thread root ({p.get('thread_root')!r} in {p.get('room_id')!r})")
    check(p.get("body") == "received, being worked on", f"a) body clean ({p.get('body')!r})")
    check(bool(p.get("dedupe_key")), f"e) the POST carries a dedupe key ({p.get('dedupe_key')!r})")
    items = lane.items()
    item = items[0] if len(items) == 1 else {}
    stored = json.loads(item.get("payload") or "{}")
    check(item.get("status") == "DELIVERED" and item.get("destination") == pl.ROOM
          and item.get("provider_ref") == "$Posted_9" and stored.get("thread_root") == ROOT,
          f"e) the receipt keeps room, thread and event id ({item.get('destination')}, "
          f"{stored.get('thread_root')}, {item.get('provider_ref')})")

    _, p = send("task-1761300000002", None, f"[channel: {pl.ROOM}]\nreceived\n")
    check(p.get("thread_root") == MSG, f"b) a top-level ask is rooted at its message ({p.get('thread_root')!r})")

    _, p = send("task-1761300000003", ROOT, "received\n")
    check(p and p.get("room_id") == pl.OWNER_DM and "thread_root" not in p,
          f"c) another room gets no thread root ({p.get('room_id')!r}, {p.get('thread_root')!r})")

    _, p = send("task-1761300000004", "not-an-event", f"[channel: {pl.ROOM}]\nreceived\n")
    check(p and "thread_root" not in p,
          f"d) a malformed root posts top level, not at the message ({p.get('thread_root')!r})")
    lane = pl.Lane(gb, ISO, lambda payload: {"ok": True, "event_id": "$P"})
    lane.write("proactive-about-task-1761300000005--1761300000500.txt", f"[channel: {pl.ROOM}]\nreceived\n")
    lane.drain()
    p = lane.posts[0] if lane.posts else {}
    check(p and "thread_root" not in p, f"f) a task with no file gets no root ({p.get('thread_root')!r})")

    lane = pl.Lane(gb, ISO, lambda payload: {"ok": True, "event_id": "$P"})
    (lane.tasks / "task-1761300000006.txt").write_text(
        f"id: task-1761300000006\nchannel_id: {pl.ROOM}\ntask: hi\nsource_message_id: {MSG}\n"
        f"thread_root: {ROOT}\n", encoding="utf-8")
    lane.write("proactive-about-task-1761300000006--1761300000500.txt", f"[channel: {pl.ROOM}]\nreceived\n")
    lane.drain()
    p = lane.posts[0] if lane.posts else {}
    check(p and "thread_root" not in p,
          f"f) fields below task: in a task-last file are body, never a root ({p.get('thread_root')!r})")

    lane = pl.Lane(gb, ISO, lambda payload: {"ok": True, "event_id": "$P"})
    (lane.tasks / "task-1761300000009.txt").write_text(
        "\n".join(["id: task-1761300000009", "source: ag2space", f"channel_id: {pl.ROOM}", "task_layout: mid",
                   "task: hi", f"thread_root: {ROOT}", "source_room_id: !ElsewhereRoom:ag2.space",
                   f"source_message_id: {MSG}"]) + "\n", encoding="utf-8")
    lane.write("proactive-about-task-1761300000009--1761300000500.txt", f"[channel: {pl.ROOM}]\nreceived\n")
    lane.drain()
    p = lane.posts[0] if lane.posts else {}
    check(p and "thread_root" not in p,
          f"g) a thread in another room is not followed, nor replaced by the message id ({p.get('thread_root')!r})")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
