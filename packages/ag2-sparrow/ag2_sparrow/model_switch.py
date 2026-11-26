"""The model card of a connector: the owner asks, in their DM with the agent, which model it runs on,
taps one on a card, and is told in that DM whether the switch happened.

One card for every runtime: a connector gives `ModelSwitch` the models its runtime offers (`seen`),
asks it whether a task is a card matter (`card_task`), runs the next owner turn on `wanted()`, and
reports the model that turn ran on (`tell`). What is kept, in `state/model.json`, is ids and names only.

One card, updated in place: a tap's answer ("Switching to X", "already X") and then the outcome
("Switched", "could not switch") are shown in place of the card that was tapped (connector_cards'
edit), never as a second card. A pick in words ("/model <name>") has no card to update and gets a new one.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid

from . import card_desk, connector_cards

FILE, CARD, ASK = "model.json", "model-switch", "/model"
# What a pick came to is a card of its own: recorded under the menu's id it would retire the menu
# (one live card per id and room), whose buttons the owner still sees and may tap again.
NOTE = "model-switch-status"
EXPIRED = "That card has expired, so nothing changed. Here's a new one."
EXPIRED_NO_CARD = "That card has expired, so nothing changed. Send /model for a new one."
BUTTONS = 6          # the card's button limit
HELD_TEXT = "I'll answer your last message on the model you pick."
REFUSED_UNNAMED = "The model I was set to"
UNTOLD_KEEP = 5      # outcome notices kept while their cards cannot be queued

# How the owner asks in their own words: the runtime answers with the mark alone, and the card is shown.
MARK = "[model-card]"
HINT = ("If the person asks to change, switch or choose the model you run on, or which models you have, "
        "reply with exactly %s and nothing else: they are then shown a card with the choices. Do not "
        "list or switch models yourself." % MARK)
IN_DM = "I can switch models from our direct message. Ask me there."
UNKNOWN = ("I don't know my models yet: I learn them when a turn starts. Send me any message, then "
           "send /model again.")
UNLISTED = "I could not get my list of models just now. Send /model again in a moment."
NOT_USABLE = "That model is not one I can use now. Send /model for the current list."
ANSWERED = "[no-send] the model card answers\n"
NOT_POSTED = "I could not post the model card. Try again in a moment."


def model_verb(model_id: str) -> str:
    """What a model's button carries. A card reply takes a short plain token, and an id may hold a
    slash, a colon or a space ("models/gemini-pro"): such an id travels as a digest of itself."""
    agent_card, _ = connector_cards._core()
    digest = "id." + hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:24]
    # An id that itself starts "id." is digested too, so no id can be another id's digest.
    if model_id.startswith("id."):
        return digest
    try:
        agent_card.reply("model", "agent", model_id)
        return model_id
    except ValueError:
        return digest


class ModelSwitch:
    """`publish(result_file, body)` writes a task's result; `quoted(text)` makes the connector's own
    words safe to publish; `refresh()`, when the runtime can list its models on request, is called
    before a card or a pick and returns False when the runtime would not list them. With `allowed`, only
    those ids are ever offered or accepted, and only while the runtime lists them."""

    KIND, ASK, MARK, HINT, IN_DM = "model", ASK, MARK, HINT, IN_DM      # what card_desk routes by
    CARD_ID = CARD
    # The card after a pick, before it is applied; a runtime that switches live says so (model_card).
    SWITCHING = "Switching to %s on your next message. I'll confirm here once it has switched."

    def display(self, names: dict, mid: str, fallback: str = "") -> str:
        """A model's name as the card shows it: the runtime's own name for `mid`, else `mid` itself."""
        return names.get(mid) or mid or fallback

    def named_in(self, words: str) -> str:
        """The model the owner's own words name for a direct pick ("switch to haiku"), '' for none: the card
        desk then picks it as a tap would. A runtime opts in by overriding this."""
        return ""

    def __init__(self, state_dir, root, bridge_state, name, publish, quoted, log, refresh=None, allowed=None):
        self.state_dir, self.root, self.bridge_state, self.name = state_dir, root, bridge_state, name
        self.publish, self.quoted, self.log, self.refresh = publish, quoted, log, refresh
        self.allowed = None if allowed is None else frozenset(allowed)
        self._unsaid = ""

    def unsaid(self) -> str:
        """An outcome whose card could not be built, in words, for the connector to add to its answer."""
        words, self._unsaid = self._unsaid, ""
        return words

    def ok(self, mid) -> bool:
        return bool(mid) and (self.allowed is None or mid in self.allowed)

    def offered(self, state: dict) -> list:
        """The models a card offers: what the runtime listed, within the allowlist."""
        return [m for m in state.get("available") or [] if isinstance(m, dict) and self.ok(m.get("id"))]

    def pending(self, state: dict) -> str:
        """A pick no turn has applied yet ('' when none): it is coming, not in use, and may yet be refused."""
        wanted = str(state.get("wanted") or "")
        return wanted if wanted and state.get("told") != wanted else ""

    def state(self) -> dict:
        try:
            with open(os.path.join(self.state_dir, FILE), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def save(self, **changes):
        state = dict(self.state(), **changes)
        path = os.path.join(self.state_dir, FILE)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
        os.replace(path + ".tmp", path)

    def wanted(self) -> "str | None":
        wanted = str(self.state().get("wanted") or "")
        return wanted if self.ok(wanted) else None

    def seen(self, offered, current):
        """Remember what the runtime offers: `offered` is (id, name) pairs, `current` the id in use."""
        # A name is one line: a newline or other run of whitespace would make the card refuse it.
        seen = {"available": [{"id": str(mid)[:200], "name": " ".join(str(name or mid).split())[:80] or str(mid)[:80]}
                              for mid, name in offered if self.ok(mid)],
                "current": str(current or "")[:200]}
        # With a pick, the turn is moved off the model its Session opened on: `tell` records what it ran on.
        if self.wanted():
            del seen["current"]
        if {k: self.state().get(k) for k in seen} != seen:
            self.save(**seen)

    def words(self, state: dict, picked: str = "", ran: "str | None" = None, same: bool = False, why: str = "") -> str:
        return self.parts(state, picked, ran, same, why)[2]

    def built(self, state: dict, picked: str = "", ran: "str | None" = None, same: bool = False, why: str = ""):
        """The card, or None when the card rules refuse it: the outcome is then said in words."""
        try:
            return self.card(state, picked, ran, same, why)
        except ValueError as e:
            self.log("model card not built:", str(e)[:200])
            return None

    def card(self, state: dict, picked: str = "", ran: "str | None" = None, same: bool = False, why: str = ""):
        """(body, card): one reply button per model offered. After a pick (`picked`) the card has no
        buttons: it says the switch is coming, then, with `ran` (the model the next turn ran on), that
        it happened or that it did not; with `same`, that the pick is the model already in use."""
        agent_card, issued_cards = connector_cards._core()
        if picked:
            kind, body, text, status = self.parts(state, picked, ran, same, why)
            return agent_card.build(NOTE, kind, title="Model", body=body, text=text, **({"status": status} if status else {}))
        names = {m["id"]: m["name"] for m in self.offered(state)}
        now, coming = state.get("current") or "", self.pending(state)
        refused = str(state.get("refused") or "")
        gone = names.pop(refused, refused)      # the account signed in cannot run it: named, not offered
        buttons = [agent_card.button(name, "reply", reply="Use %s" % name,
                                     command=agent_card.reply("model", "agent", model_verb(mid)))
                   for mid, name in list(names.items())[:BUTTONS]]
        more = list(names.values())[BUTTONS:]
        text = "%s isn't available on this account. Pick one it has." % gone if refused else \
            "Now using %s." % self.display(names, now, "the default model")
        if coming:
            text += " Pending: %s, on your next message." % self.display(names, coming)
        if more:
            text += " Also: %s (send /model and the name)." % ", ".join(more)
        waits = {"note": HELD_TEXT, "note_tone": "dim"} if refused and state.get("held") else {}
        # The menu says when it stops acting (the time its record is kept), so the app can show it expired.
        return agent_card.build(CARD, "pending", title="Model", body="Pick a model for %s" % self.name,
                                text=text, buttons=buttons, expires_at_ms=int((time.time() + issued_cards.TTL_S) * 1000),
                                **waits)

    def parts(self, state, picked, ran, same, why=""):
        """(kind, body, text, status) of the card after a pick; `why`, one short clause, says why a pick
        did not take."""
        names = {m["id"]: m["name"] for m in self.offered(state)}
        name = self.display(names, picked)
        if same:
            return "done", "Model: already %s" % name, "I'm already using %s. Nothing changed." % name, ""
        if ran is None:
            return "info", "Model: switching to %s" % name, self.SWITCHING % name, ""
        if ran == picked:
            return "done", "Model: now %s" % name, "Switched. I'm now using %s." % name, "Switched"
        reason = " (%s)" % why if why else ""
        return ("error", "Model: could not switch to %s" % name,
                "I could not switch to %s%s. I'm still using %s. Send /model to try again."
                % (name, reason, self.display(names, ran, "my default model")), "")

    def usable(self) -> bool:
        """Whether a card can reach the room: the agent's own bridge keeps a state dir (made at install).
        Without one (a bridge run elsewhere, a bare connector) nothing would post what is queued."""
        return bool(self.bridge_state) and os.path.isdir(self.bridge_state)

    def menu(self, state):
        """The card with the buttons, or None when it cannot be built (a name the card rules refuse):
        the ask is then answered in words, never left to raise and be retried."""
        try:
            return self.card(state)
        except ValueError as e:  # a name the card rules refuse
            self.log("model card not built:", str(e)[:200])
            return None

    def post(self, room, built, key: str = "", at_ns: int = 0, edit: str = "") -> bool:
        if built is None or not self.usable():
            return False
        try:
            connector_cards.queue(self.bridge_state, str(room or ""), *built, key=key, at_ns=at_ns,
                                  edit=edit if str(edit or "").startswith("$") else "")
            return True
        except (OSError, ValueError) as e:
            self.log("model card not queued:", "%s: %s" % (type(e).__name__, e))
            return False

    def tapped(self, verb, sent, result_file) -> str:
        """A tap on one of this card's buttons, as the desk hands it over."""
        return self.pick(verb, sent, result_file, by_verb=True)

    def asked_for(self, named, sent, result_file) -> str:
        """The owner's "/model" (the card) or "/model <name>" (the way to a model with no button)."""
        return self.pick(named.lower(), sent, result_file, by_name=True) if named else self.offer(sent, result_file)

    def expired(self, sent, result_file) -> str:
        """A tap on a card of this kind that no longer acts, as the desk hands it over."""
        return self.expired_card(sent, self.state(), result_file)

    def expired_card(self, sent, state, result_file) -> str:
        """Answer a tap on a model card that no longer acts: a fresh card in its place when one can be
        shown, else the words."""
        fresh = bool(self.offered(state)) and self.post(sent.get("channel_id"), self.menu(state),
                                                        edit=card_desk.tapped_event(sent))
        self.publish(result_file, ANSWERED if fresh else self.quoted(EXPIRED_NO_CARD) + "\n")
        return "model-card-expired"

    def listed(self, result_file) -> "dict | None":
        """The state with a fresh list from the runtime; None (and the owner told) when it would not list."""
        if self.refresh and self.refresh() is False:
            self.answer(result_file, self.quoted(UNLISTED) + "\n")
            return None
        return self.state()

    def pick(self, picked, sent, result_file, by_name=False, by_verb=False) -> str:
        """The owner chose `picked` (a tap or "/model <name>"): the next turn runs on it, and a card says so."""
        state, room = self.listed(result_file), str(sent.get("channel_id") or "")
        # A tap is answered in place of the card tapped; a pick in words has none.
        tapped = card_desk.tapped_event(sent) if by_verb else ""
        if state is None:
            return "model-unlisted"
        keys = (lambda m: (m["id"].lower(), m["name"].lower())) if by_name else (
            (lambda m: (model_verb(m["id"]),)) if by_verb else (lambda m: (m["id"],)))
        ids = [m["id"] for m in self.offered(state) if picked in keys(m)]
        # One model per button: a verb that names none, or more than one, picks nothing.
        if len(ids) != 1 and (by_verb or not ids):
            self.publish(result_file, self.quoted(NOT_USABLE) + "\n")
            return "model-unknown"
        picked, outcome = ids[0], "model-picked"
        if picked == str(state.get("wanted") or state.get("current") or ""):
            # Only a model a turn has run on is "in use": a repeated pending pick is still coming.
            outcome = "model-pending" if self.pending(state) == picked else "model-same"
        else:
            self.save(wanted=picked, asked_in=room, told="", card=tapped, refused="")
        same = outcome == "model-same"
        card = self.built(state, picked=picked, same=same)
        self.publish(result_file, ANSWERED if self.post(room, card, edit=tapped)
                     else self.quoted(self.words(state, picked=picked, same=same)) + "\n")
        return outcome

    def answer(self, result_file, body):
        """Publish a card matter's answer, led by any outcome this turn settled in words: a menu asked for in
        the same turn must not swallow them."""
        words = self.unsaid()
        if words:
            body = self.quoted(words) + "\n" + ("" if body == ANSWERED else body)
        self.publish(result_file, body)

    def offer(self, sent, result_file) -> str:
        """Answer the owner's ask with the model card (or the line saying why there is none)."""
        state = self.listed(result_file)
        if state is None:
            return "model-unlisted"
        if not self.offered(state):
            self.answer(result_file, self.quoted(UNKNOWN) + "\n")
        elif self.post(sent.get("channel_id"), self.menu(state)):
            self.answer(result_file, ANSWERED)
        else:
            self.answer(result_file, self.quoted(NOT_POSTED) + "\n")
        return "model-card"

    def tell(self, live, ran, room=None, may_fail=True, why: str = "") -> str:
        """After an owner turn that reached the runtime (`live`): settle the pick it ran with (`ran` is the
        model that turn ran on), then say in the DM each outcome not yet told, oldest first. The state is
        settled, and SAVED with the notice it owes, before any notice is queued, and each notice is queued
        under its own key: a connector that stops in between neither asks again for a refused model nor
        queues a notice twice. A notice whose card cannot be queued stays owed, the newest UNTOLD_KEEP of
        them; one that cannot be drawn is said in words once, in the answer of a turn in its own room
        (`room`), never another's. Without `may_fail` (a turn not the owner's own) a switch that did not
        happen settles nothing: the pick stays pending for the next turn. '' when there was nothing to settle or tell."""
        state = self.state()
        kept = state.get("untold")
        owed = [o for o in (kept if isinstance(kept, list) else [kept]) if isinstance(o, dict)]
        # A notice saved before notices had keys gets one now, and is saved with it before it is queued.
        unkeyed = [o for o in owed if not o.get("key")]
        for n, o in enumerate(unkeyed):
            o.update(key=uuid.uuid4().hex, at=time.time_ns() + n)
        wanted, outcome = str(state.get("wanted") or ""), ""
        if wanted and state.get("told") != wanted and live and (may_fail or (ran or "") == wanted):
            ran = ran or ""
            outcome = "switched" if ran == wanted else "not-switched"
            # Later than every notice already owed: the bridge posts in the order of this moment. Saved
            # whole: the cap is for what is still undeliverable after this turn's drain, below.
            at = max([time.time_ns()] + [int(o.get("at") or 0) + 1 for o in owed])
            owed.append({"picked": wanted, "ran": ran, "room": state.get("asked_in"), "key": uuid.uuid4().hex, "at": at,
                         **({"why": why[:120]} if why and outcome == "not-switched" else {}),
                         **({"edit": state["card"]} if state.get("card") else {})})
            # Only a confirmed pick becomes the model in use: a model the runtime moved the turn to was never picked.
            self.save(untold=owed, told=wanted, **({"current": wanted, "confirmed": wanted} if outcome == "switched"
                                                   else {"wanted": ""}))
        elif unkeyed:
            self.save(untold=owed)
        if live and ran and wanted and not outcome and ran != state.get("current"):
            self.save(current=ran)      # a settled pick the runtime no longer runs: what it ran on is shown
        told, words = "", []
        while owed:
            first = {"picked": str(owed[0].get("picked") or ""), "ran": str(owed[0].get("ran") or ""),
                     "why": str(owed[0].get("why") or "")}
            # A pick made on the app's settings page names no room: the page reads the outcome from
            # this state (model_settings), so it is settled without a card in any DM.
            card = self.built(state, **first) if owed[0].get("room") else False
            if card is False:
                pass
            elif card is None:
                if room is None or str(owed[0].get("room") or "") != str(room):
                    break           # its words wait for a turn in the room the card was for
                words.append(self.words(state, **first))
            elif not self.post(owed[0].get("room"), card, str(owed[0].get("key") or ""), int(owed[0].get("at") or 0),
                               str(owed[0].get("edit") or "")):
                break
            owed.pop(0)
            self.save(untold=list(owed))
            told = "switched" if first["picked"] == first["ran"] else "not-switched"
        self._unsaid = " ".join(words)
        if len(owed) > UNTOLD_KEEP:
            owed = owed[-UNTOLD_KEEP:]
            self.save(untold=owed)
        return "untold" if owed else (outcome or told)
