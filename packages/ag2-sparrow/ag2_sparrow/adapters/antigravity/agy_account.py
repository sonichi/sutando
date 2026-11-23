"""Antigravity's account card, in the owner's DM with the agent (Commons "switch account / API key", part A).

The owner says "switch account" or "use API key" (in English or Chinese, or /account, or the turn answers
with the account card's mark) and gets a card with "Switch account" and "Use API key" (plus "Stop using API
key" while a key is saved). Its words are English only. The card carries `account_controls`: in the desktop
app, on the computer that runs the agent, the owner switches account or enters a key right in the card
(nothing typed there reaches the room); elsewhere the client says where to open it. It is a card
of card_desk's `Desk`, asked for and tapped as the Codex account card is (account_switch): only the owner's
tap in their DM acts.

No secret is ever taken in chat: every card reply and card input in the web client is sent as a room
message, so neither a sign-in code nor a key may travel that way.

- "Switch account" (for a client without the in-card controls) says to open the card on the computer that
  runs the agent, where the desktop runs `agy acp --login` headless and takes the code in the card. agy
  keeps one Google sign-in per Mac, so this moves every Antigravity agent here; the card says so. The next
  owner turn reads which account agy authenticated (`applyAuthResult` in agy's own log, `signed_in_as`) and
  says when it changed.
- "Use API key" says the same for the key field (the Keychain item gemini_key reads).
- "Stop using API key" deletes this agent's own Keychain item (gemini_key.clear_key).
- A sign-in code, a landing address with one, a Gemini key, or the bridge's redaction marker for one, sent
  in chat is never passed to agy (`carries_secret`): the owner is asked to delete the message.
- When a turn fails on agy's quota (RESOURCE_EXHAUSTED, "Individual quota reached", a 429), the connector
  posts this card (`quota_card`) in the owner's DM on its own, once per limit (quota_notice).
"""
from __future__ import annotations

import json
import os
import re

from ... import account_switch, card_desk, connector_cards
from . import gemini_key

SWITCH, KEY, UNKEY = "switch", "key", "unkey"
LABELS = {SWITCH: "Switch account", KEY: "Use API key", UNKEY: "Stop using API key"}
SHARED = ("Note: every Antigravity agent on this Mac shares one Google sign-in, so switching moves them all to "
          "the new account.")
# The card's own words and its marker (the same as the Codex account card's, account_switch on #6685).
BELOW = "Switch this agent's account or API key below."
CONTROLS = "account_controls"
_MXID_RE = re.compile(r"^@[^\s:]+:\S+$")


def elsewhere(name: str) -> str:
    """Where the account controls are, for a place that cannot draw them."""
    return "Open this on the computer that runs %s to switch account." % (name or "this agent")


STATE_NAME = "agy-account.json"
QUOTA_TEXT_MAX = 400     # a completed turn's text counts as agy's quota error only when it is this short
# Words that ask for the card, on their own (short messages only, so a long request is the model's).
ASK_RE = re.compile(r"^\s*(?:/account|(?:请|帮我|我要|我想)?(?:切换|换|更换)(?:一下|个)?账号|switch(?: the)? accounts?|"
                    r"change(?: the)? accounts?|(?:改)?用\s*api\s*key|use (?:an |a |the )?(?:gemini )?api key)"
                    r"\s*[。.!！?？]?\s*$", re.I)
KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
REDACTED_KEY = "[REDACTED-Google API Key]"
KEY_IN_CHAT = ("This message looks like it has a sign-in code or an API key, so I did not pass it to Antigravity "
               "and did not keep it. Please delete it from the chat. Send /account instead: on the computer that "
               "runs me, the account card takes the code or the key without sending it to the chat.")
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|Individual quota reached|quota (?:exceeded|exhausted|reached)"
                      r"|exceeded your (?:current )?quota|\b429\b.{0,40}\b(?:Too Many Requests|quota|rate.?limit)"
                      r"|(?:status|code|HTTP)\W{0,3}429\b", re.I)
_SIGN_IN_CODE = re.compile(r"(?<![A-Za-z0-9])4/[0-9A-Za-z_\-]{20,}")
_CODE_URL = re.compile(r"https?://\S*[?&]code=[^&\s]{8,}")
_AUTH_LINE = re.compile(r"applyAuthResult: email=([^,\s]+@[^,\s]+)")


