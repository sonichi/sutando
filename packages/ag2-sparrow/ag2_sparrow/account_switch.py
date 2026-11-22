"""The account card of a connector: the owner asks, in their DM with the agent, which account it is
signed in with, taps "Switch account", signs in in the browser, and is told in that DM how it ended.

The connector supplies the runtime's part: `read()` names the account in use ('' when signed out),
`begin()` starts a sign-in and gives its id and page, `cancel(id)` gives one up, `opener(url)` shows the
page on this computer. Nothing here sees a credential; what is kept, in `state/account.json`, is the
pending sign-in's id, the room it was asked in and when.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse

from . import card_desk, connector_cards

FILE, CARD = "account.json", "account-switch"
WAIT_S = 600.0       # a sign-in the owner has not finished by then is given up
MARK = "[account-card]"
HINT = ("If the person asks to switch, change or sign in to another account for you, or which account you "
        "use, reply with exactly %s and nothing else: they are then shown a card for it. Do not start a "
        "sign-in yourself." % MARK)
IN_DM = "I can switch accounts from our direct message. Ask me there."
ANSWERED = "[no-send] the account card answers\n"


def page_ok(url) -> bool:
    """Whether `url` is a plain https address with a host: the only thing handed to this computer's
    opener, where anything else (a leading '-', a file or app scheme) could be read as something to run."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return False
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname) and "@" not in parts.netloc


def masked(email: str) -> str:
    """An address shortened to what tells two accounts apart: its first letter and its domain."""
    name, at, domain = (email or "").partition("@")
    return "%s…@%s" % (name[:1], domain) if name and at and domain else ""


