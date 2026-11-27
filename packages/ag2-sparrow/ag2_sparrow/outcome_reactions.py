"""Agreed-envelope reactions on the agent's own messages become outcome signals, never tasks.

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
chain; events outside this envelope fall through untouched and may become ambient tasks.
The label a task closed with is never changed here.
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
        """The signal for an own-message reaction, or None for any other event. Never raises."""
        if not isinstance(event, dict) or event.get("type") not in TYPES:
            return None
        target, reactor = event.get("target_event_id"), event.get("reactor_id")
        relation = event.get("reactor_relation")
        if not (isinstance(target, str) and isinstance(reactor, str) and relation in ("owner", "member")):
            return None
        key, task, room = event.get("key"), event.get("task_id"), event.get("room_id")
        return {"signal": TYPES[event["type"]], "value": key.strip() if isinstance(key, str) else "",
                "reactor_id": reactor, "reactor_relation": relation,
                "evidence": "direct" if relation == "owner" else "correlated", "ref": target,
                "source_event_id": str(event.get("event_id") or ""),
                "task_id": task if isinstance(task, str) and task else None,
                "room_id": room if isinstance(room, str) and room else None}

    def claims(self, event: dict) -> bool:
        return self._fields(event) is not None

    def offer(self, event: dict) -> list:
        """The ids to settle: a recorded, already-recorded or unrecordable reaction is settled; one
        whose write failed is not, so the next drain retries it (and nothing else ever takes it)."""
        eid = str(event.get("event_id") or "") if isinstance(event, dict) else ""
        try:
            fields = self._fields(event)
            if fields is None:
                return [eid] if eid else []
            from . import correlation_log as cl
            if fields["task_id"] and not cl.is_task_id(fields["task_id"]):
                fields["task_id"] = None  # recorded untasked rather than lost
            if fields["room_id"] and not cl.PATTERNS["room_id"].match(fields["room_id"]):
                fields["room_id"] = None
            try:
                cl.validate("outcome_signal", fields)
            except ValueError as e:
                self._log(f"outcome-reactions: {eid} not recorded ({e})")
                return [eid] if eid else []          # terminal: retrying cannot make it valid
            if any(r.get("source_event_id") == fields["source_event_id"]
                   for r in cl.read("outcome_signal", path=self._path)):
                return [eid]                          # a replay of one already recorded
            if cl.record("outcome_signal", fields, path=self._path) is None:
                self._log(f"outcome-reactions: {eid} write failed; kept for the next drain")
                return []
            return [eid]
        except Exception as e:  # noqa: BLE001 - one event must never stall the drain for the rest
            self._log(f"outcome-reactions: {eid} left for the next drain ({type(e).__name__})")
            return []
