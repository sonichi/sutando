#!/usr/bin/env python3
"""room-ops · events_acceptance — end-to-end acceptance runner for the #184
events client (push-observation + durable-cursor replay).

    python3 events_acceptance.py --room <room_id> --cursor-file <path> \
        [--mode react|print|taskify] [--promote-after N --task-dir PATH]

Modes:
  react    (default) on each `message.created` in --room from someone OTHER
           than self (AGENT_MXID), immediately add a 🔭 reaction to that
           message's event id (`content.message_id`) via the existing react
           verb, and print a JSON status line — the observe→act round-trip.
  print    print every delivered envelope as a JSON line (passive observation).
  taskify  accumulate meaningful events and promote every N of them into ONE
           task file in --task-dir (see EventAccumulator).

Every mode streams via stream_with_resume(--cursor-file): kill the runner,
restart it, and delivery resumes from the persisted cursor — the replayed
window is the at-least-once proof.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import events    # noqa: E402
import react as _react  # noqa: E402

# Types that count toward a taskify promotion: the produced message / reaction /
# member events. `room.state_changed` is ambient noise for this purpose.
MEANINGFUL_TYPES = frozenset({
    "message.created", "message.edited", "reaction.added",
    "member.joined", "member.left",
})
ALL_TYPES = MEANINGFUL_TYPES | {"room.state_changed"}

# React-mode reaction key. 🔭 (telescope = ambient observation), NOT 👀: the
# task-processing ack already uses 👀 (the bridge's existing received-ack
# convention, see react.ACK), and the owner found the two visually
# indistinguishable in a room during live acceptance. Keeping the keys distinct
# means a glance tells "the agent OBSERVED this event" apart from "the agent is
# PROCESSING this as a task".
OBSERVE_REACTION = "\U0001F52D"  # 🔭


class EventAccumulator:
    """taskify mode: batch meaningful room events → ONE task file per threshold.

    Why this exists: this is the only doorway from ambient events into the core
    model's attention — the task file lands in tasks/, the existing task
    watcher notifies the core, closing the full loop (room message → SSE →
    consumer → threshold → task → core wakes).

    Skips self events (actor_id == agent_mxid), duplicate event_ids (the cursor
    file already anchors replay; this catches the replayed window), events for
    other rooms, and non-meaningful types. After a promotion the batch resets
    and streaming continues — each batch promotes exactly once.
    """

    def __init__(self, room_id, agent_mxid, threshold, task_dir,
                 meaningful_types=MEANINGFUL_TYPES):
        self.room_id = room_id
        self.agent_mxid = agent_mxid
        self.threshold = max(1, int(threshold))
        self.task_dir = task_dir
        self.meaningful_types = frozenset(meaningful_types)
        self._batch = []        # [{event_id, cursor, type}] pending promotion
        self._seen_ids = set()  # replay/duplicate guard across the whole run

    def offer(self, cursor, envelope):
        """Feed one delivered envelope. Returns the written task-file path when
        this event completes a batch (promotion), else None."""
        if not isinstance(envelope, dict):
            return None
        if envelope.get("room_id") != self.room_id:
            return None
        if envelope.get("actor_id") == self.agent_mxid:
            return None  # self-echo: our own sends/reactions never wake the core
        etype = envelope.get("type")
        if etype not in self.meaningful_types:
            return None
        eid = envelope.get("event_id")
        if not eid or eid in self._seen_ids:
            return None  # at-least-once replay window / duplicate delivery
        self._seen_ids.add(eid)
        self._batch.append({"event_id": eid, "cursor": cursor, "type": etype})
        if len(self._batch) < self.threshold:
            return None
        path = self._promote()
        self._batch = []
        return path

    def _summary(self):
        # "2 messages, 1 reaction" — breakdown by type family, stable order.
        names = {"message": ("message", "messages"),
                 "reaction": ("reaction", "reactions"),
                 "member": ("member event", "member events")}
        counts = {}
        for item in self._batch:
            fam = item["type"].split(".", 1)[0]
            counts[fam] = counts.get(fam, 0) + 1
        parts = []
        for fam in ("message", "reaction", "member"):
            n = counts.get(fam)
            if n:
                one, many = names[fam]
                parts.append(f"{n} {one if n == 1 else many}")
        return ", ".join(parts) + " — review and act if needed"

    def _promote(self):
        cursors = [i["cursor"] for i in self._batch if isinstance(i["cursor"], int)]
        n = len(self._batch)
        provenance = {
            "source_event_ids": [i["event_id"] for i in self._batch],
            "promotion_reason": f"threshold {self.threshold} meaningful events",
            "cursor_range": [cursors[0], cursors[-1]] if cursors else [None, None],
        }
        os.makedirs(self.task_dir, exist_ok=True)
        ts_ms = int(time.time() * 1000)
        while True:
            task_id = f"task-{ts_ms}"
            path = os.path.join(self.task_dir, task_id + ".txt")
            if not os.path.exists(path):
                break
            ts_ms += 1  # same-ms double promotion (bursts/tests) — never overwrite
        # The [taskify] marker + `source: events-promotion` make the origin
        # explicit at a glance: this is an event-promotion, NOT a direct human
        # ask. priority stays `low` so ambient promotions never outrank direct
        # human tasks; `model_hint: efficient` tells the core this task is
        # suitable for a cheaper/faster model.
        body = "\n".join([
            f"id: {task_id}",
            "timestamp: " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            f"task: [taskify] {self._summary()} "
            f"(promoted from {n} subscribed events in {self.room_id})",
            "source: events-promotion",
            f"channel_id: {self.room_id}",
            "priority: low",
            "model_hint: efficient",
            "access_tier: owner",
            "",
            "provenance: " + json.dumps(provenance, ensure_ascii=False),
            "",
        ])
        # tmp + rename: the task watcher globs task-*.txt and must never see a
        # half-written file (same atomicity rule as the cursor file).
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(body)
        os.replace(tmp, path)
        return path


def _print_line(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="events_acceptance",
                                 description="#184 events-client acceptance runner")
    ap.add_argument("--room", required=True)
    ap.add_argument("--cursor-file", required=True)
    ap.add_argument("--mode", choices=["react", "print", "taskify"], default="react")
    ap.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))
    ap.add_argument("--max-events", type=int, default=None,
                    help="stop after N delivered events (scripted acceptance)")
    ap.add_argument("--promote-after", type=int, default=3,
                    help="taskify: meaningful-event batch threshold")
    ap.add_argument("--task-dir", default=None,
                    help="taskify: directory promoted task files land in")
    a = ap.parse_args(argv)

    if a.mode == "taskify" and not a.task_dir:
        ap.error("--task-dir is required for --mode taskify")

    # Make sure delivery actually flows before streaming: subscribe to the
    # types this mode consumes (re-subscribing is the server's idempotency
    # concern per #184; a failure is printed but not fatal — a prior
    # subscription may already cover us).
    sub_types = {
        "react": ["message.created"],
        "print": sorted(ALL_TYPES),
        "taskify": sorted(MEANINGFUL_TYPES),
    }[a.mode]
    sub = events.subscribe(a.room, sub_types, agent_mxid=a.agent_mxid)
    _print_line({"phase": "subscribe", **sub})

    acc = None
    if a.mode == "taskify":
        acc = EventAccumulator(a.room, a.agent_mxid, a.promote_after, a.task_dir)

    def on_event(cur, env):
        if not isinstance(env, dict):
            return
        if a.mode == "print":
            _print_line({"cursor": cur, **env})
            return
        if a.mode == "taskify":
            path = acc.offer(cur, env)
            if path:
                _print_line({"phase": "promoted", "task_file": path,
                             "events": a.promote_after, "cursor": cur})
            return
        # react mode: the observe→act round-trip.
        status = {"phase": "event", "cursor": cur, "type": env.get("type"),
                  "room_id": env.get("room_id"), "actor_id": env.get("actor_id")}
        if (env.get("type") == "message.created" and env.get("room_id") == a.room
                and env.get("actor_id") != a.agent_mxid):
            # `content.message_id` is the reactable event id per #184 — the
            # envelope's own event_id names the delivery, not the message.
            msg_id = (env.get("content") or {}).get("message_id")
            if msg_id:
                res = _react.react(a.room, msg_id, OBSERVE_REACTION, a.agent_mxid)
                status.update(action="react", target=msg_id,
                              ok=res.get("ok"), reason=res.get("reason"))
            else:
                status.update(action="skip", reason="no content.message_id in envelope")
        else:
            status.update(action="skip")
        _print_line(status)

    try:
        cur = events.stream_with_resume(a.cursor_file, on_event, max_events=a.max_events)
        out = {"phase": "done", "ok": True, "cursor": cur}
    except KeyboardInterrupt:
        out = {"phase": "done", "ok": True, "reason": "interrupted"}
    except RuntimeError as e:  # config/permission — not retryable, surface it
        out = {"phase": "done", "ok": False, "reason": str(e)}
    _print_line(out)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