class AccountSwitch:
    KIND, ASK, MARK, HINT, IN_DM = "account", "/account", MARK, HINT, IN_DM

    def __init__(self, state_dir, bridge_state, name, publish, quoted, log, read, begin, cancel, opener):
        self.state_dir, self.bridge_state, self.name = state_dir, bridge_state, name
        self.publish, self.quoted, self.log = publish, quoted, log
        self.read, self.begin, self.cancel, self.opener = read, begin, cancel, opener

    def pending(self) -> dict:
        try:
            with open(os.path.join(self.state_dir, FILE), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) and state.get("id") else {}

    def keep(self, state: dict):
        path = os.path.join(self.state_dir, FILE)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
        os.replace(path + ".tmp", path)

    def usable(self) -> bool:
        return card_desk.usable(self.bridge_state)

    def post(self, room, built) -> bool:
        return card_desk.post(self.bridge_state, room, built, self.log)

    def using(self) -> str:
        """The account in use, in words; a runtime that cannot say reads as unknown, never as signed out."""
        try:
            label = self.read()
        except Exception as e:  # noqa: BLE001 - the card still says what it can
            self.log("account not read:", "%s: %s" % (type(e).__name__, str(e)[:200]))
            return "I could not read which account I'm signed in with."
        return "I'm signed in as %s." % label if label else "I'm not signed in."

    def card(self, state: str, text: str, url: str = "", ask: bool = False):
        agent_card, _ = connector_cards._core()
        buttons = []
        if ask:
            buttons.append(agent_card.button("Switch account", "reply", reply="Switch account",
                                             command=agent_card.reply("account", "agent", "switch")))
        if url:
            buttons.append(agent_card.button("Open sign-in page", "url", url=url))
        return agent_card.build(CARD, state, title="Account", body="Account of %s" % self.name, text=text,
                                **({"buttons": buttons} if buttons else {}))

    def say(self, room, built, result_file=None) -> None:
        """Post the card; where it cannot be posted, a task's result carries its words instead."""
        posted = self.post(room, built)
        if result_file:
            self.publish(result_file, ANSWERED if posted else self.quoted(built[1]["text"]) + "\n")

    def offer(self, sent, result_file) -> str:
        self.say(sent.get("channel_id"), self.card("pending", self.using(), ask=True), result_file)
        return "account-card"

    def asked_for(self, words, sent, result_file) -> str:
        return self.offer(sent, result_file)

    def tapped(self, verb, sent, result_file) -> str:
        """The owner tapped "Switch account": start a sign-in, show its page here, and say so. The page
        is checked, and its card built, before anything is opened or kept: a page that is not a plain
        https address is given up at once, so a retry cannot open a browser again and again."""
        room = str(sent.get("channel_id") or "")
        if verb != "switch":
            self.publish(result_file, self.quoted("I couldn't match that button. Send /account for a new card.") + "\n")
            return "account-unknown"
        waiting = self.pending()
        if waiting:
            self.give_up(waiting)
        before = self.using()       # read now: the sign-in may change it before the card is built
        try:
            started = self.begin()
            login, url = str(started["id"]), str(started["url"])
        except Exception as e:  # noqa: BLE001 - whatever the runtime refused with, the owner is told
            self.log("sign-in not started:", "%s: %s" % (type(e).__name__, str(e)[:200]))
            self.say(room, self.card("error", "I could not start a sign-in. %s" % before), result_file)
            return "account-not-started"
        try:
            if not page_ok(url):
                raise ValueError("the sign-in page is not an https address")
            link = self.card("info", "Open the sign-in page on the computer I run on. Finish signing in there; I'll "
                                     "confirm here. Until then: %s" % before, url=url)
        except ValueError as e:
            self.log("sign-in page refused:", str(e)[:200])
            self.give_up({"id": login})
            self.say(room, self.card("error", "I could not start a sign-in: the page I was given for it is not one "
                                              "I can show. %s" % before), result_file)
            return "account-not-started"
        self.keep({"id": login, "room": room, "at": time.time()})
        if self.opener(url):
            link = self.card("info", "I opened the sign-in page in the browser on the computer I run on. Finish "
                                     "signing in there; I'll confirm here. Until then: %s" % before, url=url)
        self.say(room, link, result_file)
        return "account-started"

    def give_up(self, waiting) -> None:
        try:
            self.cancel(waiting["id"])
        except Exception as e:  # noqa: BLE001 - a sign-in already over cannot be cancelled
            self.log("sign-in not cancelled:", "%s: %s" % (type(e).__name__, str(e)[:200]))
        self.keep({})

    def last_word(self, waiting, state, text) -> bool:
        """Post a sign-in's final card. Until it is posted the sign-in stays on record with that card's
        words, so a card that could not be queued is not lost: `expire` posts it later."""
        if self.post(waiting.get("room"), self.card(state, text)):
            self.keep({})
            return True
        self.keep(dict(waiting, final={"state": state, "text": text}))
        return False

    def finished(self, login, success, error="") -> str:
        """The runtime reported a sign-in over: tell the owner, in the DM it was asked in, how it ended
        and which account is in use now. '' for a sign-in this card did not start."""
        waiting = self.pending()
        if not waiting or waiting.get("final") or (login and str(login) != waiting["id"]):
            return ""
        if success:
            self.last_word(waiting, "done", "Signed in. %s" % self.using())
            return "account-switched"
        why = " (%s)" % str(error)[:120] if error else ""
        self.last_word(waiting, "error", "The sign-in did not finish%s. %s" % (why, self.using()))
        return "account-not-switched"

    def expire(self, now=None) -> str:
        """Give up a sign-in nobody finished within WAIT_S, and say so; and post a final card that could
        not be posted when its sign-in ended."""
        waiting = self.pending()
        final = waiting.get("final") if waiting else None
        if isinstance(final, dict):
            return "account-told" if self.last_word(waiting, str(final.get("state")), str(final.get("text"))) else ""
        if not waiting or (now if now is not None else time.time()) - float(waiting.get("at") or 0) < WAIT_S:
            return ""
        try:
            self.cancel(waiting["id"])
        except Exception as e:  # noqa: BLE001 - a sign-in already over cannot be cancelled
            self.log("sign-in not cancelled:", "%s: %s" % (type(e).__name__, str(e)[:200]))
        self.last_word(waiting, "error", "The sign-in was not finished in %d minutes, so I stopped waiting. %s Send "
                                         "/account to try again." % (WAIT_S // 60, self.using()))
        return "account-timed-out"
