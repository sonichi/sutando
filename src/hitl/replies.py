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

from .manager import HitlManager
from .schema import Action, ActionReply, HumanRequirement, MalformedActionError, StaleRequirementError

REPLY_FIELD = "space.ag2.hitl.action"
EVENT_TYPE = "message.created"
# The one action kind the runtime driver executes; every other kind is
# finished by the requirement's own producer (hook, client, probe).
TUI_ACTION_KIND = "tui_select"


def actions_dir(workspace: Path) -> Path:
    return Path(workspace) / "state" / "hitl" / "actions"


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

    def claims(self, event: Dict[str, Any]) -> bool:
        content = event.get("content") or {}
        return event.get("type") == EVENT_TYPE and isinstance(content.get(REPLY_FIELD), dict)

    def offer(self, event: Dict[str, Any]) -> List[str]:
        eid = str(event.get("event_id") or "")
        claimed = [eid] if eid else []
        if not self.claims(event):
            return claimed
        actor = str(event.get("actor_id") or "")
        # AUTHORIZATION — the whole point: only the owner resolves.
        if not self._owner or actor != self._owner:
            self._log(f"hitl: action reply from non-owner {actor or '?'} ignored")
            return claimed
        reply = parse_reply(event)
        if reply is None:
            self._log("hitl: malformed action reply ignored")
            return claimed
        try:
            action = self._manager.apply_action(reply)
        except (StaleRequirementError, MalformedActionError) as e:
            self._log(f"hitl: reply for {reply.hitl_id} rejected — {e}")
            return claimed
        note = ""
        if action.kind == TUI_ACTION_KIND and self._workspace is not None:
            req = self._manager.get(reply.hitl_id)
            if req is not None:
                note = f"; driver action {write_driver_action(self._workspace, req, action).name}"
        self._log(f"hitl: {reply.hitl_id} -> {action.id} by {actor} (in_progress{note})")
        return claimed