def quota_error(status, text, detail) -> bool:
    """Whether a turn ended on agy's quota: a failed turn whose error says so, or a completed one whose whole
    (short) answer is that error, as agy has answered it."""
    if status == "completed":
        said = (text or "").strip()
        return 0 < len(said) <= QUOTA_TEXT_MAX and bool(QUOTA_RE.search(said))
    return status in ("failed", "refused") and bool(QUOTA_RE.search("%s %s" % (text or "", detail or "")))


def asks_for_card(text) -> bool:
    return bool(ASK_RE.match(text or "")) and len(text or "") <= 60


def carries_key(text) -> bool:
    """A Gemini API key, or the bridge's marker for one it redacted."""
    return bool(KEY_RE.search(text or "")) or REDACTED_KEY in (text or "")


def carries_secret(text) -> bool:
    """A Gemini API key or its redaction marker, a Google sign-in code (4/0A...), or an address carrying one."""
    said = text or ""
    return carries_key(said) or bool(_SIGN_IN_CODE.search(said)) or bool(_CODE_URL.search(said))


def signed_in_as(log_dir, pid) -> str:
    """The account agy process `pid` authenticated as, from its own log (`acp-server-<time>-<pid>.log`,
    `applyAuthResult: email=...`), shortened (account_switch.masked); '' when it says none. Only that line
    is read."""
    try:
        names = [n for n in os.listdir(log_dir) if n.startswith("acp-server-") and n.endswith("-%d.log" % int(pid))]
    except (OSError, TypeError, ValueError):
        return ""
    for name in sorted(names, reverse=True):
        try:
            with open(os.path.join(log_dir, name), encoding="utf-8", errors="replace") as f:
                found = [m.group(1) for m in (_AUTH_LINE.search(line) for line in f) if m]
        except OSError:
            continue
        if found:
            return account_switch.masked(found[-1])
    return ""


def agy_log_dir(env) -> str:
    """Where agy writes its logs by default: <HOME>/.gemini/antigravity-cli/log."""
    home = (env or {}).get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".gemini", "antigravity-cli", "log")


