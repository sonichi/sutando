"""Whether a claude-local session's AG2 Space tools may be called right now.

The owner and attested collaborators share the one session, and Claude Code keeps a session's MCP servers for
its life, so the tools cannot be granted per sender. `mcp_name_shim`'s turn gate asks here before every
`tools/call`. Shared (not the adapter's) because the shim is. Stdlib only.
"""
from __future__ import annotations

import os
import sys

from . import claude_local_records as records, task_io, task_lifecycle as lifecycle

ROOM_TOOLS_OWNER_ONLY = ("the AG2 Space tools are the owner's: a collaborator's task is in flight in this session, so "
                         "no room tool is called until it is answered")
ROOM_TOOLS_NO_SESSION = "no live session holds this root, so no room tool is called"
ROOM_TOOLS_UNREAD = "a claimed task could not be read, so no room tool is called"


def room_tools_refusal(root) -> str:
    """Why a room-tool call is refused right now, '' when it may go on: while any collaborator's task of the live
    generation is claimed and not yet published or settled, no call is forwarded. A claim or task that cannot be
    read refuses; a task file already gone is an answered task. Read under claims.lock, changing nothing."""
    consumer = task_io.holder_consumer(root)
    if not consumer or not consumer.get("pid") or consumer.get("live") is not True:
        return ROOM_TOOLS_NO_SESSION
    live = (consumer["pid"], consumer.get("start"))
    lc = lifecycle.Lifecycle(root, identity=task_io.process_identity(), lineage=task_io.verify_lineage,
                             log=lambda *a: print("room-tools gate:", *a, file=sys.stderr))
    with records.flocked(records.claims_lock_path(root)):
        for task_id in lc.claims.ids():
            try:
                rec = lc.claims.load(task_id)
            except lifecycle.ClaimUnreadable:
                return ROOM_TOOLS_UNREAD
            gen = (rec or {}).get("generation") or {}
            if not rec or rec.get("claim_mode") != lifecycle.LOCAL or (gen.get("pid"), gen.get("start")) != live \
                    or rec.get("phase") in (lifecycle.PUBLISHED, lifecycle.SETTLED):
                continue
            try:
                headers, _, _ = task_io.parse_task(os.path.join(root, "tasks", task_id + ".txt"))
            except FileNotFoundError:
                continue
            except OSError:
                return ROOM_TOOLS_UNREAD
            if task_io.turn_policy(headers, True, True).kind == task_io.COLLABORATOR:
                return ROOM_TOOLS_OWNER_ONLY
    return ""
