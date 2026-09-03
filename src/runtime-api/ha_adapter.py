"""runtime-api ↔ human-action adapter, over the HITL Requirement store.

A runtime request that needs a human (approval, elicitation, human_action)
becomes ONE HumanRequirement in the HITL store — the same record the hook
driver, the card projector and the bridge's reply handler already share — and
the owner's card click resolves it through the Manager's revision + guard gate.
The v0.5 `ha_*.json` action files and their separate card poster are gone: the
Requirement is the only object, the card is its projection.

Mapping (kind / actions):
  approval.request     → permission       Approve / Deny
  elicitation.request  → choice           one action per option (opt1..optN), or
                                          a free-text action when no options
  human_action.request → external_action  Done / Decline

`poll_resolution` keeps the dispatcher's answer shape — {"1": [idx, ...]} for
selections, {"1": "text"} for free text — so `_settle` and `first_answer` are
unchanged; the adapter maps the chosen action id back to the option index.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Sibling-import bootstrap (NOT workspace resolution): put src/ on sys.path so
# the in-repo `hitl` package imports the same way the hook driver imports it.
_HERE = Path(__file__).resolve().parent  # src/runtime-api
_SRC = _HERE.parent                      # src
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.schema import (  # noqa: E402
    Action,
    ActionReply,
    HumanRequirement,
    MalformedActionError,
    StaleRequirementError,
)

RUNTIME = "runtime-api"
DEFAULT_TTL_S = 24 * 3600
FREE_TEXT_ACTION = "answer"


def _now() -> float:
    return time.time()


def ha_action_id(request_id: str) -> str:
    """Deterministic requirement id for a runtime request, so `recover()` can
    relink a pending request after a daemon restart without any map. The
    requestId's uuid tail is hex; the type prefix ("approval-") never leaks in."""
    return "hitl_" + request_id.split("-", 1)[-1][:24]


