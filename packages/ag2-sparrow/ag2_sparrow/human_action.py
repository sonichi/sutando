"""human_action — sparrow side of the human-action bridge (v1 steps 2+3).

The `human-action-bridge.py` hook (main repo, hooks/) writes durable pending
actions to `<state>/human-actions/ha_*.json` and polls them for a decision.
This module closes the loop over the SHIPPED event channel:

  CardPoster       pending action, no card yet → post a question card into the
                   configured room via the gateway message op (sparrow already
                   holds URL/TOKEN — the hook stays transport-free) and record
                   the returned `card_event_id` as the correlation anchor.
  DecisionHandler  room events from the P0/P1 channel → if one is the OWNER
                   answering a pending action (reaction on the card, a reply
                   `answer ha_x 2`, or a bare option number replying to the
                   card), write the decision into the action file. The polling
                   hook picks it up within its poll interval.

Trust boundary: ONLY the configured owner mxid can resolve an action — anyone
else's reaction/reply is ignored (room activity is ambient; resolution is an
owner capability). No owner configured ⇒ the handler is inert. Terminal action
states are immutable — late or duplicate answers never overwrite a resolution,
and expired actions stay expired (the hook already gave Claude the timeout
deny; honoring a late answer would desync).

Chain routing: `HandlerChain([decisions, taskify])` — the first handler that
CLAIMS an event settles it; unclaimed events flow to the next. Decision events
(the owner's answer) are therefore consumed by the bridge and never counted as
taskify material. The chain preserves the EventConsumer handler contract
(offer -> settled ids, last_path passthrough), so the shipped consumer is
untouched.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

_KEYCAP = {f"{i}️⃣": i for i in range(1, 10)}  # 1️⃣..9️⃣ → 1..9
_ANSWER_RE = re.compile(r"\banswer\s+(ha_[0-9a-f]{6,})\s+([0-9][0-9,\s]*)", re.IGNORECASE)


def _relates_to(event: dict) -> "str | None":
    content = event.get("content") or {}
    rel = content.get("m.relates_to") or event.get("relates_to") or {}
    if isinstance(rel, dict):
        # reply shape nests one level deeper
        reply = rel.get("m.in_reply_to") or {}
        return rel.get("event_id") or reply.get("event_id")
    return None


def _text(event: dict) -> str:
    """Answer-bearing text of an event: the body, plus — when the envelope
    carries a structured A2UI click (content["space.ag2.a2ui.action"], per
    A2UI-CONTRACT.md) — that click's value/name. Button actions use our own
    `answer ha_x N` grammar, so folding them into the scanned text lets ONE
    regex serve typed replies, ▸-style click bodies, and structured clicks."""
    content = event.get("content") or {}
    parts = [str(content.get("body") or content.get("text") or "")]
    click = content.get("space.ag2.a2ui.action")
    if isinstance(click, dict):
        parts += [str(click.get("value") or ""), str(click.get("name") or "")]
    return " ".join(p for p in parts if p)


def _emoji_key(event: dict) -> "str | None":
    content = event.get("content") or {}
    rel = content.get("m.relates_to") or {}
    return rel.get("key") or content.get("key") or content.get("emoji")


class ActionStore:
    """Read/write access to the hook's pending-action files (shared contract)."""

    def __init__(self, store_dir: str):
        self.dir = store_dir

    def _path(self, action_id: str) -> str:
        return os.path.join(self.dir, action_id + ".json")

    def pending(self) -> list:
        out = []
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return out
        for name in names:
            if not (name.startswith("ha_") and name.endswith(".json")):
                continue
            try:
                with open(os.path.join(self.dir, name)) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if rec.get("status") == "pending":
                out.append(rec)
        return out

    def update(self, rec: dict) -> None:
        path = self._path(rec["action_id"])
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def resolve(self, action_id: str, answers: dict, resolved_by: str) -> bool:
        """Write a decision iff the action is still pending (terminal states
        immutable). Returns True when the decision landed."""
        try:
            with open(self._path(action_id)) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            return False
        if rec.get("status") != "pending":
            return False
        rec["status"] = "resolved"
        rec["decision"] = {"answers": answers}
        rec["resolved_by"] = resolved_by
        rec.setdefault("audit", []).append(
            {"at": time.time(), "event": "resolved", "by": resolved_by})
        self.update(rec)
        return True


