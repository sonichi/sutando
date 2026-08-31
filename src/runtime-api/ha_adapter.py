"""runtime-api ↔ human-action adapter — the v0 approve/answer transport.

The design doc's "server pending-request API + Space approve UI" already
exists at v0.5 in this repo: the human-action lifecycle. A pending action
written to `<state>/human-actions/ha_*.json` is picked up by the gateway's
CardPoster (question card with buttons in the owner's room, rendered by all
three clients) and resolved by DecisionHandler when the owner answers.

So v0 approval/elicitation does NOT need a new server or UI: this adapter
mirrors a runtime request into an ha_* pending action (CardPoster posts it)
and a resolver poll maps the owner's decision back onto the runtime request's
terminal state. `/v1/agent-requests` can formalize this server-side later
without touching the daemon's contract.

Mapping:
  approval.request     → single confirmation question (Approve / Deny)
  elicitation.request  → free_text / single_select / multi_select /
                         confirmation question
Resolution:
  decision.answers {"1": [idx, ...]} or {"1": "free text"}  (DecisionHandler
  shapes) → approved/denied (approval) or resolved+answer (elicitation).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Sibling-import bootstrap (NOT workspace resolution — that goes through the
# sanctioned sutando_config helpers below): put src/ on sys.path so
# sutando_config imports, then let its marker-walking _find_repo_root locate
# the repo root for the in-repo sparrow package.
_HERE = Path(__file__).resolve().parent  # src/runtime-api
_SRC = _HERE.parent                      # src
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from sutando_config import _find_repo_root  # noqa: E402

_REPO = _find_repo_root(_HERE) or _SRC.parent
_PKG = str(_REPO / "packages" / "ag2-sparrow")
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

from ag2_sparrow.human_action import ActionStore  # noqa: E402


def _now() -> float:
    return time.time()


def ha_action_id(request_id: str) -> str:
    """ha action id for a runtime request — MUST stay matchable by
    DecisionHandler's answer grammar `ha_[0-9a-f]{6,}` (hex only!). The
    requestId's uuid tail is hex; the type prefix ("approval-") is NOT and
    must never leak into the id. Live-acceptance finding 2026-07-26: the
    first cut used the full requestId and the owner's real answer could not
    match — the local E2E missed it by writing resolutions directly."""
    return "ha_" + request_id.split("-", 1)[-1][:24]


class HumanActionAdapter:
    def __init__(self, actions_dir: str):
        Path(actions_dir).mkdir(parents=True, exist_ok=True)
        self.store = ActionStore(actions_dir)

    # ── outbound: runtime request → pending ha action ──────────────────────
    def open_approval(self, request: dict) -> str:
        p = request["params"]
        action_line = p.get("action", "?")
        resource = p.get("resource")
        inp = p.get("input")
        reason = p.get("reason")
        # The card must show the FULL effect being approved — including the
        # governed input (for message.send, the input IS the message body).
        # The daemon binds the approval to this exact effect (review P1:
        # an unshown input could be substituted after the owner answered).
        q = (f"Approve: {action_line}"
             + (f"\nResource: {json.dumps(resource, ensure_ascii=False)}" if resource else "")
             + (f"\nInput: {json.dumps(inp, ensure_ascii=False)}" if inp else "")
             + (f"\nReason: {reason}" if reason else ""))
        return self._write(request, [{
            "question": q,
            "options": [{"label": "Approve"}, {"label": "Deny"}],
        }])

    def open_elicitation(self, request: dict) -> str:
        p = request["params"]
        etype = p.get("type", "single_select")
        options = [{"label": str(o)} for o in (p.get("options") or [])]
        if etype == "confirmation" and not options:
            options = [{"label": "Yes"}, {"label": "No"}]
        q = {"question": str(p.get("question", "?")), "options": options}
        if etype == "multi_select":
            # The multiSelect flag switches DecisionHandler to the comma-list
            # grammar; without it multiple numbers are rejected by the
            # single-select branch (review P1 dead path).
            q["multiSelect"] = True
        return self._write(request, [q])

    def open_human_action(self, request: dict) -> str:
        """A real-world act the human must perform (sign, pay, plug in, ...).
        The card asks for the act and takes the outcome; Done/Decline map to
        the request's completed/declined terminal states."""
        p = request["params"]
        q = (f"Action needed: {p.get('action', '?')}"
             + (f"\nInstructions: {p['instructions']}" if p.get("instructions") else "")
             + (f"\nDeadline: {p['deadline']}" if p.get("deadline") else ""))
        return self._write(request, [{
            "question": q,
            "options": [{"label": "Done"}, {"label": "Decline"}],
        }])

    def close(self, action_id: str, resolved_by: str, note: str | None = None) -> None:
        """Resolve a still-pending card out-of-band (API completion path) so
        CardPoster stops showing a question the requester already settled."""
        rec = self.store.get(action_id)
        if rec is None or rec.get("status") != "pending":
            return
        rec["status"] = "resolved"
        rec["resolved_by"] = resolved_by
        rec["decision"] = {"answers": {}, "via": "runtime-api",
                           **({"note": note} if note else {})}
        rec.setdefault("audit", []).append(
            {"at": _now(), "event": "resolved-via-api", "by": resolved_by})
        self.store.update(rec)

    def _write(self, request: dict, questions: list) -> str:
        action_id = ha_action_id(request["requestId"])
        now = _now()
        rec = {
            "action_id": action_id,
            "kind": "runtime_request",
            "runtime_request_id": request["requestId"],
            "status": "pending",
            "claude_session_id": None,
            "tool_input": {"questions": questions},
            "questions": questions,
            "decision": None,
            "resolved_by": None,
            "created_at": now,
            "expires_at": request.get("expiresAt") or (now + 24 * 3600),
            "audit": [{"at": now, "event": "created",
                       "runtime_request": request["requestId"]}],
        }
        self.store.update(rec)
        return action_id

    # ── inbound: resolved ha action → runtime terminal state ───────────────
    def poll_resolution(self, action_id: str):
        """Return (status, payload, resolved_by) once the ha action reaches a
        terminal state, else None while pending. status ∈ resolved|expired."""
        rec = self.store.get(action_id)
        if rec is None:
            return None
        if rec.get("status") == "pending":
            if rec.get("expires_at") and _now() > rec["expires_at"]:
                return ("expired", None, None)
            return None
        if rec.get("status") == "resolved":
            answers = ((rec.get("decision") or {}).get("answers")) or {}
            return ("resolved", answers, rec.get("resolved_by"))
        return ("expired", None, rec.get("resolved_by"))

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
