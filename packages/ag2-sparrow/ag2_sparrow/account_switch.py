"""The account card of a connector: the owner asks, in their DM with the agent, which account it is
signed in with, taps "Switch account", signs in in the browser, and is told in that DM how it ended.
One card, updated in place: what a tap comes to (the sign-in page, then how the sign-in ended) is shown
in place of the card that was tapped (connector_cards' edit), never as a second card. A card asked for
in words (/account, the card's mark) is a new card; a sign-in that ends with no card waiting for it
(the desktop app switched the account itself) posts nothing.

The connector supplies the runtime's part: `read()` names the account in use ('' when signed out),
`begin()` starts a sign-in and gives its id and page, `cancel(id)` gives one up, `opener(url)` shows the
page on this computer. A connector may also set `remote` (its "Add account" and "Sign out", a sign-in the
owner finishes on another device), which then answers those taps. Nothing here sees a credential; what is kept, in `state/account.json`, is the
pending sign-in's id, the room it was asked in and when.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import uuid
from pathlib import Path

from . import card_ask, card_desk, connector_cards

FILE, CARD = "account.json", "account-switch"
# "Switch account" by the browser on this computer, where `switch_anywhere` makes Switch the device-code one.
HERE, HERE_LABEL = "here", "Sign in on this computer"
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
# The account card's words, English only. A client that knows the `account_controls` marker draws the
# agent's saved accounts and sign-in below them; one that does not shows the text and the button.
BELOW = "Switch this agent's account below."
CONTROLS = "account_controls"
# The owner's own words that ask for the card, as a whole short message: to switch the account, or which
# one the agent is on. The runtime cannot see its own sign-in, so these never go to the model.
ASK_MAX = card_ask.ASK_MAX
asks_for_card = card_ask.asks_for_account
ANSWERED = "[no-send] the account card answers\n"
# The account dropdown (owner 11-27: the phone works as the desktop's menu does): the saved accounts, the one in
# use checked, then "Add account…"; a saved account's tap verb is its 16-hex key.
MENU_ROW, ADD_ITEM, ADD_WORDS, CHECK = "Account", "Add account…", "Add account", "✓ "
NO_ACCOUNT = "Choose an account"
KEY_RE = re.compile(r"[0-9a-f]{16}")
UNMATCHED = "I couldn't match that button. Send /account for a new card."


def page_ok(url) -> bool:
    """Whether `url` is a plain https address with a host: the only thing handed to this computer's
    opener, where anything else (a leading '-', a file or app scheme) could be read as something to run."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return False
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname) and "@" not in parts.netloc


def elsewhere(name: str) -> str:
    """Where the account controls are, for a place that cannot draw them."""
    return "Open this on the computer that runs %s to switch account." % (name or "this agent")


def with_controls(built, runtime: str = "", agent: str = "", **flags):
    """`built` (body, card) marked as an account card: a client that knows the marker draws the agent's
    account controls in it, nothing typed there reaching the room; one that does not shows the text."""
    body, card = built
    marker = {k: v for k, v in dict(flags, runtime=runtime, agent=agent).items() if v}
    return body, dict(card, **{CONTROLS: marker})


def limited(built):
    """An account card that is a usage-limit notice: its marker says so (`limit`), so the client lists it
    with what the agent waits on, apart from an account menu the owner asked for. A card with no marker is
    returned as it is."""
    body, card = built
    marker = card.get(CONTROLS) if isinstance(card, dict) else None
    if not isinstance(marker, dict):
        return built
    return body, dict(card, **{CONTROLS: dict(marker, limit=True)})


def masked(email: str) -> str:
    """An address shortened to what tells two accounts apart: its first letter and its domain."""
    name, at, domain = (email or "").partition("@")
    return "%s…@%s" % (name[:1], domain) if name and at and domain else ""


def shown(email: str) -> str:
    """An address as the owner reads it on the account card in their DM with the agent: in full, never
    with a plan; "" when it is not an address."""
    name, at, domain = (email or "").strip().partition("@")
    return "%s@%s" % (name, domain) if name and at and domain and not any(c.isspace() for c in name + domain) else ""


def use_words(email: str) -> str:
    """What a pick of the saved account `email` sends as the owner's reply."""
    agent_card, _ = connector_cards._core()
    return ("Use " + email)[:agent_card.LIMITS["label"]]


def account_menu(saved: dict, current: str, add_verb: str = "add") -> list:
    """The card's groups for the account dropdown: one "Account" row whose menu lists `saved` (key -> address)
    by address, the one in use (`current`) checked and not tappable, then "Add account…" (`add_verb`). At most
    one item short of the menu's limit are listed, the one in use always among them."""
    agent_card, _ = connector_cards._core()
    cap, using = agent_card.LIMITS["label"], (current or "").strip().lower()
    ranked = sorted(saved.items(), key=lambda kv: (kv[1].lower() != using, kv[1].lower()))
    items = []
    for key, email in sorted(ranked[:agent_card.LIMITS["menu_items"] - 1], key=lambda kv: kv[1].lower()):
        here = bool(using) and email.lower() == using
        items.append(agent_card.button(((CHECK if here else "") + email)[:cap], "reply", reply=use_words(email),
                                       command=agent_card.reply("account", "agent", key),
                                       **({"disabled": True} if here else {})))
    items.append(agent_card.button(ADD_ITEM, "reply", reply=ADD_WORDS,
                                   command=agent_card.reply("account", "agent", add_verb)))
    menu = agent_card.button((current or NO_ACCOUNT)[:cap], "menu", items=items)
    return [{"rows": [agent_card.row(MENU_ROW, actions=[menu])]}]


