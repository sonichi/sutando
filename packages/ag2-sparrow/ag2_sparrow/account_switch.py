"""The account card of a connector: the owner asks, in their DM with the agent, which account it is
signed in with, taps "Switch account", signs in in the browser, and is told in that DM how it ended.
One card, updated in place: what a tap comes to (the sign-in page, then how the sign-in ended) is shown
in place of the card that was tapped (connector_cards' edit), never as a second card. A card asked for
in words (/account, the card's mark) is a new card; a sign-in that ends with no card waiting for it
(the desktop app switched the account itself) posts nothing.

The connector supplies the runtime's part: `read()` names the account in use ('' when signed out),
`begin()` starts a sign-in and gives its id and page, `cancel(id)` gives one up, `opener(url)` shows the
page on this computer. Nothing here sees a credential; what is kept, in `state/account.json`, is the
pending sign-in's id, the room it was asked in and when.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import uuid

from . import card_desk, connector_cards

FILE, CARD = "account.json", "account-switch"
# A card with no "Switch account" button is not the account card: recorded under its id it would retire
# that card (one live card per id and room) while the owner still sees its button.
NOTE = "account-switch-status"
EXPIRED = "That card has expired, so nothing changed. Here's a new one."
EXPIRED_NO_CARD = "That card has expired, so nothing changed. Send /account for a new one."
WAIT_S = 600.0       # a sign-in the owner has not finished by then is given up
OWED_KEEP = 5        # final cards kept while they cannot be queued
MARK = "[account-card]"
HINT = ("If the person asks to switch, change or sign in to another account for you, or which account you "
        "use, reply with exactly %s and nothing else: they are then shown a card for it. Do not start a "
        "sign-in yourself." % MARK)
IN_DM = "I can switch accounts from our direct message. Ask me there."
# The owner's own words that ask for the card, as a whole short message: to switch the account, or which
# one the agent is on. The runtime cannot see its own sign-in, so these never go to the model.
ASK_MAX = 80
_YOU = r"(?:you|u|agy|antigravity|codex|claude(?: code)?|this agent|the agent)"
_IN = r"(?:signed|logged) ?in"
_END = r"\s*[。.!！?？]*\s*$"
ASK_RES = tuple(re.compile(r"^\s*(?:hey|hi|ok|okay|so|and|请问|那)?[,，\s]*" + body + _END, re.I) for body in (
    r"/account",
    # switch it
    r"(?:请|帮我|我要|我想)?(?:切换|换|更换)(?:一下|个)?账[号户]",
    r"(?:please )?(?:switch|change)(?: the| your| my)? accounts?(?: please)?",
    # which one: "which account", "what account are you on", "what's your account", "which account is this"
    r"(?:which|what)(?:'s| is)?(?: the| your)? (?:account|e-?mail)"
    r"(?: (?:are|r|is) " + _YOU + r"(?: (?:using|on|in|" + _IN + r"(?: (?:with|to|as))?))?"
    r"| is (?:this|that|it)| (?:do|does) " + _YOU + r" use| " + _YOU + r"(?:'re| are| is) (?:using|on|" + _IN + r"(?: (?:with|to|as))?))?",
    r"who (?:are|r|is) " + _YOU + r" " + _IN + r" as",
    r"(?:can|could|do) " + _YOU + r" (?:see|tell me|show(?: me)?) (?:your|the|which|what|" + _YOU + r"(?:'s)?) ?account"
    r"(?: (?:you(?:'re| are) (?:on|using)|it is|is it))?",
    r"(?:" + _YOU + r"(?:'s)?|your) account[,:;]? ?(?:can you see it|which one(?: is it)?|what is it|which is it)",
    # 哪个账号 / 你用的是什么账号 / 账号是什么 / 能看到你的账号吗
    r"(?:你|您)?(?:现在|目前|当前)?(?:用的|在用的?|使用的|登录的|登的)?是?(?:哪个|哪一个|什么|啥)账[号户](?:啊|呢|呀)?",
    r"(?:你的?|您的?)?(?:现在的?|目前的?|当前的?)?(?:登录的?)?账[号户]是(?:什么|哪个|哪一个|啥|谁)(?:啊|呢|呀)?",
    r"(?:你|您)?(?:能|可以|能不能)?看(?:到|见|得到|得见)(?:你的?|自己的?)?账[号户]吗",
))


def asks_for_card(text) -> bool:
    """Whether a message, whole and short, asks to switch the account or which one the agent is on. A
    longer message, or one about some other account ("create an account on GitHub"), is the model's."""
    said = text or ""
    return len(said) <= ASK_MAX and any(r.match(said) for r in ASK_RES)
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
    CARD_ID = CARD
    asks = staticmethod(asks_for_card)

    def __init__(self, state_dir, bridge_state, name, publish, quoted, log, read, begin, cancel, opener):
        self.state_dir, self.bridge_state, self.name = state_dir, bridge_state, name
        self.publish, self.quoted, self.log = publish, quoted, log
        self.read, self.begin, self.cancel, self.opener = read, begin, cancel, opener

    def record(self) -> dict:
        try:
            with open(os.path.join(self.state_dir, FILE), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def pending(self) -> dict:
        """The sign-in in progress ({} when there is none), apart from the results still to be told."""
        state = self.record()
        return {k: v for k, v in state.items() if k != "owed"} if state.get("id") else {}

    def owed(self) -> list:
        """The final cards of sign-ins that ended while their card could not be queued, oldest first."""
        return [o for o in self.record().get("owed") or [] if isinstance(o, dict)]

    def keep(self, attempt: dict, owed=None):
        """Record the sign-in in progress (`{}` for none); the results still to be told are kept as they
        are unless `owed` is given."""
        owed = self.owed() if owed is None else owed
        state = dict(attempt, **({"owed": owed} if owed else {}))
        path = os.path.join(self.state_dir, FILE)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
        os.replace(path + ".tmp", path)

    def usable(self) -> bool:
        return card_desk.usable(self.bridge_state)

    def post(self, room, built, key: str = "", at_ns: int = 0, edit: str = "") -> bool:
        return card_desk.post(self.bridge_state, room, built, self.log, key, at_ns, edit)

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
        return agent_card.build(CARD if ask else NOTE, state, title="Account", body="Account of %s" % self.name, text=text,
                                **({"buttons": buttons} if buttons else {}))

    def say(self, room, built, result_file=None, edit: str = "") -> None:
        """Post the card (in place of the card `edit` names, when given); where it cannot be posted, a
        task's result carries its words instead."""
        posted = self.post(room, built, edit=edit)
        if result_file:
            self.publish(result_file, ANSWERED if posted else self.quoted(built[1]["text"]) + "\n")

    def offer(self, sent, result_file) -> str:
        self.say(sent.get("channel_id"), self.card("pending", self.using(), ask=True), result_file)
        return "account-card"

    def asked_for(self, words, sent, result_file) -> str:
        return self.offer(sent, result_file)

    def expired(self, sent, result_file) -> str:
        """A tap on an account card that no longer acts: a fresh card in its place when one can be shown,
        else the words. No sign-in is started by it."""
        fresh = self.post(sent.get("channel_id"), self.card("pending", self.using(), ask=True),
                          edit=card_desk.tapped_event(sent))
        self.publish(result_file, ANSWERED if fresh else self.quoted(EXPIRED_NO_CARD) + "\n")
        return "account-card-expired"

    def tapped(self, verb, sent, result_file) -> str:
        """The owner tapped "Switch account": start a sign-in, show its page here, and say so. The page
        is checked, and its card built, before anything is opened or kept: a page that is not a plain
        https address is given up at once, so a retry cannot open a browser again and again."""
        room, tapped = str(sent.get("channel_id") or ""), card_desk.tapped_event(sent)
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
            self.say(room, self.card("error", "I could not start a sign-in. %s" % before), result_file, tapped)
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
                                              "I can show. %s" % before), result_file, tapped)
            return "account-not-started"
        self.keep(dict({"id": login, "room": room, "at": time.time()}, **({"card": tapped} if tapped else {})))
        if self.opener(url):
            link = self.card("info", "I opened the sign-in page in the browser on the computer I run on. Finish "
                                     "signing in there; I'll confirm here. Until then: %s" % before, url=url)
        self.say(room, link, result_file, tapped)
        return "account-started"

    def give_up(self, waiting) -> None:
        try:
            self.cancel(waiting["id"])
        except Exception as e:  # noqa: BLE001 - a sign-in already over cannot be cancelled
            self.log("sign-in not cancelled:", "%s: %s" % (type(e).__name__, str(e)[:200]))
        self.keep({})

    def last_word(self, waiting, state, text) -> bool:
        """A sign-in is over: it is no longer the one in progress, and its final card joins the results
        to be told, which are then posted oldest first. A new tap never removes one of them."""
        owed = self.keyed(self.owed())
        # Later than every result already owed: the bridge posts in the order of this moment.
        at = max([time.time_ns()] + [int(o.get("at") or 0) + 1 for o in owed])
        # Shown in place of the card that was tapped, when there was one.
        self.keep({}, owed + [dict({"room": waiting.get("room"), "state": state, "text": text,
                                    "key": uuid.uuid4().hex, "at": at},
                                   **({"edit": waiting["card"]} if waiting.get("card") else {}))])
        return not self.tell()

    @staticmethod
    def keyed(owed: list) -> list:
        """`owed` with a key, and a moment that keeps the saved order, on each result saved before
        results had them."""
        for o in owed:
            if not o.get("key"):
                o.update(key=uuid.uuid4().hex, at=max([time.time_ns()] + [int(x.get("at") or 0) + 1 for x in owed]))
        return owed

    def tell(self) -> list:
        """Post the results still to be told, oldest first, stopping at the first that cannot be queued;
        what is left. Each is queued under the key saved with it, so one queued just before a stop,
        and still on the saved list after the restart, is not a second card. Only what is still
        undeliverable afterwards is capped, at the newest OWED_KEEP."""
        owed = self.owed()
        # A result saved before results had keys gets one now, and is saved with it before it is queued.
        if any(not o.get("key") for o in owed):
            self.keep(self.pending(), self.keyed(owed))
        while owed and self.post(owed[0].get("room"), self.card(str(owed[0].get("state")), str(owed[0].get("text"))),
                                 str(owed[0].get("key") or ""), int(owed[0].get("at") or 0), str(owed[0].get("edit") or "")):
            owed.pop(0)
            self.keep(self.pending(), owed)
        if len(owed) > OWED_KEEP:
            owed = owed[-OWED_KEEP:]
            self.keep(self.pending(), owed)
        return owed

    def finished(self, login, success, error="") -> str:
        """The runtime reported a sign-in over: tell the owner, in the DM it was asked in, how it ended
        and which account is in use now. '' for any report that does not name the sign-in in progress:
        one without an id could be another sign-in's, and must not end this one."""
        waiting = self.pending()
        if not waiting or not login or str(login) != waiting["id"]:
            return ""
        if success:
            self.last_word(waiting, "done", "Signed in. %s" % self.using())
            return "account-switched"
        why = " (%s)" % str(error)[:120] if error else ""
        self.last_word(waiting, "error", "The sign-in did not finish%s. %s" % (why, self.using()))
        return "account-not-switched"

    def expire(self, now=None) -> str:
        """Post any result still to be told; then give up a sign-in nobody finished within WAIT_S, and
        say so."""
        told = bool(self.owed()) and not self.tell()
        waiting = self.pending()
        if not waiting or (now if now is not None else time.time()) - float(waiting.get("at") or 0) < WAIT_S:
            return "account-told" if told else ""
        try:
            self.cancel(waiting["id"])
        except Exception as e:  # noqa: BLE001 - a sign-in already over cannot be cancelled
            self.log("sign-in not cancelled:", "%s: %s" % (type(e).__name__, str(e)[:200]))
        self.last_word(waiting, "error", "The sign-in was not finished in %d minutes, so I stopped waiting. %s Send "
                                         "/account to try again." % (WAIT_S // 60, self.using()))
        return "account-timed-out"
