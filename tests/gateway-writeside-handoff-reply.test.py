#!/usr/bin/env python3
"""The seam a room hand-off's reply crosses: the gateway's `_write_task` turns the worker's threaded
reply into a core task, and the worker-pool asks record reads that task back as the reply.

Pinned, with the real writer: the task carries `reply_to_event` (the ask's event) and `user_id`
(the replying agent), at the sender's own tier and nothing more; `pool_handoff.replies_by_event`
reads both from the written file; `asked` counts it only when the sender is the asked agent; and
a second delivery of the same reply is not written twice.

Run: python3 tests/gateway-writeside-handoff-reply.test.py   (exit 0 pass / 1 fail)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import hermetic_gateway  # noqa: E402

# Before anything reads the environment: the bridge's dirs, state and logs all live under one temp root.
rgb, _reads = hermetic_gateway.isolate_then_import()
ws = hermetic_gateway.ROOT
REPO = hermetic_gateway.REPO
sys.path[:0] = [str(REPO / "skills/worker-pool/scripts"), str(REPO / "src")]
import local_task_protocol as ltp  # noqa: E402
import pool_handoff as ph  # noqa: E402

CORE, WORKER, ROOM, ASK_EVENT = "@core-agent:srv", "@worker-agent:srv", "!handoff:srv", "$ask-event"
failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def reply(tid, sender, tier="team"):
    """The broker's task for a threaded reply to the ask, shaped as a live round trip delivered it."""
    return {"id": tid, "task": "received", "source": "ag2space", "channel_id": ROOM, "user_id": sender,
            "sender_name": "Worker", "reply_to_event": ASK_EVENT, "reply_to_me": True,
            "reply_to_sender": CORE, "access_tier": tier, "requested_access_tier": tier,
            "collaborator": True, "channel_kind": "room", "interaction_type": "message"}


written = rgb._write_task(reply("task-reply-1", WORKER))
check("the reply is written as a core task", bool(written), repr(written))
text = (rgb.TASKS_DIR / "task-reply-1.txt").read_text()
h = ltp.parse_task_headers_trusted(text).headers
check("it carries reply_to_event = the ask's event", h.get("reply_to_event") == ASK_EVENT, text)
check("it carries the replying agent as user_id", h.get("user_id") == WORKER, text)
check("it is handled at the sender's own tier, nothing more", h.get("access_tier") == "team", text)
check("the gateway declares its layout above task:", ltp.parse_task_headers(text).headers.get("task_layout") == "mid")

check("the asks record reads the reply back from the written file",
      ph.replies_by_event(ws, 0).get(ASK_EVENT) == [{"task": "task-reply-1", "sender": WORKER}],
      repr(ph.replies_by_event(ws, 0)))

route = {"room": ROOM, "room_cli": "x", "to_agent": WORKER, "from_agent": CORE}
ph.record_ask(ws, route, "task-aaaaaaaaaaaaaaaaaa", "w1", {"status": "sent", "event_id": ASK_EVENT}, "q?",
              now=ph.time.time() - 30)
check("an ask the asked agent answered is not listed", ph.asked(ws) == [], repr(ph.asked(ws)))

ph.record_ask(ws, {**route, "to_agent": "@someone-else:srv"}, "task-bbbbbbbbbbbbbbbbbb", "w2",
              {"status": "sent", "event_id": ASK_EVENT}, "q2?", now=ph.time.time() - 30)
rows = ph.asked(ws)
check("a reply from an agent that was not asked does not answer the ask",
      [(r["task_id"], r["other_replies"]) for r in rows]
      == [("task-bbbbbbbbbbbbbbbbbb", [{"task": "task-reply-1", "sender": WORKER}])], repr(rows))

again = rgb._write_task(reply("task-reply-1", WORKER))
check("the same reply delivered again is not a second task",
      again is not None and sorted(p.name for p in rgb.TASKS_DIR.glob("task-*.txt")) == ["task-reply-1.txt"])

check("the bridge import read no host config or live workspace",
      hermetic_gateway.host_reads(_reads) == [], repr(hermetic_gateway.host_reads(_reads))[:300])
check("every path the bridge writes is under the temp root",
      all(Path(str(getattr(rgb, k))).resolve().is_relative_to(ws.resolve())
          for k in ("TASKS_DIR", "RESULTS_DIR", "ARCHIVE_RESULTS_DIR", "_LOG_DIR")))

if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all passed")