def menu_items(card) -> list:
    """The account dropdown's items on `card` ([] for a card without one)."""
    for grp in (card or {}).get("groups") or []:
        for row in grp.get("rows") or []:
            for act in row.get("actions") or []:
                if row.get("title") == MENU_ROW and act.get("kind") == "menu":
                    return list(act.get("items") or [])
    return []


def offered_on(root, event: str, key: str) -> bool:
    """Whether the card at `event` is one this agent posted that offers saved account `key` (its issued record)."""
    agent_card, issued_cards = connector_cards._core()
    entry = issued_cards.live(Path(root), event) if event else None
    return bool(entry) and agent_card.reply("account", "agent", key) in entry["offers"].values()


# A login kept on this computer that no longer works, said apart from never having signed in.
LAPSED = ("My sign-in here no longer works: it was signed out, or the same account was used by another copy of it on "
          "this computer, which ends this one. Sign in again.")


class AccountSwitch:
    KIND, ASK, MARK, HINT, IN_DM = "account", "/account", MARK, HINT, IN_DM
    CARD_ID = CARD
    asks = staticmethod(asks_for_card)

    def __init__(self, state_dir, bridge_state, name, publish, quoted, log, read, begin, cancel, opener, runtime="",
                 lapsed=None, limit=None):
        # `remote`: a connector's sign-in the owner finishes on another device ("Add account", "Sign out"),
        # set by that connector after it makes the card; None offers neither.
        self.remote = None
        # `switch_anywhere`: "Switch account" starts `remote`'s sign-in too, never the browser on this computer.
        self.switch_anywhere = False
        # `saved()`: {key: address} of the accounts the desktop app saved for this agent that a pick can switch to;
        # set, the card is the dropdown (`account_menu`) and a key's tap goes to `pick(key, sent, result_file)`.
        self.saved = self.pick = None
        self.in_use = ""
        self.state_dir, self.bridge_state, self.name, self.runtime = state_dir, bridge_state, name, runtime
        # `lapsed()`: the runtime itself refused this login (unauthorized even after a restart), not merely read
        # no account; asked again on every card, so an earlier answer is never carried into a later one.
        self.lapsed = lapsed
        # `limit()`: the usage limit in force, in words ('' for none), so a card asked for meanwhile says it too.
        self.limit = limit
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
        # Owner-only: a result still to be told names the account's address.
        with os.fdopen(os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            os.chmod(path + ".tmp", 0o600)
            json.dump(state, f, sort_keys=True)
        os.replace(path + ".tmp", path)

    def usable(self) -> bool:
        return card_desk.usable(self.bridge_state)

    def post(self, room, built, key: str = "", at_ns: int = 0, edit: str = "") -> bool:
        return card_desk.post(self.bridge_state, room, built, self.log, key, at_ns, edit)

    def using(self) -> str:
        """The account in use, in words; a runtime that cannot say reads as unknown, never as signed out."""
        return self.status()[0]

    def status(self) -> tuple:
        """(the account in use in words, whether the runtime refused its login): read afresh for each card."""
        self.in_use = ""
        try:
            label = self.read()
        except Exception as e:  # noqa: BLE001 - the card still says what it can
            self.log("account not read:", "%s: %s" % (type(e).__name__, str(e)[:200]))
            return "I could not read which account I'm signed in with.", False
        self.in_use = label or ""
        if self.lapsed is not None and self.lapsed() is True:
            return LAPSED, True
        return ("I'm using %s." % label if label else "I'm not signed in."), False

    def menu_current(self) -> str:
        """The account in use as the dropdown names it (`status` read it); '' when none or unknown."""
        return self.in_use

    def menu_accounts(self, saved=None) -> dict:
        """The dropdown's saved accounts: `saved` when the caller read them already, else `saved()` now."""
        if saved is not None:
            return saved
        try:
            return dict(self.saved() or {})
        except Exception as e:  # noqa: BLE001 - the dropdown still offers "Add account…"
            self.log("saved accounts not read:", "%s: %s" % (type(e).__name__, str(e)[:200]))
            return {}

    def card(self, state: str, text: str, url: str = "", ask: bool = False, signed_out: bool = False, saved=None):
        """The card; asked for (`ask`) with `saved` set, the dropdown of `menu_accounts(saved)` in place of the
        "Switch account" and "Add account" buttons."""
        agent_card, _ = connector_cards._core()
        buttons, groups = [], None
        if ask and self.saved is not None:
            groups = account_menu(self.menu_accounts(saved), self.menu_current())
            if self.remote is not None and self.switch_anywhere:
                buttons.append(agent_card.button(HERE_LABEL, "reply", reply=HERE_LABEL,
                                                 command=agent_card.reply("account", "agent", HERE)))
        elif ask:
            buttons.append(agent_card.button("Switch account", "reply", reply="Switch account",
                                             command=agent_card.reply("account", "agent", "switch")))
            if self.remote is not None:
                buttons.append(agent_card.button("Add account", "reply", reply="Add account",
                                                 command=agent_card.reply("account", "agent", "add")))
            if self.remote is not None and self.switch_anywhere:
                # The browser sign-in, asked for by name: on this computer only, and the way when device code fails.
                buttons.append(agent_card.button(HERE_LABEL, "reply", reply=HERE_LABEL,
                                                 command=agent_card.reply("account", "agent", HERE)))
        if url:
            buttons.append(agent_card.button("Open sign-in page", "url", url=url))
        built = agent_card.build(CARD if ask else NOTE, state, title="Account", body="Account of %s" % self.name,
                                 text="%s %s" % (text, BELOW) if ask else text,
                                 **({"buttons": buttons} if buttons else {}), **({"groups": groups} if groups else {}))
        return with_controls(built, self.runtime, signed_out=signed_out) if ask else built

    def say(self, room, built, result_file=None, edit: str = "") -> None:
        """Post the card (in place of the card `edit` names, when given); where it cannot be posted, a
        task's result carries its words instead."""
        posted = self.post(room, built, edit=edit)
        if result_file:
            self.publish(result_file, ANSWERED if posted else self.quoted(built[1]["text"]) + "\n")

    def asked(self):
        """The card for an owner who asked: led by the usage limit while one is in force, and marked as one."""
        text, refused = self.status()
        try:
            out = (self.limit() if self.limit else "") or ""
        except Exception as e:  # noqa: BLE001 - the card still says the account
            self.log("limit not read:", "%s: %s" % (type(e).__name__, str(e)[:200]))
            out = ""
        built = self.card("pending", "%s %s" % (out, text) if out else text, ask=True, signed_out=refused)
        return limited(built) if out else built

    def offer(self, sent, result_file) -> str:
        self.say(sent.get("channel_id"), self.asked(), result_file)
        return "account-card"

    def asked_for(self, words, sent, result_file) -> str:
        return self.offer(sent, result_file)

    def expired(self, sent, result_file) -> str:
        """A tap on an account card that no longer acts: a fresh card in its place when one can be shown,
        else the words. No sign-in is started by it."""
        fresh = self.post(sent.get("channel_id"), self.asked(), edit=card_desk.tapped_event(sent))
        self.publish(result_file, ANSWERED if fresh else self.quoted(EXPIRED_NO_CARD) + "\n")
        return "account-card-expired"

    def tapped(self, verb, sent, result_file) -> str:
        """The owner tapped "Switch account": start a sign-in, show its page here, and say so. The page
        is checked, and its card built, before anything is opened or kept: a page that is not a plain
        https address is given up at once, so a retry cannot open a browser again and again."""
        room, tapped = str(sent.get("channel_id") or ""), card_desk.tapped_event(sent)
        if self.saved is not None and KEY_RE.fullmatch(str(verb or "")):
            return self.pick(verb, sent, result_file)
        if self.saved is not None and verb == "switch":
            # An older card's "Switch account": the dropdown in its place, when it has accounts to pick.
            text, refused = self.status()
            saved = self.menu_accounts()
            if saved:
                self.say(room, self.card("pending", text, ask=True, signed_out=refused, saved=saved),
                         result_file, tapped)
                return "account-picks"
        # A tap does not say its device, and a browser sign-in finishes only here (owner 11-27: the phone's tap
        # opened the Mac's browser): `switch_anywhere` switches with the sign-in finished on any device.
        if self.remote is not None and self.switch_anywhere and verb == "switch":
            verb = "add"
        elif verb == HERE:
            verb = "switch"     # the browser sign-in on this computer, by name
        if self.remote is not None and verb in ("add", "use", "discard", "signout"):
            return self.remote.tapped(verb, sent, result_file)
        if verb != "switch":
            self.publish(result_file, self.quoted("I couldn't match that button. Send /account for a new card.") + "\n")
            return "account-unknown"
        waiting = self.pending()
        if waiting:
            self.give_up(waiting)
        if self.remote is not None:
            self.remote.stop()      # one sign-in at a time: a device-code one still waiting ends
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

    def finished(self, login, success, error="", note="") -> str:
        """The runtime reported a sign-in over: tell the owner, in the DM it was asked in, how it ended
        and which account is in use now, and after one that took, the connector's `note`. '' for any
        report that does not name the sign-in in progress: one without an id could be another
        sign-in's, and must not end this one."""
        waiting = self.pending()
        if not waiting or not login or str(login) != waiting["id"]:
            return ""
        if success:
            self.last_word(waiting, "done", " ".join(x for x in ("Signed in.", self.using(), note) if x))
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
