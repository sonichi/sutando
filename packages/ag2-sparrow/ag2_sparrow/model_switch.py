"""The model card of a connector: the owner asks, in their DM with the agent, which model it runs on,
taps one on a card, and is told in that DM whether the switch happened.

One card for every runtime: a connector gives `ModelSwitch` the models its runtime offers (`seen`),
asks it whether a task is a card matter (`card_task`), runs the next owner turn on `wanted()`, and
reports the model that turn ran on (`tell`). What is kept, in `state/model.json`, is ids and names only.
"""
from __future__ import annotations

import json
import os

from . import connector_cards

FILE, CARD, ASK = "model.json", "model-switch", "/model"
BUTTONS = 6          # the card's button limit
# How the owner asks in their own words: the runtime answers with the mark alone, and the card is shown.
MARK = "[model-card]"
HINT = ("If the person asks to change, switch or choose the model you run on, or which models you have, "
        "reply with exactly %s and nothing else: they are then shown a card with the choices. Do not "
        "list or switch models yourself." % MARK)
IN_DM = "I can switch models from our direct message. Ask me there."
UNKNOWN = ("I don't know my models yet: I learn them when a turn starts. Send me any message, then "
           "send /model again.")
ANSWERED = "[no-send] the model card answers\n"


class ModelSwitch:
    """`publish(result_file, body)` writes a task's result; `quoted(text)` makes the connector's own
    words safe to publish; `refresh()`, when the runtime can list its models on request, is called
    before the card is shown."""

    def __init__(self, state_dir, root, bridge_state, name, publish, quoted, log, refresh=None):
        self.state_dir, self.root, self.bridge_state, self.name = state_dir, root, bridge_state, name
        self.publish, self.quoted, self.log, self.refresh = publish, quoted, log, refresh

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
        return str(self.state().get("wanted") or "") or None

    def seen(self, offered, current):
        """Remember what the runtime offers: `offered` is (id, name) pairs, `current` the id in use."""
        seen = {"available": [{"id": str(mid)[:200], "name": str(name or mid)[:80]} for mid, name in offered if mid],
                "current": str(current or "")[:200]}
        if {k: self.state().get(k) for k in seen} != seen:
            self.save(**seen)

    def card(self, state: dict, picked: str = "", ran: "str | None" = None, same: bool = False):
        """(body, card): one reply button per model offered. After a pick (`picked`) the card has no
        buttons: it says the switch is coming, then, with `ran` (the model the next turn ran on), that
        it happened or that it did not; with `same`, that the pick is the model already in use."""
        agent_card, _ = connector_cards._core()
        names = {m["id"]: m["name"] for m in state.get("available") or []}
        now = state.get("current") or ""
        if picked:
            name = names.get(picked, picked)
            if same:
                return agent_card.build(CARD, "done", title="Model", body="Model: already %s" % name,
                                        text="I'm already using %s. Nothing changed." % name)
            if ran is None:
                return agent_card.build(CARD, "info", title="Model", body="Model: switching to %s" % name,
                                        text="Switching to %s on your next message. I'll confirm here once it has "
                                             "switched." % name)
            if ran == picked:
                return agent_card.build(CARD, "done", title="Model", body="Model: now %s" % name,
                                        status="Switched", text="Switched. I'm now using %s." % name)
            return agent_card.build(CARD, "error", title="Model", body="Model: could not switch to %s" % name,
                                    text="I could not switch to %s. I'm still using %s. Send /model to try again."
                                         % (name, names.get(ran, ran or "my default model")))
        buttons = [agent_card.button(name, "reply", reply="Use %s" % name,
                                     command=agent_card.reply("model", "agent", mid))
                   for mid, name in list(names.items())[:BUTTONS]]
        more = list(names.values())[BUTTONS:]
        return agent_card.build(CARD, "pending", title="Model", body="Pick a model for %s" % self.name,
                                text="Now using %s.%s" % (names.get(now, now or "the default model"),
                                                          " Also: %s (send /model and the name)." % ", ".join(more)
                                                          if more else ""), buttons=buttons)

    def usable(self) -> bool:
        """Whether a card can reach the room: the agent's own bridge keeps a state dir (made at install).
        Without one (a bridge run elsewhere, a bare connector) nothing would post what is queued."""
        return bool(self.bridge_state) and os.path.isdir(self.bridge_state)

    def post(self, room, built) -> bool:
        if not self.usable():
            return False
        try:
            connector_cards.queue(self.bridge_state, str(room or ""), *built)
            return True
        except (OSError, ValueError) as e:
            self.log("model card not queued:", "%s: %s" % (type(e).__name__, e))
            return False

    def card_task(self, path, result_file) -> str:
        """What a task that was a card matter came to (a tap, or the owner's /model in their DM), else
        '': a tap never reaches the model, and a tap this agent cannot match gets the fixed line."""
        agent_card, _ = connector_cards._core()
        # The card rules read the task as the core does (the trusted header parse), not the connector's.
        with open(path, encoding="utf-8") as f:
            sent, said = agent_card.reply_words(f.read())
        verdict = connector_cards.click(sent, said, self.root)
        if verdict.verdict == agent_card.COMMAND and verdict.command.kind == "model":
            return self.pick(verdict.command.verb, sent, result_file)
        if verdict.verdict in (agent_card.COMMAND, agent_card.FORGED):
            self.publish(result_file, self.quoted(verdict.say) + "\n")
            return "card-refused"
        ask, _, named = said.strip().partition(" ")
        if ask.lower() != ASK or agent_card.owner_may_act(sent) is not None or not self.usable():
            return ""
        if not named.strip():
            return self.offer(sent, result_file)
        # "/model <name>": the way to a model the card has no button for.
        if self.refresh:
            self.refresh()
        wanted = named.strip().lower()
        match = [m["id"] for m in self.state().get("available") or [] if wanted in (m["id"].lower(), m["name"].lower())]
        return self.pick(match[0] if match else "", sent, result_file)

    def pick(self, picked, sent, result_file) -> str:
        """The owner chose `picked` (a tap or "/model <name>"): the next turn runs on it, and a card says so."""
        state, room = self.state(), str(sent.get("channel_id") or "")
        if picked not in [m["id"] for m in state.get("available") or []]:
            self.publish(result_file, self.quoted("That model is not one I can use now. Send /model for the "
                                                  "current list.") + "\n")
            return "model-unknown"
        same = picked == str(state.get("wanted") or state.get("current") or "")
        if not same:
            self.save(wanted=picked, asked_in=room, told="")
        # A pick of the model in use leaves nothing for the next turn to "confirm".
        card = self.card(state, picked=picked, same=same)
        self.publish(result_file, ANSWERED if self.post(room, card) else self.quoted(card[1]["text"]) + "\n")
        return "model-same" if same else "model-picked"

    def offer(self, sent, result_file) -> str:
        """Answer the owner's ask with the model card (or the line saying why there is none)."""
        if self.refresh:
            self.refresh()
        state = self.state()
        if not state.get("available"):
            self.publish(result_file, self.quoted(UNKNOWN) + "\n")
        elif self.post(sent.get("channel_id"), self.card(state)):
            self.publish(result_file, ANSWERED)
        else:
            self.publish(result_file, self.quoted("I could not post the model card. Try again in a moment.") + "\n")
        return "model-card"

    def asked(self, tier, path, status, text, result_file) -> str:
        """The runtime answered the owner with the mark alone: show the card in their DM, or say where to
        ask. '' for any other answer, and for any sender but the owner, whose mark is then ordinary text."""
        if status != "completed" or (text or "").strip() != MARK or tier != "owner":
            return ""
        agent_card, _ = connector_cards._core()
        with open(path, encoding="utf-8") as f:
            sent, _ = agent_card.reply_words(f.read())
        if agent_card.owner_may_act(sent) is None:
            return self.offer(sent, result_file)
        self.publish(result_file, self.quoted(IN_DM) + "\n")
        return "model-card-elsewhere"

    def tell(self, live, ran) -> str:
        """Once per pick, after the first owner turn that reached the runtime (`live`): say in the DM the
        pick was made in whether the switch happened (`ran` is the model that turn ran on). A switch the
        runtime refused is dropped, so it is not retried on every turn; '' when there was nothing to tell."""
        state = self.state()
        wanted = str(state.get("wanted") or "")
        if not wanted or state.get("told") == wanted or not live:
            return ""
        switched = ran == wanted
        self.post(state.get("asked_in"), self.card(state, picked=wanted, ran=ran))
        self.save(told=wanted, **({} if switched else {"wanted": ""}))
        return "switched" if switched else "not-switched"
