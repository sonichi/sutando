"""Which of a connector's cards a task belongs to.

A connector may offer several cards (the model, the account). Each is asked for the same three ways: a
tap on a card this agent posted, a slash command in the owner's DM, or the runtime answering the owner
with the card's mark alone; a card may also claim the owner's own words (`asks`). `Desk` reads the task once, as the core does, and hands it to the card it
names; a tap no card claims gets the fixed line, and never reaches the model.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import connector_cards

ANSWERED = "[no-send] the card answers\n"
# A switch can never be made in a room with other people in it, and no card that switches is shown
# there: anyone asking for one in a room is told so, in their language, and the model never sees it.
IN_ROOM = "Switching the account or model isn't available in a room. Please ask me in our DM."
IN_ROOM_ZH = "房间里不能切换账号或模型，请到私聊里找我。"
_CJK = re.compile(r"[\u3400-\u9fff]")


class Desk:
    """`cards` each carry KIND (the tap's kind), CARD_ID (the id of the card with its buttons), ASK (the
    slash command), MARK, HINT and IN_DM, and answer `tapped(verb, sent, result_file)`,
    `asked_for(words, sent, result_file)`, `offer(sent, result_file)`, `expired(sent, result_file)`
    and `usable()`."""

    def __init__(self, root, publish, quoted, cards):
        self.root, self.publish, self.quoted, self.cards = root, publish, quoted, list(cards)

    def task(self, path, result_file) -> str:
        """What a task that was a card matter came to (a tap, or the owner's slash command in their DM),
        else ''."""
        agent_card, issued_cards = connector_cards._core()
        # The card rules read the task as the core does (the trusted header parse), not the connector's.
        with open(path, encoding="utf-8") as f:
            sent, said = agent_card.reply_words(f.read())
        verdict = connector_cards.click(sent, said, self.root)
        if verdict.verdict == agent_card.COMMAND:
            for card in self.cards:
                if card.KIND == verdict.command.kind:
                    return self.drawn(card, result_file, lambda: card.tapped(verdict.command.verb, sent, result_file))
        if verdict.verdict == agent_card.FORGED and agent_card.owner_may_act(sent) is None:
            # The owner tapped a card this agent no longer keeps (a newer one replaced it, it is past its
            # time, or an earlier install posted it): its kind says so and shows the current one.
            tapped = (agent_card.card_click(sent) or {}).get("card_id")
            if not issued_cards.issued(Path(self.root), tapped_event(sent), str(sent.get("channel_id") or "")):
                # Not a card this agent is known to have posted: the fresh card is a new message, never an edit.
                sent = dict(sent, reply_to_event="")
            for card in self.cards:
                if tapped == card.CARD_ID:
                    return self.drawn(card, result_file, lambda: card.expired(sent, result_file))
        if verdict.verdict in (agent_card.COMMAND, agent_card.FORGED):
            self.publish(result_file, self.quoted(verdict.say) + "\n")
            return "card-refused"
        ask, _, words = said.strip().partition(" ")
        if in_room(sent):
            return self.in_room(ask, said, result_file)
        if agent_card.owner_may_act(sent) is not None:
            return ""
        for card in self.cards:
            if ask.lower() == card.ASK and card.usable():
                return self.drawn(card, result_file, lambda: card.asked_for(words.strip(), sent, result_file))
        # The owner's own words naming one choice ("switch to haiku"): picked as a tap would be, no menu.
        for card in self.cards:
            named = getattr(card, "named_in", None)
            choice = named(said.strip()) if named and card.usable() else ""
            if choice:
                return self.drawn(card, result_file, lambda: card.asked_for(choice, sent, result_file))
        # A card may also name the owner's own words that are its matter (`asks`), read here and never by
        # the model; one message may ask for several ("which account and model"), each shown once.
        asking = {}
        for card in self.cards:
            if getattr(card, "asks", None) and card.KIND not in asking and card.asks(said) and card.usable():
                asking[card.KIND] = card
        if len(asking) == 1:
            card = next(iter(asking.values()))
            return self.drawn(card, result_file, lambda: card.offer(sent, result_file))
        return self.offered_all(list(asking.values()), sent, result_file) if asking else ""

    def in_room(self, ask, said, result_file) -> str:
        """A room is no place for a switch: a slash command, a named pick or a card's own question there,
        from anyone, gets the one line saying to ask in the DM, and no card. '' for anything else."""
        said = said.strip()
        for card in self.cards:
            named = getattr(card, "named_in", None)
            asks = getattr(card, "asks", None)
            if ask.lower() == card.ASK or (named and named(said)) or (asks and asks(said)):
                self.publish(result_file, self.quoted(IN_ROOM_ZH if _CJK.search(said) else IN_ROOM) + "\n")
                return "card-in-room"
        return ""

    def offered_all(self, cards, sent, result_file) -> str:
        """Offer each card in `cards` and publish one answer for them all: the words of any card that could
        not be shown, else the first card's no-send line. A task's result is written once."""
        bodies, outcomes = [], []
        for card in cards:
            got, kept = [], card.__dict__.get("publish")
            card.publish = lambda path, body, got=got: got.append(body)
            try:
                outcomes.append(self.drawn(card, result_file, lambda: card.offer(sent, result_file), card.publish))
            finally:
                if kept is None:
                    del card.publish
                else:
                    card.publish = kept
            bodies.append(got[-1] if got else "")
        words = [b for b in bodies if b and not b.startswith("[no-send]")]
        self.publish(result_file, "".join(words) if words else next((b for b in bodies if b), ANSWERED))
        return "+".join(outcomes)

    def drawn(self, card, result_file, act, publish=None) -> str:
        """`act()`, with a card that cannot be drawn (a name or a link the card rules refuse) answered in
        words once: nothing raises out of a card matter, so the task is never retried over it."""
        try:
            return act()
        except ValueError:
            (publish or self.publish)(result_file, self.quoted(
                "I could not show the %s card. Tell me what you'd like in your own words." % card.KIND) + "\n")
            return "%s-card-failed" % card.KIND

    def marked(self, tier, path, status, text, result_file, task_text=None) -> str:
        """The runtime answered the owner with a card's mark alone: show that card in their DM, or say
        where to ask. '' for any other answer, and for any sender but the owner, whose mark is ordinary text.
        With `task_text` (the task as read before the turn, `read_task`) the file at `path` is not opened:
        by the end of the turn the bridge may have archived it."""
        card = next((c for c in self.cards if (text or "").strip() == c.MARK), None)
        if status != "completed" or tier != "owner" or card is None:
            return ""
        agent_card, _ = connector_cards._core()
        sent, _ = agent_card.reply_words(read_task(path) if task_text is None else task_text)
        if agent_card.owner_may_act(sent) is None:
            return self.drawn(card, result_file, lambda: card.offer(sent, result_file))
        self.publish(result_file, self.quoted(card.IN_DM) + "\n")
        return "%s-card-elsewhere" % card.KIND

    def hint(self) -> str:
        """What an owner turn is told about the cards that can be shown, '' when none can."""
        return "\n\n".join(card.HINT for card in self.cards if card.usable())


def in_room(sent) -> bool:
    """Whether the task came in a room (any channel kind but the DM); a task that names none is not."""
    kind = str((sent or {}).get("channel_kind") or "").strip()
    return bool(kind) and kind != "dm"


def read_task(path) -> str:
    """The task file's text, read before the turn for `Desk.marked`; '' when it is already gone."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def led(publish, pending, quoted):
    """`publish`, with every answer led by `pending()`: what another card settled this turn and could
    only say in words. A card's answer must not swallow them, whichever card answers; an answer that
    was only "[no-send]" gives way to the words."""
    def write(result_file, body):
        words = pending()
        if words:
            body = quoted(words) + "\n" + ("" if body.startswith("[no-send]") else body)
        return publish(result_file, body)
    return write


def tapped_event(sent) -> str:
    """The event id of the card a tap answers ('' for none): its answer is shown in place of that card."""
    event = str((sent or {}).get("reply_to_event") or "")
    return event if event.startswith("$") and not any(c.isspace() for c in event) else ""


def usable(bridge_state) -> bool:
    """Whether a card can reach the room: the agent's own bridge keeps a state dir (made at install).
    Without one (a bridge run elsewhere, a bare connector) nothing would post what is queued."""
    return bool(bridge_state) and os.path.isdir(bridge_state)


def post(bridge_state, room, built, log, key: str = "", at_ns: int = 0, edit: str = "") -> bool:
    """Queue a built card for the bridge to post in `room`; False when it cannot be. With `key` the
    call may be repeated after a restart without a second card (connector_cards.queue). With `edit` (the
    event id of the card being answered) it is shown in place of that card, not as a second one."""
    if not usable(bridge_state):
        return False
    try:
        connector_cards.queue(bridge_state, str(room or ""), *built, key=key, at_ns=at_ns,
                              edit=edit if str(edit or "").startswith("$") else "")
        return True
    except (OSError, ValueError) as e:
        log("card not queued:", "%s: %s" % (type(e).__name__, e))
        return False