class HumanActionAdapter:
    def __init__(self, actions_dir: str):
        # The directory is the HITL store root for this daemon (one store per
        # workspace in production: default_store(workspace)).
        Path(actions_dir).mkdir(parents=True, exist_ok=True)
        self.manager = HitlManager(HitlStore(Path(actions_dir)))

    # ── outbound: runtime request → requirement ────────────────────────────
    def open_approval(self, request: dict) -> str:
        p = request["params"]
        action_line = p.get("action", "?")
        resource = p.get("resource")
        inp = p.get("input")
        reason = p.get("reason")
        # The card must show the FULL effect being approved — including the
        # governed input (for message.send, the input IS the message body).
        message = (f"Approve: {action_line}"
                   + (f"\nResource: {_json(resource)}" if resource else "")
                   + (f"\nInput: {_json(inp)}" if inp else "")
                   + (f"\nReason: {reason}" if reason else ""))
        return self._create(request, kind="permission", message=message,
                            options=["Approve", "Deny"],
                            actions=[Action(id="approve", kind="allow_once", label="Approve"),
                                     Action(id="deny", kind="reject_once", label="Deny")])

    def open_elicitation(self, request: dict) -> str:
        p = request["params"]
        etype = p.get("type", "single_select")
        options = [str(o) for o in (p.get("options") or [])]
        if etype == "confirmation" and not options:
            options = ["Yes", "No"]
        question = str(p.get("question", "?"))
        if options:
            actions = [Action(id=f"opt{i}", kind="select", label=label)
                       for i, label in enumerate(options, 1)]
        else:
            actions = [Action(id=FREE_TEXT_ACTION, kind="free_text", label="Answer")]
        return self._create(request, kind="choice", message=question, options=options,
                            actions=actions, extra={"type": etype,
                                                    "multi_select": etype == "multi_select"})

    def open_human_action(self, request: dict) -> str:
        """A real-world act the human must perform (sign, pay, plug in, ...).
        Done/Decline map to the request's completed/declined terminal states."""
        p = request["params"]
        message = (f"Action needed: {p.get('action', '?')}"
                   + (f"\nInstructions: {p['instructions']}" if p.get("instructions") else "")
                   + (f"\nDeadline: {p['deadline']}" if p.get("deadline") else ""))
        return self._create(request, kind="external_action", message=message,
                            options=["Done", "Decline"],
                            actions=[Action(id="done", kind="complete", label="Done"),
                                     Action(id="decline", kind="reject_once", label="Decline")])

    def _create(self, request: dict, *, kind: str, message: str, options: List[str],
                actions: List[Action], extra: Optional[Dict[str, Any]] = None) -> str:
        rid = request["requestId"]
        req = HumanRequirement(
            id=ha_action_id(rid),
            kind=kind,
            runtime=RUNTIME,
            message=message,
            title=f"runtime request {rid}",
            # The guard is the request identity: a runtime request never repaints.
            guard=f"runtime:{rid}",
            subject={"runtime_request_id": rid, "options": options, **(extra or {})},
            actions=actions,
            expires_at=request.get("expiresAt") or (_now() + DEFAULT_TTL_S),
        )
        self.manager.create(req)
        return req.id

    def close(self, action_id: str, resolved_by: str, note: Optional[str] = None) -> None:
        """Settle a still-open requirement out-of-band (API completion path) so
        the card does not dangle: the requester already settled it."""
        req = self.manager.get(action_id)
        if req is None or req.terminal:
            return
        with self.manager.store.locked():
            req = self.manager.get(action_id)
            if req is None or req.terminal:
                return
            req.decided_by = resolved_by
            if note:
                req.answer = {"note": note}
            self.manager.store.save(req)
        self.manager.resolve(action_id)

    # ── inbound: resolved requirement → runtime terminal state ─────────────
    def poll_resolution(self, action_id: str):
        """Return (status, answers, resolved_by) once the requirement reaches a
        terminal state, else None while pending. status ∈ resolved|expired.
        `answers` keeps the DecisionHandler shape the dispatcher settles on."""
        req = self.manager.get(action_id)
        if req is None:
            return None
        if not req.terminal:
            if req.expires_at and _now() > req.expires_at:
                self.manager.expire(action_id)
                return ("expired", None, None)
            if req.chosen_action is None:
                return None
            # in_progress with a chosen action: the click landed, finish it.
            self.manager.resolve(action_id)
            req = self.manager.get(action_id)
        if req.status == "resolved":
            return ("resolved", self._answers(req), req.decided_by or "owner")
        return ("expired", None, req.decided_by)

    def resolve(self, action_id: str, answers: Dict[str, Any], resolved_by: str) -> None:
        """Apply an answer in the dispatcher's shape ({"1": [idx, ...]} or
        {"1": "text"}) as the owner's action — the test and CLI entry point."""
        req = self.manager.get(action_id)
        if req is None:
            raise MalformedActionError(f"no requirement {action_id}")
        a = answers.get("1")
        if a is None and answers:
            a = next(iter(answers.values()))
        if isinstance(a, list):
            idxs = [int(i) for i in a if str(i).isdigit()]
            valid = [i for i in idxs if 1 <= i <= len(req.subject.get("options") or [])]
            if not valid:
                raise MalformedActionError(f"{action_id}: no valid option index in {a!r}")
            action_id_chosen = f"opt{valid[0]}" if req.kind == "choice" else self._label_action(req, valid[0])
            payload = [req.subject["options"][i - 1] for i in valid] if len(valid) > 1 else None
        else:
            action_id_chosen = FREE_TEXT_ACTION if any(x.id == FREE_TEXT_ACTION for x in req.actions) \
                else self._label_action(req, None, text=str(a))
            payload = str(a)
        try:
            self.manager.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision,
                                                  action_id=action_id_chosen, guard=req.guard,
                                                  answer=payload))
        except StaleRequirementError:
            return  # a late or duplicate answer changes nothing
        with self.manager.store.locked():
            cur = self.manager.get(action_id)
            if cur is not None and not cur.terminal:
                cur.decided_by = resolved_by
                self.manager.store.save(cur)
        self.manager.resolve(action_id)

    @staticmethod
    def _label_action(req: HumanRequirement, idx: Optional[int], text: Optional[str] = None) -> str:
        """Approval / human_action cards: the option index or a typed label maps
        onto the fixed action ids (approve/deny, done/decline)."""
        opts = req.subject.get("options") or []
        label = (opts[idx - 1] if idx is not None and 1 <= idx <= len(opts) else (text or "")).strip().lower()
        for a in req.actions:
            if a.label.lower() == label:
                return a.id
        raise MalformedActionError(f"{req.id}: {label!r} is not one of {[a.label for a in req.actions]}")

    @staticmethod
    def _answers(req: HumanRequirement) -> Dict[str, Any]:
        """Back to the dispatcher's shape from the chosen action + payload."""
        opts = req.subject.get("options") or []
        if req.chosen_action == FREE_TEXT_ACTION:
            return {"1": req.answer if isinstance(req.answer, str) else ""}
        if isinstance(req.answer, list) and opts:
            return {"1": [opts.index(x) + 1 for x in req.answer if x in opts]}
        if isinstance(req.answer, str):
            return {"1": req.answer}  # a typed label keeps the text shape the caller sent
        chosen = next((a for a in req.actions if a.id == req.chosen_action), None)
        if chosen is None:
            return {}
        if chosen.label in opts:
            return {"1": [opts.index(chosen.label) + 1]}
        return {"1": chosen.label}

    @staticmethod
    def first_answer(answers: dict, options: list):
        """DecisionHandler answers → the chosen option label(s) or free text.
        Shapes: {"1": [2]} (1-based option indexes) or {"1": "text"}."""
        a = answers.get("1")
        if a is None and answers:
            a = next(iter(answers.values()))
        if isinstance(a, list):
            labels = []
            for idx in a:
                try:
                    labels.append(options[int(idx) - 1]["label"])
                except (ValueError, IndexError, TypeError, KeyError):
                    pass
            return labels
        return a


def _json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)