class CardPoster:
    """Post question cards for pending actions that have none yet. One card per
    action (posted_card stamp), best-effort — a failed post retries next sweep."""

    def __init__(self, store: ActionStore, base_url: str, headers: dict,
                 room_id: str, log=print):
        self._store = store
        self._url = base_url.rstrip("/")
        self._headers = headers
        self._room = room_id
        self._log = log

    def _render(self, rec: dict) -> str:
        # Markdown text = the fallback (renders in any client); the fenced
        # ```a2ui block = the interactive card (A2UI-CONTRACT.md: the broker
        # lifts it into content["space.ag2.a2ui"], leaving the text outside the
        # block as the degraded view). Each option's `action` is our EXISTING
        # decision grammar (`answer ha_x N`), so a button click — which comes
        # back as a normal m.room.message carrying the action string — is
        # parsed by the same regex as a typed reply. Buttons are a macro.
        lines = ["**Claude is asking you a question**", ""]
        for qi, q in enumerate(rec.get("questions") or [], 1):
            lines.append(f"Q{qi}. {q.get('question', '?')}")
            for oi, opt in enumerate(q.get("options") or [], 1):
                lines.append(f"  {oi}. {opt.get('label', '?')}")
            lines.append("")
        lines.append(f"React with the option number, or reply "
                     f"`answer {rec['action_id']} <n>`.")
        first_q = (rec.get("questions") or [{}])[0]
        card = {
            "version": "0.9",
            "type": "buttons",
            "prompt": first_q.get("question", "?"),
            "options": [
                {"label": opt.get("label", "?"),
                 "action": f"answer {rec['action_id']} {oi}"}
                for oi, opt in enumerate(first_q.get("options") or [], 1)
            ],
        }
        lines += ["", "```a2ui", json.dumps(card, ensure_ascii=False), "```"]
        return "\n".join(lines)

    def sweep(self) -> int:
        posted = 0
        for rec in self._store.pending():
            if rec.get("card_event_id"):
                continue
            body = json.dumps({"op": "message", "room_id": self._room,
                               "text": self._render(rec)}).encode()
            req = urllib.request.Request(
                self._url + "/v1/room", data=body,
                headers={**self._headers, "Content-Type": "application/json"},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    reply = json.loads(resp.read().decode() or "{}")
            except Exception as e:  # noqa: BLE001 — best-effort, retry next sweep
                self._log(f"human-action: card post failed (will retry): {e}")
                continue
            event_id = reply.get("event_id") or (reply.get("result") or {}).get("event_id")
            rec["card_event_id"] = event_id
            rec.setdefault("audit", []).append(
                {"at": time.time(), "event": "card_posted", "card_event_id": event_id})
            self._store.update(rec)
            posted += 1
        return posted


class DecisionHandler:
    """EventConsumer-compatible handler that turns the owner's room activity
    into decisions on pending actions. Claims ONLY events it recognizes."""

    def __init__(self, store: ActionStore, owner_mxid: "str | None", log=print):
        self._store = store
        self._owner = owner_mxid
        self._log = log
        self.last_path = None  # handler-contract compat (never promotes tasks)

    def _match(self, event: dict) -> "tuple[dict, dict] | None":
        """Return (action_record, answers) when this event is a decision."""
        if not self._owner:
            return None  # no owner configured → inert (fail-closed)
        pending = self._store.pending()
        if not pending:
            return None
        by_card = {r.get("card_event_id"): r for r in pending if r.get("card_event_id")}
        etype = event.get("type")
        text = _text(event)

        rec, choice_nums = None, None
        if etype == "reaction.added":
            rec = by_card.get(_relates_to(event))
            key = _emoji_key(event) or ""
            num = _KEYCAP.get(key) or (int(key) if key.isdigit() else None)
            if rec and num:
                choice_nums = [num]
        elif etype == "message.created":
            m = _ANSWER_RE.search(text)
            if m:
                rec = next((r for r in pending if r["action_id"] == m.group(1)), None)
                choice_nums = [int(n) for n in re.findall(r"\d+", m.group(2))]
            else:
                rec = by_card.get(_relates_to(event))
                if rec and text.strip().isdigit():
                    choice_nums = [int(text.strip())]
        if not rec or not choice_nums:
            return None
        # AUTHORIZATION — the whole point: only the owner resolves.
        if event.get("actor_id") != self._owner:
            self._log(f"human-action: non-owner {event.get('actor_id')} tried to "
                      f"resolve {rec['action_id']} — ignored")
            return "unauthorized", None  # claimed (so taskify won't batch it), no decision
        answers = {}
        for q, num in zip(rec.get("questions") or [], choice_nums):
            opts = q.get("options") or []
            if 1 <= num <= len(opts):
                answers[q.get("question", "?")] = opts[num - 1].get("label", "?")
        if not answers:
            return None
        return rec, answers

    def claims(self, event: dict) -> bool:
        return self._match(event) is not None

    def offer(self, event: dict) -> list:
        eid = str(event.get("event_id") or "")
        matched = self._match(event)
        if not matched:
            return [eid] if eid else []
        rec, answers = matched
        if rec == "unauthorized":
            return [eid] if eid else []
        if self._store.resolve(rec["action_id"], answers,
                               str(event.get("actor_id"))):
            self._log(f"human-action: {rec['action_id']} resolved by "
                      f"{event.get('actor_id')} → {answers}")
        return [eid] if eid else []


class HandlerChain:
    """First handler that CLAIMS an event handles it; unclaimed events flow to
    the last handler (the default). Preserves the EventConsumer contract."""

    def __init__(self, handlers: list):
        self._handlers = handlers

    @property
    def last_path(self):
        for h in self._handlers:
            lp = getattr(h, "last_path", None)
            if lp:
                return lp
        return None

    def offer(self, event: dict) -> list:
        for h in self._handlers[:-1]:
            if hasattr(h, "claims") and h.claims(event):
                return h.offer(event)
        return self._handlers[-1].offer(event)
