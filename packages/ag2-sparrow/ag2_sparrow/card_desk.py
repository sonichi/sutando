"""Which of a connector's cards a task belongs to.

A connector may offer several cards (the model, the account). Each is asked for the same three ways: a
tap on a card this agent posted, a slash command in the owner's DM, or the runtime answering the owner
with the card's mark alone; a card may also claim the owner's own words (`asks`). `Desk` reads the task once, as the core does, and hands it to the card it
names; a tap no card claims gets the fixed line, and never reaches the model.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import connector_cards

ANSWERED = "[no-send] the card answers\n"


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
        if agent_card.owner_may_act(sent) is not None:
            return ""
        for card in self.cards:
            if ask.lower() == card.ASK and card.usable():
                return self.drawn(card, result_file, lambda: card.asked_for(words.strip(), sent, result_file))
        for card in self.cards:
            # A card may also name the owner's own words that are its matter (`asks`), read here and
            # never by the model: the account card's, for what the runtime cannot see of itself.
            if getattr(card, "asks", None) and card.asks(said) and card.usable():
                return self.drawn(card, result_file, lambda: card.offer(sent, result_file))
        return ""

    def drawn(self, card, result_file, act) -> str:
        """`act()`, with a card that cannot be drawn (a name or a link the card rules refuse) answered in
        words once: nothing raises out of a card matter, so the task is never retried over it."""
        try:
            return act()
        except ValueError:
            self.publish(result_file, self.quoted("I could not show the %s card. Tell me what you'd like in your "
                                                  "own words." % card.KIND) + "\n")
            return "%s-card-failed" % card.KIND

    def marked(self, tier, path, status, text, result_file) -> str:
        """The runtime answered the owner with a card's mark alone: show that card in their DM, or say
        where to ask. '' for any other answer, and for any sender but the owner, whose mark is ordinary text."""
        card = next((c for c in self.cards if (text or "").strip() == c.MARK), None)
        if status != "completed" or tier != "owner" or card is None:
            return ""
        agent_card, _ = connector_cards._core()
        with open(path, encoding="utf-8") as f:
            sent, _ = agent_card.reply_words(f.read())
        if agent_card.owner_may_act(sent) is None:
            return self.drawn(card, result_file, lambda: card.offer(sent, result_file))
        self.publish(result_file, self.quoted(card.IN_DM) + "\n")
        return "%s-card-elsewhere" % card.KIND

    def hint(self) -> str:
        """What an owner turn is told about the cards that can be shown, '' when none can."""
        return "\n\n".join(card.HINT for card in self.cards if card.usable())


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
