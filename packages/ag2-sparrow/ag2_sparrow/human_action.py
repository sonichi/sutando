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

import fcntl
import json
import os
import re
import time
import urllib.request
import uuid

_KEYCAP = {f"{i}️⃣": i for i in range(1, 10)}  # 1️⃣..9️⃣ → 1..9
# The `answer` keyword is OPTIONAL: live human acceptance (2026-07-26) showed
# a real owner naturally types the bare `ha_<hex> 1` — a grammar humans fail
# on the first try is a UX defect, not a user error. Intent boundary (review):
# the EXPLICIT `answer ha_<hex> N` form may appear inside prose (the keyword
# is the intent signal), but the bare `ha_<hex> N` form must BE the entire
# trimmed message — an owner message that merely QUOTES a pending token in
# prose must never resolve the action. Both forms remain bounded by the same
# gates: owner-only sender, PENDING id, valid option numbers.
_ANSWER_EXPLICIT_RE = re.compile(
    r"\banswer\s+(ha_[0-9a-f]{6,})\s+([0-9][0-9,\s]*)", re.IGNORECASE)
_ANSWER_BARE_RE = re.compile(
    r"(ha_[0-9a-f]{6,})\s+([0-9][0-9,\s]*)", re.IGNORECASE)


def _parse_answer(text: str) -> "tuple[str, str] | None":
    """(action_id, numbers) from an owner message, or None. Explicit form is
    searched anywhere; bare form must fullmatch the trimmed message."""
    m = _ANSWER_EXPLICIT_RE.search(text)
    if m is None:
        m = _ANSWER_BARE_RE.fullmatch(text.strip())
    return (m.group(1), m.group(2)) if m else None


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

    def get(self, action_id: str) -> "dict | None":
        try:
            with open(self._path(action_id)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

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

    def _lock_path(self, action_id: str) -> str:
        return os.path.join(self.dir, action_id + ".lock")

    def transition_lock(self, action_id: str):
        """flock-guarded critical section shared by EVERY writer of an action
        file (this resolver AND the hook's timeout path — same protocol, same
        lock file). Serializes read→check→write so exactly one terminal
        transition can win (review: CAS race between resolver and expiry)."""
        lock_file = open(self._lock_path(action_id), "a+")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return lock_file

    def update(self, rec: dict) -> None:
        """Durable atomic write: unique temp name (the hook's timeout writer
        may run concurrently — a fixed name could be clobbered mid-write),
        fsync the data before rename and the directory entry after, so a
        crash can never lose an acknowledged decision (review blocker)."""
        path = self._path(rec["action_id"])
        tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def resolve(self, action_id: str, answers: dict, resolved_by: str) -> bool:
        """Write a decision iff the action is still pending (terminal states
        immutable). Runs read→check→write under the shared transition lock so
        a concurrent expiry writer cannot interleave. Returns True when the
        decision landed — and only then is the answer event safe to consume."""
        lk = self.transition_lock(action_id)
        try:
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
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
            lk.close()


class CardPoster:
    """Post question cards for pending actions that have none yet. One card per
    action (posted_card stamp), best-effort — a failed post retries next sweep."""

    def __init__(self, store: ActionStore, base_url: str, headers: dict,
                 room_id: str, log=print, include_a2ui: bool = False):
        self._store = store
        self._url = base_url.rstrip("/")
        # Cloudflare rejects urllib's default UA with 403 (same edge rule the
        # bridge's gateway path documents) — without an explicit client UA the
        # card post would retry forever on the real gateway (review blocker).
        # BY REFERENCE (no copy): the owning bridge mutates this dict on token
        # rotation; the post path below builds a per-request copy anyway.
        self._headers = headers
        self._headers.setdefault("User-Agent", "sutando-gateway-client/1.0")
        self._room = room_id
        self._log = log
        # A2UI block is OPT-IN: the deployed web client does not render
        # space.ag2.a2ui yet — it shows an unclickable "Room App" chip AND
        # hides the text fallback (observed live 2026-07-24), which is worse
        # than plain text. Flip on (SPARROW_HA_A2UI) once the client renderer
        # (backend repo a2ui/a2ui-renderer.js — client-integration lane) ships.
        self._a2ui = bool(include_a2ui)

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
        # Instructions must match what _complete_answers actually accepts for
        # THIS action's shape — a card telling the owner to react on a
        # multi-question action would be silently claimed-but-pending.
        aid = rec["action_id"]
        qs = rec.get("questions") or []
        if len(qs) <= 1 and not (qs and qs[0].get("multiSelect")):
            lines.append(f"React with the option number, or reply "
                         f"`answer {aid} <n>`.")
        elif len(qs) <= 1:
            lines.append(f"Pick one or more: reply `answer {aid} <n1>,<n2>,...` "
                         f"(a reaction or a single number picks just one).")
        elif any(q.get("multiSelect") for q in qs):
            lines.append("This card mixes multiple questions with multi-select, "
                         "which can't be answered by numbers here — if it stays "
                         "unanswered, Claude decides autonomously at timeout.")
        else:
            lines.append(f"Reply `answer {aid} <n1>,<n2>,...` — one number per "
                         f"question, in order. (Reactions can't answer a "
                         f"multi-question card.)")
        # A2UI buttons are single-pick macros for `answer <id> <n>` — only a
        # single-question single-select action can be resolved by one click, so
        # other shapes keep the text-only card (same silent-claim hazard).
        if not self._a2ui or len(qs) != 1 or qs[0].get("multiSelect"):
            return "\n".join(lines)
        first_q = qs[0]
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
                               "body": self._render(rec)}).encode()
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
            # Record the card on the CURRENT record under the shared transition
            # lock — the action can resolve/expire while the POST is in flight,
            # and writing the pre-POST snapshot back would RESURRECT a terminal
            # action to pending with a stale card id (review P1, twice
            # verified). Re-read under the lock; only a still-pending action
            # accepts the card stamp — terminal states are never touched.
            lk = self._store.transition_lock(rec["action_id"])
            try:
                current = self._store.get(rec["action_id"])
                if current and current.get("status") == "pending":
                    current["card_event_id"] = event_id
                    current.setdefault("audit", []).append(
                        {"at": time.time(), "event": "card_posted",
                         "card_event_id": event_id})
                    self._store.update(current)
                    posted += 1
                else:
                    self._log(f"human-action: {rec['action_id']} reached a "
                              f"terminal state during card post — card stamp "
                              f"skipped (no resurrect)")
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
                lk.close()
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
            parsed = _parse_answer(text)
            if parsed:
                rec = next((r for r in pending if r["action_id"] == parsed[0]), None)
                choice_nums = [int(n) for n in re.findall(r"\d+", parsed[1])]
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
        answers = self._complete_answers(rec, choice_nums, etype)
        if answers is None:
            # Matched the action but the answer set is incomplete/invalid
            # (review: a single reaction must not terminally resolve a
            # multi-question action with only Q1 answered). Claim the event so
            # taskify never batches it, but leave the action PENDING so the
            # owner can retry with the full `answer ha_x n1,n2,...` form.
            self._log(f"human-action: incomplete/invalid answer for "
                      f"{rec['action_id']} ({choice_nums}) — action stays pending")
            return "unauthorized", None
        return rec, answers

    def _complete_answers(self, rec: dict, nums: list, etype: str) -> "dict | None":
        """Map choice numbers to a COMPLETE, valid answer set — or None.
        Rules (review: preserve AskUserQuestion multi-select semantics):
        - one question, single-select: exactly one in-range number.
        - one question, multiSelect: every number in range; labels joined.
          Reaction/bare-number forms stay valid only for the single pick.
        - multiple questions: only the explicit answer-command form, one number
          per question in order (count must equal len(questions)); multiSelect
          inside a multi-question set can't be expressed by numbers → never
          resolved by this router (surface stays pending for a typed answer).
        """
        questions = rec.get("questions") or []
        if not questions:
            return None
        if len(questions) == 1:
            q = questions[0]
            opts = q.get("options") or []
            if any(n < 1 or n > len(opts) for n in nums):
                return None
            if q.get("multiSelect"):
                labels = [opts[n - 1].get("label", "?") for n in dict.fromkeys(nums)]
                return {q.get("question", "?"): ", ".join(labels)}
            if len(nums) != 1:
                return None
            return {q.get("question", "?"): opts[nums[0] - 1].get("label", "?")}
        # multi-question: require the full ordered vector, no multiSelect
        if etype == "reaction.added":
            return None
        if len(nums) != len(questions):
            return None
        if any(q.get("multiSelect") for q in questions):
            return None
        answers = {}
        for q, num in zip(questions, nums):
            opts = q.get("options") or []
            if not (1 <= num <= len(opts)):
                return None
            answers[q.get("question", "?")] = opts[num - 1].get("label", "?")
        return answers

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
