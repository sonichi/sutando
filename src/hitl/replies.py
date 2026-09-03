"""Inbound half of the client action wire.

An owner's card click arrives on the bridge's event channel as a room message
whose content carries `space.ag2.hitl.action` = {hitl_id, expected_revision,
action_id, guard}. This handler runs it through the Manager's two-layer stale
gate and, when the chosen action is a TUI keystroke, hands the request to the
runtime driver through <workspace>/state/hitl/actions/<hitl_id>.json — the
desktop watchdog re-validates the guard against the live screen before it
types anything. EventConsumer handler contract (claims / offer / last_path),
chain-compatible with the bridge's HandlerChain: it claims only what it
recognises, only the configured owner resolves, and a rejected reply changes
nothing. No owner configured => inert, fail-closed.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from workspace_default import resolve_workspace

from .manager import HitlManager
from .schema import Action, ActionReply, HumanRequirement, MalformedActionError, StaleRequirementError

REPLY_FIELD = "space.ag2.hitl.action"
EVENT_TYPE = "message.created"
# The one action kind the runtime driver executes; every other kind is
# finished by the requirement's own producer (hook, client, probe).
TUI_ACTION_KIND = "tui_select"


def actions_dir(workspace: Optional[Path] = None) -> Path:
    ws = Path(workspace) if workspace is not None else resolve_workspace()
    return ws / "state" / "hitl" / "actions"


def parse_reply(event: Dict[str, Any]) -> Optional[ActionReply]:
    content = event.get("content") or {}
    payload = content.get(REPLY_FIELD)
    if not isinstance(payload, dict):
        return None
    try:
        return ActionReply.from_wire(payload)
    except MalformedActionError:
        return None


def write_driver_action(workspace: Path, req: HumanRequirement, action: Action) -> Path:
    """Atomically drop the driver's action file; the driver replaces it with a receipt."""
    d = actions_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    device = req.device or {}
    body = {
        "session": str(device.get("id") or ""),
        "socket": str(device.get("socket") or ""),
        "guard": req.guard,
        "action_id": action.id,
        "hitl_id": req.id,
    }
    fd, tmp = tempfile.mkstemp(prefix=".action-", suffix=".tmp", dir=d)
    with os.fdopen(fd, "w") as f:
        json.dump(body, f)
        f.flush()
        os.fsync(f.fileno())
    final = d / f"{req.id}.json"
    os.replace(tmp, final)
    return final