class AgyAccount:
    KIND, ASK, MARK, HINT, IN_DM = "account", "/account", account_switch.MARK, account_switch.HINT, account_switch.IN_DM
    CARD_ID = account_switch.CARD

    def __init__(self, state_dir, bridge_state, name, agent_id, publish, quoted, log, saved=None, clear=None):
        self.state_dir, self.bridge_state, self.name, self.agent_id = state_dir, bridge_state, name, agent_id or ""
        self.publish, self.quoted, self.log = publish, quoted, log
        self.saved = saved or (lambda: gemini_key.key_saved(self.agent_id))
        self.clear = clear or (lambda: gemini_key.clear_key(self.agent_id))

    # ── state (no secret: the account's shortened name and when the quota card was last shown) ──
    def record(self) -> dict:
        try:
            with open(os.path.join(self.state_dir, STATE_NAME), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def keep(self, **changes) -> None:
        state = dict(self.record(), **changes)
        path = os.path.join(self.state_dir, STATE_NAME)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
        os.replace(path + ".tmp", path)

    def usable(self) -> bool:
        return card_desk.usable(self.bridge_state)

    def keyed(self) -> bool:
        try:
            return bool(self.saved())
        except Exception as e:  # noqa: BLE001 - unknown reads as the sign-in, which agy falls back to
            self.log("Gemini API key not checked:", type(e).__name__)
            return False

    def using(self, keyed=None) -> str:
        keyed = self.keyed() if keyed is None else keyed
        if keyed:
            return "I'm using this agent's own Gemini API key."
        who = str(self.record().get("account") or "")
        return "I'm using this computer's Google sign-in%s." % (" (%s)" % who if who else "")

    def card(self, state, text, menu=False, keyed=False, url=""):
        agent_card, _ = connector_cards._core()
        buttons = []
        if menu:
            verbs = (SWITCH, KEY, UNKEY) if keyed else (SWITCH, KEY)
            buttons = [agent_card.button(LABELS[v], "reply", reply=LABELS[v],
                                         command=agent_card.reply("account", "agent", v)) for v in verbs]
        if url:
            buttons.append(agent_card.button("Open sign-in page", "url", url=url))
        body, built = agent_card.build(account_switch.CARD if menu else account_switch.NOTE, state,
                                       title="Antigravity account", body="Account of %s" % self.name, text=text,
                                       **({"buttons": buttons} if buttons else {}))
        if menu:
            # The marker the desktop app draws this agent's account controls from; older clients ignore it.
            marker = {"runtime": "antigravity"}
            if _MXID_RE.match(self.agent_id):
                marker["agent"] = self.agent_id
            built = dict(built, **{CONTROLS: marker})
        return body, built

    def menu(self, lead=""):
        keyed = self.keyed()
        return self.card("pending", "%s%s %s" % (lead + " " if lead else "", self.using(keyed), BELOW),
                         menu=True, keyed=keyed)

    def post(self, room, built) -> bool:
        return card_desk.post(self.bridge_state, room, built, self.log)

    def say(self, room, built, result_file) -> None:
        posted = self.post(room, built)
        self.publish(result_file, account_switch.ANSWERED if posted else self.quoted(built[1]["text"]) + "\n")

    # ── card_desk's card ──
    def offer(self, sent, result_file) -> str:
        self.say(sent.get("channel_id"), self.menu(), result_file)
        return "account-card"

    def asked_for(self, words, sent, result_file) -> str:
        return self.offer(sent, result_file)

    def expired(self, sent, result_file) -> str:
        fresh = self.post(sent.get("channel_id"), self.menu())
        self.publish(result_file, self.quoted(account_switch.EXPIRED if fresh else account_switch.EXPIRED_NO_CARD) + "\n")
        return "account-card-expired"

    def tapped(self, verb, sent, result_file) -> str:
        room = str(sent.get("channel_id") or "")
        if verb == SWITCH:
            self.say(room, self.card("info", "%s Never send a sign-in code in this chat. %s I'll name the new account "
                                             "in my next reply." % (elsewhere(self.name), SHARED)), result_file)
            return "account-switch-where"
        if verb == KEY:
            self.say(room, self.card("info", "Open this on the computer that runs %s to enter a Gemini API key: it is "
                                             "kept in that Mac's Keychain and used from the next message. A key sent "
                                             "in this chat is masked by the platform, so I never receive or keep it."
                                             % self.name), result_file)
            return "account-key-where"
        if verb == UNKEY:
            try:
                outcome = self.clear()
            except Exception as e:  # noqa: BLE001 - said as a failure, never raised into the task
                self.log("Gemini API key not removed:", type(e).__name__)
                outcome = gemini_key.FAILED
            if outcome == gemini_key.REMOVED:
                self.say(room, self.card("done", "Stopped using the API key and deleted it from the Keychain. From the next "
                                                 "message I use the Google sign-in, in a new session."),
                         result_file)
                return "account-key-cleared"
            if outcome == gemini_key.ABSENT:
                # A tap on an older card, after the key was already removed: nothing to do, and said so.
                self.say(room, self.card("info", "This agent has no saved API key, so nothing changed. " + self.using(False)), result_file)
                return "account-key-absent"
            self.log("Gemini API key not removed (security failed)")
            self.say(room, self.card("error", "I could not delete the API key (the Keychain refused), so it is still in "
                                              "use. Send /account to try again."),
                     result_file)
            return "account-key-not-cleared"
        self.publish(result_file, self.quoted("I couldn't match that button. Send /account for a new card.") + "\n")
        return "account-unknown"

    # ── which account agy used ──
    def saw(self, who) -> str:
        """A turn's agy authenticated as `who` (masked, '' unknown): the words to lead the answer with when
        that is another account than the last one seen, else ''. The first account seen is only kept."""
        if not who:
            return ""
        before = str(self.record().get("account") or "")
        if before == who:
            return ""
        self.keep(account=who)
        return "(Switched to the %s account.)" % who if before else ""

    # ── out of quota (quota_notice posts it) ──
    def quota_card(self, text):
        """The account card for the quota notice, led by `text` (which already names the account in use)."""
        keyed = self.keyed()
        return self.card("pending", "%s %s" % (text, BELOW), menu=True, keyed=keyed)
