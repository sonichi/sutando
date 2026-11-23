"""The quota notice of a connector: the account card, unasked, in the owner's DM, once the runtime says it
is out of usage. The local Claude Code version (adapters/claude_code/quota_card.py) reads the pane; a
connector is told by its runtime (Codex's app-server, agy's ACP errors) and hands that here.

A runtime at its limit cannot answer, so the connector itself (no model turn) queues the account card,
carrying the `account_controls` marker so the desktop draws the controls in place: what happened, when it
resets if the runtime said, the account in use, and the controls to switch with. One card per limit: the
next only after the limit cleared (a turn that answered, or the reset time the runtime gave has passed)
and came back.

The owner's DM room is not something a connector is told; it is remembered from the owner's own messages
there (`remember`), in a record of its own (quota-room.json), apart from the incident's (quota-card.json).
Until a room has been seen no card can be sent.

Queued is not told. The bridge drops a card it could not post within connector_cards.MAX_QUEUED_S, so the
incident counts as told only once the card left the outbox in time or its posted receipt is there; one
that was dropped is queued again (the same card id, which replaces the earlier in the room). Nothing here
changes an account. English only.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from . import card_desk, connector_cards

FILE = "quota-card.json"        # the incident
ROOM_FILE = "quota-room.json"   # the owner's DM room
RETRY_S = 60.0                  # a card that could not be queued is tried again no sooner than this
_ROOM = re.compile(r"^![^\s:]+(:\S+)?$")
# When a runtime's own error text says the limit ends: "resets 3pm", "try again in 2 days 3 hours",
# "try again at 3:05 PM". Only time-like characters of it are kept: nothing else of the error reaches the DM.
_RESET = re.compile(r"\b(?:resets?(?: after| at| in)?|try again (?:at|in|after)|retry (?:at|in|after))\s+"
                    r"([\w :,./+-]{1,40})", re.IGNORECASE)
_TIMEISH = re.compile(r"[^\w :,./+-]")


def reset_from_text(text: str) -> str:
    """When the limit resets, as the runtime's error words say it ("in 2 days 3 hours", "3:05 PM"); ''."""
    found = _RESET.search(str(text or ""))
    if not found:
        return ""
    said = " ".join(_TIMEISH.sub("", found.group(1)).split()).rstrip(" ,:./+-")
    said = re.split(r"\.\s|\s(?:or|and|to|for|if)\s", said, maxsplit=1)[0]
    return said[:40]


def reset_from_epoch(at, now=None) -> str:
    """A reset moment (epoch seconds) in local words: "3:05 PM" today, "Nov 24, 3:05 PM" otherwise; ''."""
    try:
        at = float(at)
    except (TypeError, ValueError):
        return ""
    if at <= 0:
        return ""
    when, today = time.localtime(at), time.localtime(time.time() if now is None else now)
    clock = time.strftime("%I:%M %p", when).lstrip("0")
    if (when.tm_year, when.tm_yday) == (today.tm_year, today.tm_yday):
        return clock
    return "%s %d, %s" % (time.strftime("%b", when), when.tm_mday, clock)


def words(name: str, reset: str, using: str) -> str:
    return ("%s has reached its usage limit%s, so I can't answer until then. Switch to another account or an API "
            "key to go on now.%s" % (name, " (resets %s)" % reset if reset else " (it does not say when that resets)",
                                     " " + using if using else ""))