class HitlReplyHandler:
    """Turns the owner's card clicks into Manager actions. Claims ONLY events
    carrying the reply field; unrelated room traffic flows on untouched."""

    def __init__(self, manager: HitlManager, owner_mxid: Optional[str],
                 workspace: Optional[Path] = None, log=print):
        self._manager = manager
        self._owner = owner_mxid
        self._workspace = Path(workspace) if workspace is not None else None
        self._log = log
        self.last_path = None  # handler-contract compat (never promotes tasks)
        # What the most recent offer() did: applied | rejected | ignored (+ reason).
        self.last_outcome = None
        self.last_reason = ""
        # Form task_to_event() last recognised: "click" (hitl_action) or
        # "fallback" (typed label) — a non-owner's fallback is a MESSAGE.
        self.last_branch = None

    def claims(self, event: Dict[str, Any]) -> bool:
        content = event.get("content") or {}
        return event.get("type") == EVENT_TYPE and isinstance(content.get(REPLY_FIELD), dict)

    def offer(self, event: Dict[str, Any]) -> List[str]:
        eid = str(event.get("event_id") or "")
        claimed = [eid] if eid else []
        if not self.claims(event):
            return claimed
        actor = str(event.get("actor_id") or "")
        self.last_outcome = "ignored"
        self.last_reason = ""
        # AUTHORIZATION — the whole point: only the owner resolves.
        if not self._owner or actor != self._owner:
            self._log(f"hitl: action reply from non-owner {actor or '?'} ignored")
            return claimed
        reply = parse_reply(event)
        if reply is None:
            self._log("hitl: malformed action reply ignored")
            self.last_outcome = "rejected"
            self.last_reason = "malformed action payload"
            return claimed
        try:
            action = self._manager.apply_action(reply)
        except (StaleRequirementError, MalformedActionError) as e:
            self._log(f"hitl: reply for {reply.hitl_id} rejected — {e}")
            self.last_outcome = "rejected"
            self.last_reason = str(e)
            return claimed
        self.last_outcome = "applied"
        note = ""
        if action.kind == TUI_ACTION_KIND and self._workspace is not None:
            req = self._manager.get(reply.hitl_id)
            if req is not None:
                note = f"; driver action {write_driver_action(self._workspace, req, action).name}"
        self._log(f"hitl: {reply.hitl_id} -> {action.id} by {actor} (in_progress{note})")
        return claimed

    # -- task-relay path ---------------------------------------------------------

    def offer_task(self, task: Dict[str, Any]) -> Optional[str]:
        """A click delivered as a relay TASK, not an event (an owner DM travels
        only the task relay). Returns "applied" / "rejected" / "ignored" for a
        click (consumed either way), or None for an ordinary message that stays
        on the task path. A rejection is the owner's to hear about."""
        event = self.task_to_event(task)
        if event is None:
            return None
        self.offer(event)
        return self.last_outcome or "ignored"

    def task_to_event(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Exact form: `hitl_action` (the relay forwards the message's action
        field). Fallback: a reply to a card's own event whose text is one of
        that card's action labels — the projection ledger names the card event,
        so no field is needed. Anything else is not a click."""
        base = {
            "type": EVENT_TYPE,
            "event_id": str(task.get("source_message_id") or task.get("id") or ""),
            "room_id": task.get("channel_id"),
            "actor_id": str(task.get("user_id") or ""),
        }
        self.last_branch = None
        payload = task.get("hitl_action")
        if isinstance(payload, dict):
            self.last_branch = "click"
            return {**base, "content": {REPLY_FIELD: dict(payload)}}
        target = str(task.get("reply_to_event") or "")
        if not target:
            return None
        req = self.requirement_for_event(target)
        if req is None:
            return None
        action, note = match_action(req, _reply_text(str(task.get("task") or "")))
        if action is None:
            return None  # a reply to the card that is not a click stays a message
        self.last_branch = "fallback"
        payload = {"hitl_id": req.id, "expected_revision": req.revision,
                   "action_id": action.id, "guard": req.guard}
        if note:
            payload["answer"] = note
        return {**base, "content": {REPLY_FIELD: payload}}

    def requirement_for_event(self, event_id: str) -> Optional[HumanRequirement]:
        """The active requirement whose card is `event_id` (CREATE projection;
        status EDITs keep the same event id, so a reply to any revision maps)."""
        for req in self._manager.active():
            if self._manager.projection_target(req.id) == event_id:
                return req
        return None


# The separator is required: without it a label prefix-matches an unrelated
# sentence that merely starts with it, silently turning prose into a decision.
NOTE_SEPARATORS = ("\u2014", "\u2013", "-", ":")


def match_action(req: HumanRequirement, text: str):
    """(Action, note) for a reply that is a click, else (None, None).

    Exact label or id is a bare click. `<label> <sep> <note>` is the same click
    carrying a free-text qualification, which travels on as ActionReply.answer.
    """
    t = (text or "").strip()
    low = t.lower()
    for action in req.actions:
        for cand in (action.label or "", action.id or ""):
            c = cand.strip()
            if not c:
                continue
            if low == c.lower():
                return action, None
            if low.startswith(c.lower()):
                rest = t[len(c):].lstrip()
                if rest[:1] in NOTE_SEPARATORS:
                    # A separator with nothing after it is still the click; the
                    # human just left the note empty.
                    return action, (rest[1:].strip() or None)
    return None, None


REPLY_CONTEXT_END = "[End AG2 Space reply context]"


def _reply_text(task_text: str) -> str:
    """The message text after the relay's quoted reply context. A wrong cut can
    only fail to match a label (the task then stays a message), never pick one."""
    head, sep, tail = task_text.rpartition(REPLY_CONTEXT_END)
    return tail if sep else task_text
