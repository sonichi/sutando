"""Reactions on the agent's own messages, recorded as outcome signals and never turned into tasks.

The broker delivers a reaction on a message this agent sent with the message it targets and who
reacted (the reactor's id, and whether that is the agent's owner or a room member), plus the task
whose result that message was, when the message was one:

    {"type": "reaction.added" | "reaction.removed", "event_id", "room_id", "ts",
     "target_event_id", "reactor_id", "reactor_relation": "owner" | "member",
     "key", "task_id"?}

`OutcomeReactionHandler` claims exactly those events (both ids present, a known relation) and
writes one `outcome_signal` to the correlation log: the owner's reaction is `direct` evidence, a
member's `correlated`. The event is consumed and no task is made, so a reaction never causes a
reply. Approval-card reactions are answered by the human-action handler ahead of this one in the
chain; any other event falls through untouched. The label a task closed with is never changed here.
"""
from __future__ import annotations

from pathlib import Path

POSITIVE = frozenset({"✅", "👍", "🙏", "🎉", "❤️", "❤", "💯", "🙌", "+1", "👏", "🔥"})
NEGATIVE = frozenset({"👎", "❌", "😞", "😕", "-1", "🙁", "😡"})
TYPES = {"reaction.added": "reaction", "reaction.removed": "reaction_removed"}


def bucket(key: str) -> str:
    """For display only: the raw key is what is recorded."""
    k = (key or "").strip()
    return "positive" if k in POSITIVE else "negative" if k in NEGATIVE else "neutral"


class OutcomeReactionHandler:
    """EventConsumer handler: claims own-message reactions, records them, consumes them."""

    def __init__(self, log_path, log=print):
        self._path = Path(log_path)
        self._log = log

    @staticmethod
    def _fields(event: dict) -> "dict | None":
        if event.get("type") not in TYPES:
            return None
        target, reactor = event.get("target_event_id"), event.get("reactor_id")
        relation = event.get("reactor_relation")
        if not (isinstance(target, str) and isinstance(reactor, str) and relation in ("owner", "member")):
            return None
        return {"signal": TYPES[event["type"]], "value": str(event.get("key") or "").strip(),
                "reactor_id": reactor, "reactor_relation": relation,
                "evidence": "direct" if relation == "owner" else "correlated", "ref": target,
                "task_id": event.get("task_id") or None, "room_id": event.get("room_id") or None}

    def claims(self, event: dict) -> bool:
        return self._fields(event) is not None

    def offer(self, event: dict) -> list:
        eid = str(event.get("event_id") or "")
        fields = self._fields(event)
        if fields is not None:
            try:
                from . import correlation_log as cl
            except ImportError:  # run from src/, not the package
                import correlation_log as cl
            if fields["task_id"] and not cl.is_task_id(fields["task_id"]):
                fields["task_id"] = None  # recorded untasked rather than lost
            if fields["room_id"] and not cl.PATTERNS["room_id"].match(fields["room_id"]):
                fields["room_id"] = None
            if cl.record("outcome_signal", fields, path=self._path) is None:
                self._log(f"outcome-reactions: {eid} not recorded (refused by the log)")
        return [eid] if eid else []