class QuotaNotice:
    """`build(text) -> (body, card)` is the connector's account card (its account_controls marker on), led by
    `text`; `using()` names the account in use in words ('' when it cannot say)."""

    def __init__(self, state_dir, bridge_state, name, build, using, log, now=time.time):
        self.state_dir, self.bridge_state, self.name = str(state_dir), bridge_state, name
        self.build, self.using, self.log, self.now = build, using, log, now

    # ── records ──
    def _read(self, name) -> dict:
        try:
            with open(os.path.join(self.state_dir, name), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            return {}
        return rec if isinstance(rec, dict) else {}

    def _write(self, name, rec: dict) -> bool:
        path = os.path.join(self.state_dir, name)
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(rec, f, sort_keys=True)
            os.replace(path + ".tmp", path)
            return True
        except OSError as e:
            self.log("quota record not kept:", "%s: %s" % (type(e).__name__, e))
            return False

    def load(self) -> dict:
        return self._read(FILE)

    def save(self, rec: dict) -> bool:
        return self._write(FILE, rec)

    def room(self) -> str:
        """The owner's DM room as last seen, '' when none has been."""
        kept = str(self._read(ROOM_FILE).get("room") or "")
        return kept if _ROOM.match(kept) else ""

    def remember(self, task_text: str) -> None:
        """A message of the owner's in their DM (the task's text, as the bridge wrote it) names the room a
        notice goes to. Any other sender or room is ignored; nothing here stops the task."""
        try:
            agent_card, _ = connector_cards._core()
            sent, _ = agent_card.reply_words(task_text or "")
            seen = str(sent.get("channel_id") or "")
            if agent_card.owner_may_act(sent) is None and _ROOM.match(seen) and self.room() != seen:
                self._write(ROOM_FILE, {"room": seen})
        except Exception as e:  # noqa: BLE001 - the task goes on without it
            self.log("owner DM not remembered:", "%s: %s" % (type(e).__name__, e))

    # ── the incident ──
    def limited(self) -> bool:
        """Whether a limit is being told or was told and has not cleared: the card is the notice, so the
        room needs no error line of its own."""
        rec = self.load()
        return bool(rec.get("since")) and bool(rec.get("told") or rec.get("qid"))

    def cleared(self) -> str:
        """The runtime answered: the limit is over, and a later one is a new card."""
        if not self.load().get("since"):
            return ""
        self.save({})
        return "quota-cleared"

    def hit(self, reset: str = "", reset_at=None) -> str:
        """The runtime said it is out of usage (`reset`: when it ends, in words; `reset_at`: as epoch seconds,
        when known). What was done: 'quota-card' (queued for the owner's DM), 'quota-card-posted',
        'quota-card-dropped' (queued again on the next tick), 'quota-no-room', 'quota-not-posted' (tried again
        later), or '' for nothing (told already, or still on its way)."""
        rec, at = self.load(), self.now()
        until = rec.get("reset_at")
        if rec.get("since") and until is not None and at >= float(until) and (rec.get("told") or rec.get("qid")):
            rec = {}                     # the earlier limit reset and this is a new one: a new card
        if not rec.get("since"):
            rec = {"since": at}
        if reset_at is not None:
            rec["reset_at"] = float(reset_at)
        if reset and not rec.get("reset"):
            rec["reset"] = reset[:60]
        return self._deliver(rec, at)

    def tick(self) -> str:
        """Between turns: settle a queued card's fate, and queue again one the bridge dropped. '' when idle."""
        rec = self.load()
        if not rec.get("since") or rec.get("told"):
            return ""
        return self._deliver(rec, self.now())

    def delivery(self, rec: dict, at: float) -> str:
        """What became of the card `rec` queued: 'posted', 'pending' (still in the bridge's outbox), or
        'dropped'. A card gone from the outbox before the bridge's deadline can only have been posted."""
        name = str(rec.get("qid") or "") + ".json"
        bridge = Path(str(self.bridge_state or ""))
        if bridge.joinpath(*connector_cards.POSTED, name).is_file():
            return "posted"
        if bridge.joinpath(*connector_cards.OUTBOX, name).is_file():
            return "pending"
        return "posted" if at - float(rec.get("queued") or 0) <= connector_cards.MAX_QUEUED_S else "dropped"

    def _deliver(self, rec: dict, at: float) -> str:
        if rec.get("told"):
            self.save(rec)
            return ""
        if rec.get("qid"):
            fate = self.delivery(rec, at)
            if fate == "pending":
                self.save(rec)
                return ""
            if fate == "posted":
                self.save(dict(rec, told=True))
                return "quota-card-posted"
            self.save({k: v for k, v in rec.items() if k not in ("qid", "key", "at", "queued", "tried")})
            return "quota-card-dropped"
        if at - float(rec.get("tried") or 0) < RETRY_S:
            self.save(rec)
            return ""
        # The card's key is saved before it is queued: a stop between the two queues the same card, not a second.
        rec = dict(rec, tried=at, key=rec.get("key") or uuid.uuid4().hex, at=rec.get("at") or time.time_ns())
        if not self.save(rec):
            return "quota-not-posted"
        to = self.room()
        if not to:
            return "quota-no-room"
        if not card_desk.usable(self.bridge_state):
            return "quota-not-posted"
        try:
            using = self.using() or ""
        except Exception as e:  # noqa: BLE001 - the card still says what it can
            self.log("account not read:", "%s: %s" % (type(e).__name__, str(e)[:200]))
            using = ""
        try:
            built = self.build(words(self.name, str(rec.get("reset") or ""), using))
            qid = connector_cards.queue(str(self.bridge_state), to, *built, key=rec["key"], at_ns=rec["at"])
        except (OSError, ValueError) as e:
            self.log("quota card not queued:", "%s: %s" % (type(e).__name__, e))
            return "quota-not-posted"
        self.save(dict(rec, qid=qid, queued=at))
        return "quota-card"
