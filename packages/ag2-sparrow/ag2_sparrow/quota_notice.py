"""The quota notice of a connector: the account card, unasked, in the owner's DM, once the runtime says it
is out of usage. The local Claude Code version (adapters/claude_code/quota_card.py) reads the pane; a
connector is told by its runtime (Codex's app-server, agy's ACP errors) and hands that here.

A runtime at its limit cannot answer, so the connector itself (no model turn) queues the account card,
carrying the `account_controls` marker so the desktop draws the controls in place: what happened, when it
resets if the runtime said, the account in use, and the controls to switch with. One card per limit: the
next only after the limit cleared (a turn that answered, or the reset time the runtime gave has passed)
and came back.

Every owner message is told, not only the first: each message of the owner's that the same limit refuses
(`hit(task=..., task_text=...)`) gets the card too, once per task id, in their DM. One that could not be
queued is kept as owed, with its card's key, and queued on a later tick: once, never in the task's room.

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

from . import account_switch, card_desk, connector_cards

FILE, ROOM_FILE = "quota-card.json", "quota-room.json"   # the incident; the owner's DM room
TASKS_KEEP = 50                 # the owner messages of one limit remembered as told
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

    def remember(self, task_text: str = "", path=None) -> None:
        """A message of the owner's in their DM (the task's text as the bridge wrote it, or its file at `path`)
        names the room a notice goes to. Any other sender or room is ignored; nothing here stops the task."""
        try:
            if path is not None:
                with open(path, encoding="utf-8") as f:
                    task_text = f.read()
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

    def says(self) -> str:
        """The limit in force, in words, for a card the owner asks for meanwhile; '' when none is, or its reset passed."""
        rec = self.load()
        until = rec.get("reset_at")
        if not self.limited() or (until is not None and self.now() >= float(until)):
            return ""
        return words(self.name, str(rec.get("reset") or ""), "")

    def cleared(self) -> str:
        """The runtime answered: the limit is over, and a later one is a new card."""
        if not self.load().get("since"):
            return ""
        self.save({})
        return "quota-cleared"

    def hit(self, reset: str = "", reset_at=None, task: str = "", task_text: str = "") -> str:
        """The runtime said it is out of usage (`reset`: when it ends, in words; `reset_at`: as epoch seconds,
        when known). What was done: 'quota-card' (queued for the owner's DM), 'quota-card-posted',
        'quota-card-dropped' (queued again on the next tick), 'quota-no-room', 'quota-not-posted' (tried again
        later), or '' for nothing (told already, or still on its way). With `task` (the refused task's id and
        its text as the bridge wrote it), a message of the owner's the limit's card did not answer gets a card
        of its own: 'quota-task-card', said after whatever became of the first."""
        done = self._hit(reset, reset_at)
        try:
            more = self._tell_task(task, task_text, done) if task else ""
        except Exception as e:  # noqa: BLE001 - the limit's own card stands without it
            self.log("quota card for the message not queued:", "%s: %s" % (type(e).__name__, str(e)[:200]))
            more = ""
        return " ".join(w for w in (done, more) if w)

    def answered(self, task: str) -> bool:
        """Whether a card of this limit was queued for `task`."""
        return bool(task) and task in (self.load().get("tasks") or [])

    def owed(self) -> list:
        """The owner messages whose card is not queued yet."""
        return sorted(self.load().get("owed") or {})

    def _tell_task(self, task: str, task_text: str, done: str) -> str:
        """The owner's message `task`, refused by a limit already told: the card again, once per task id.
        It is owed (kept, with its card's key) before it is queued, so one that could not be is tried again."""
        agent_card, _ = connector_cards._core()
        sent, _ = agent_card.reply_words(task_text or "")
        if agent_card.owner_may_act(sent, dm_only=False) is not None or agent_card.click_tagged(sent):
            return ""                    # not the owner's own words: no account card for it
        rec = self.load()
        told = [t for t in rec.get("tasks") or [] if isinstance(t, str)]
        owed = dict(rec.get("owed") or {})
        if task in told or task in owed or not rec.get("since"):
            return ""
        # Asked in a room that is not the owner's DM: the card answers there, without the account's name.
        seen = str(sent.get("channel_id") or "")
        where = seen if agent_card.owner_may_act(sent) is not None and _ROOM.match(seen) else ""
        if done == "quota-card" and not where:      # the card queued just now answers this message
            self.save(dict(rec, tasks=(told + [task])[-TASKS_KEEP:]))
            return ""
        owed[task] = dict({"key": "quota-task:%s:%s" % (task, uuid.uuid4().hex), "at": time.time_ns()},
                          **({"room": where} if where else {}))
        if not self.save(dict(rec, owed=owed)):
            return "quota-not-posted"
        return self._owed(self.now(), now_too=True)

    def _owed(self, at: float, now_too: bool = False) -> str:
        """Queue the card of each owner message still owed one; '' when none is, or it is too soon to retry."""
        rec = self.load()
        owed = {t: e for t, e in (rec.get("owed") or {}).items() if isinstance(e, dict) and e.get("key")}
        if not (rec.get("told") or rec.get("qid")):
            # No card stands in the DM yet: the limit's own, once queued, answers the messages sent there.
            owed = {t: e for t, e in owed.items() if e.get("room")}
        if not owed:
            return ""
        if not now_too and at - float(rec.get("owed_tried") or 0) < RETRY_S:
            return ""
        dm, sent, built = self.room(), [], {}
        if card_desk.usable(self.bridge_state):
            for task, entry in owed.items():
                to, there = entry.get("room") or dm, bool(entry.get("room"))
                if not to:
                    continue
                try:    # keyed: a card queued and not recorded is not queued a second time
                    if there not in built:
                        built[there] = self._card(rec, named=not there)
                    connector_cards.queue(str(self.bridge_state), to, *built[there], key=entry["key"], at_ns=entry["at"])
                except (OSError, ValueError) as e:
                    self.log("quota card for %s not queued:" % task, "%s: %s" % (type(e).__name__, e))
                    continue
                sent.append(task)
        told = [t for t in rec.get("tasks") or [] if isinstance(t, str)]
        rec = self.load()
        self.save(dict(rec, owed={t: e for t, e in (rec.get("owed") or {}).items() if t not in sent}, owed_tried=at,
                       tasks=(told + sent)[-TASKS_KEEP:]))
        reachable = dm or any(e.get("room") for e in owed.values())
        return "quota-task-card" if sent else ("quota-not-posted" if reachable else "quota-no-room")

    def _card(self, rec: dict, named: bool = True):
        """The limit's card; `named` says the account in use (the owner's DM only, never a shared room)."""
        using = ""
        if named:
            try:
                using = self.using() or ""
            except Exception as e:  # noqa: BLE001 - the card still says what it can
                self.log("account not read:", "%s: %s" % (type(e).__name__, str(e)[:200]))
        return account_switch.limited(self.build(words(self.name, str(rec.get("reset") or ""), using)))

    def _hit(self, reset: str = "", reset_at=None) -> str:
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
        if not rec.get("since"):
            return ""
        return ("" if rec.get("told") else self._deliver(rec, self.now())) or self._owed(self.now())

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
            built = self._card(rec)
            qid = connector_cards.queue(str(self.bridge_state), to, *built, key=rec["key"], at_ns=rec["at"])
        except (OSError, ValueError) as e:
            self.log("quota card not queued:", "%s: %s" % (type(e).__name__, e))
            return "quota-not-posted"
        owed = rec.get("owed") or {}
        rooms = {t: e for t, e in owed.items() if isinstance(e, dict) and e.get("room")}   # theirs go to their rooms
        told = [t for t in rec.get("tasks") or [] if isinstance(t, str)] + [t for t in owed if t not in rooms]
        self.save(dict(rec, qid=qid, queued=at, owed=rooms, tasks=told[-TASKS_KEEP:]))    # this card answers the rest
        return "quota-card"
