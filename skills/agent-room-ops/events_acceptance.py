#!/usr/bin/env python3
"""room-ops · events_acceptance — end-to-end acceptance runner for the #184
events client (push-observation + durable-cursor replay).

    python3 events_acceptance.py --room <room_id> --cursor-file <path> \
        [--mode react|print|taskify] [--promote-after N --task-dir PATH]

Modes:
  react    (default) on each `message.created` in --room from someone OTHER
           than self (AGENT_MXID), immediately add a 👀 reaction to that
           message's event id (`content.message_id`) via the existing react
           verb, and print a JSON status line — the observe→act round-trip.
  print    print every delivered envelope as a JSON line (passive observation).
  taskify  TEST-ONLY: accumulate meaningful events and promote every N of
           them into ONE task file in --task-dir (see EventAccumulator).
           Exercises the promotion contract for acceptance evidence; the
           PRODUCTION taskify consumer is ag2-sparrow's (see POSITIONING).

Every mode streams via stream_with_resume(--cursor-file): kill the runner,
restart it, and delivery resumes from the persisted cursor — the replayed
window is the at-least-once proof.

POSITIONING (decided with the owner, 2026-07-24): this runner is an
ACCEPTANCE/DEBUG HARNESS, not a production consumer. Production event
streaming + taskify promotion belong to the ag2-sparrow long-running client
(EventChannel/EventInbox/TaskifyHandler — durable SQLite inbox, crash-safe
exactly-once promotion, human-action decision routing), enabled via
SPARROW_EVENTS in the gateway bridge. Never run this runner's `taskify` mode
against a room the sparrow consumer is also draining: two consumers with
independent cursors will promote the same events twice. What THIS module and
events.py remain canonical for: subscription management (subscribe/
unsubscribe/rooms), ad-hoc pull/print debugging, and scripted acceptance
evidence — the control-plane half, bridge-independent by design.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import events    # noqa: E402
import react as _react  # noqa: E402

# ONE promotion implementation, two uses (owner directive 2026-07-24): the
# taskify contract — meaningful-type filter, self-echo skip, per-room batches,
# deterministic idempotent ids, ambient tier, fsync durability, in-band DiD
# block — lives ONLY in the sparrow package. This harness imports it rather
# than carrying a parallel copy (the previous EventAccumulator duplicated it,
# and contract fixes had to land twice — e.g. the fsync order fix).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "packages", "ag2-sparrow"))
from ag2_sparrow.event_consumer import (  # noqa: E402
    MEANINGFUL_TYPES, TaskifyHandler,
)

ALL_TYPES = MEANINGFUL_TYPES | {"room.state_changed"}

# React-mode reaction key. 👀 = "the agent OBSERVED this event" — the owner's
# finalized convention (👀 observed / 🫡 task-acknowledged). This was held as 🔭
# earlier ONLY to dodge a collision: the task-intake ack was also 👀 back then.
# That collision is gone — the broker now emits 🫡 for task intake server-side
# (ag2space-backend#188, deployed), so 👀 is free to mean observation. A glance
# still separates the two states, now via the owner's chosen glyphs.
OBSERVE_REACTION = "\U0001F440"  # 👀

class EventAccumulator:
    """taskify mode: batch meaningful room events → ONE task file per threshold.

    THIN ADAPTER over the sparrow package's TaskifyHandler — the single
    promotion implementation (dedup, deterministic idempotent ids, ambient
    tier, fsync durability, in-band DiD block, per-room partitioning). This
    class only adds what the acceptance harness needs on top:
      - a single-room filter (the harness observes ONE --room; the production
        consumer drains every subscribed room),
      - the (cursor, envelope) call shape used by stream_with_resume, folding
        the stream cursor into the envelope for provenance,
      - a returns-path-on-promotion contract for the runner's status lines.
    """

    def __init__(self, room_id, agent_mxid, threshold, task_dir):
        self.room_id = room_id
        self._handler = TaskifyHandler(task_dir, agent_mxid,
                                       threshold=threshold, log=lambda *_: None)

    def has_pending(self):
        """True while events sit accumulated-but-not-yet-promoted. The cursor
        gate (stream_with_resume should_persist) must HOLD while this is True:
        the pending events live only in memory, so persisting a cursor past them
        would drop them on the next restart (#2292 P1-2). Empty batch = the last
        promotion (or a skip with nothing pending) durably covered everything up
        to here, so the cursor is safe to advance."""
        return self._handler.has_pending()

    def offer(self, cursor, envelope):
        """Feed one delivered envelope. Returns the written task-file path when
        this event completes a batch (promotion), else None."""
        if not isinstance(envelope, dict):
            return None
        if envelope.get("room_id") != self.room_id:
            return None  # harness contract: observe exactly --room
        ev = dict(envelope)
        if "cursor" not in ev and isinstance(cursor, int):
            ev["cursor"] = cursor  # provenance cursor_range comes from here
        # A promotion (including the idempotent replay of an already-promoted
        # batch, which resolves to the SAME deterministic path on a fresh
        # handler) moves last_path; anything else leaves it unchanged.
        before = self._handler.last_path
        self._handler.offer(ev)
        after = self._handler.last_path
        return after if after != before else None


def _print_line(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="events_acceptance",
                                 description="#184 events-client acceptance runner")
    ap.add_argument("--room", required=True)
    ap.add_argument("--cursor-file", required=True)
    ap.add_argument("--mode", choices=["react", "print", "taskify"], default="react",
                    help="react|print|taskify — taskify is TEST-ONLY (acceptance "
                         "harness); production promotion is the sparrow consumer")
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

    # Acting modes (react emits reactions, taskify writes tasks) key self-echo
    # suppression off the agent's own mxid: without it, actor_id == self can
    # never be detected, so the runner reacts to / promotes its OWN events —
    # a feedback loop (#2292 P2-2). print mode is passive, so it stays optional.
    if a.mode in ("react", "taskify") and not a.agent_mxid:
        ap.error(f"--agent (or AGENT_MXID env) is required for --mode {a.mode}: "
                 "self-echo suppression needs the agent's own mxid")

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

    # taskify batches in memory, so the durable cursor must NOT advance past
    # events still pending promotion (#2292 P1-2). Gate persistence on an empty
    # batch. Other modes are stateless per event → persist every time (None).
    should_persist = (lambda cur, env: not acc.has_pending()) if acc else None
    try:
        cur = events.stream_with_resume(a.cursor_file, on_event,
                                        max_events=a.max_events,
                                        should_persist=should_persist)
        out = {"phase": "done", "ok": True, "cursor": cur}
    except KeyboardInterrupt:
        out = {"phase": "done", "ok": True, "reason": "interrupted"}
    except RuntimeError as e:  # config/permission — not retryable, surface it
        out = {"phase": "done", "ok": False, "reason": str(e)}
    _print_line(out)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
