"""Antigravity's account card: in their DM with the agent, the owner sees whether it runs on the Google
sign-in or on the Gemini API key saved for it in the AG2 Space app, and can switch from there.

It is a card of card_desk's `Desk`, asked for as the Codex account card is (account_switch): /account, the
runtime answering with account_switch.MARK alone, or a tap on its buttons; only the owner's tap in their DM
acts. Its buttons:

- "Sign in with Google again" opens a Terminal window on the computer it runs on with `agy acp --login`: the
  owner follows the link there and pastes the code back into that Terminal. Nothing of the sign-in passes
  through the room or this process. agy keeps one Google sign-in in the Keychain for this Mac, so this moves
  every Antigravity agent here; the card says so. Where no Terminal can be opened, the card says where to
  sign in instead (the agent's Antigravity card in the app's Connections, or `agy acp --login`).
- "Use a Gemini API key" points the owner to that same Connections card, which saves the key in the
  Keychain item gemini_key reads. A key is never taken in chat: one pasted here is not passed to agy
  (`looks_like_key`), and the owner is asked to delete the message.
- "Stop using the key" deletes this agent's own Keychain item (gemini_key.clear_key).

Which one is in use is read from whether the item exists (gemini_key.key_saved), never from the key; the
next turn opens a fresh Session when that changes (acp_host saves the auth mode with each Session).
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys

from ... import account_switch, card_desk, connector_cards
from . import gemini_key

GOOGLE, KEY, UNKEY = "google", "key", "unkey"
LABELS = {GOOGLE: "Sign in with Google again", KEY: "Use a Gemini API key", UNKEY: "Stop using the key"}
SHARED = ("Antigravity keeps one Google sign-in for this Mac, so this changes it for every Antigravity agent "
          "here.")
WHERE = "the AG2 Space app: Connections, then %s's Antigravity card"
NO_KEYS_HERE = ("Please don't send the key in this chat: I don't take keys here, and one sent here is never "
                "passed on.")
KEY_IN_CHAT = ("That looks like a Gemini API key, so I did not pass your message on. Please delete it from "
               "this chat and add the key in %s instead.")
SCRIPT_NAME = "agy-google-sign-in.command"
# Google API keys: "AIza" and 35 more characters of [0-9A-Za-z_-].
_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{35}")


def looks_like_key(text) -> bool:
    """Whether `text` carries something shaped like a Google API key."""
    return bool(_KEY_RE.search(text or ""))


def open_terminal(argv, state_dir, env=None, run=subprocess.run, platform=None) -> bool:
    """Open a Terminal window running `argv` (`agy acp --login`) on this Mac; False where it cannot be. The
    command goes in a 0700 `.command` file in the agent's state dir, made of the resolved program and
    shell-quoted arguments only, so nothing a room said can reach it."""
    if (platform or sys.platform) != "darwin" or not argv:
        return False
    program = shutil.which(argv[0], path=(env or os.environ).get("PATH")) or argv[0]
    if not os.path.isabs(program):
        return False
    path = os.path.join(os.path.abspath(state_dir), SCRIPT_NAME)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexec %s\n" % " ".join(shlex.quote(a) for a in [program] + list(argv[1:])))
        os.chmod(tmp, 0o700)
        os.replace(tmp, path)
        done = run(["/usr/bin/open", "-a", "Terminal", path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(done, "returncode", 1) == 0


class AgyAccount:
    KIND, ASK, MARK, HINT, IN_DM = "account", "/account", account_switch.MARK, account_switch.HINT, account_switch.IN_DM
    CARD_ID = account_switch.CARD

    def __init__(self, bridge_state, name, agent_id, publish, quoted, log, sign_in, saved=None, clear=None):
        """`sign_in()` opens the Google sign-in on this computer (True when it did); `saved()` and `clear()`
        default to gemini_key's for `agent_id`."""
        self.bridge_state, self.name, self.agent_id = bridge_state, name, agent_id or ""
        self.publish, self.quoted, self.log, self.sign_in = publish, quoted, log, sign_in
        self.saved = saved or (lambda: gemini_key.key_saved(self.agent_id))
        self.clear = clear or (lambda: gemini_key.clear_key(self.agent_id))

    def usable(self) -> bool:
        return card_desk.usable(self.bridge_state)

    def where(self) -> str:
        return WHERE % self.name

    def keyed(self) -> bool:
        try:
            return bool(self.saved())
        except Exception as e:  # noqa: BLE001 - unknown reads as the sign-in, which is what agy falls back to
            self.log("Gemini API key not checked:", type(e).__name__)
            return False

    def using(self, keyed=None) -> str:
        keyed = self.keyed() if keyed is None else keyed
        if keyed:
            return ("I'm using a Gemini API key (saved for me in the AG2 Space app) when Antigravity offers it, "
                    "else the Google sign-in.")
        return "I'm using the Google sign-in on this computer."

    def card(self, state, text, buttons=False, keyed=False):
        agent_card, _ = connector_cards._core()
        extra = {}
        if buttons:
            verbs = (GOOGLE, UNKEY) if keyed else (GOOGLE, KEY)
            extra["buttons"] = [agent_card.button(LABELS[v], "reply", reply=LABELS[v],
                                                  command=agent_card.reply("account", "agent", v)) for v in verbs]
        return agent_card.build(account_switch.CARD if buttons else account_switch.NOTE, state, title="Account",
                                body="Account of %s" % self.name, text=text, **extra)

    def menu(self):
        keyed = self.keyed()
        mode = "Gemini API key" if keyed else "Google sign-in"
        return self.card("pending", "%s %s" % (mode + ".", self.using(keyed)), buttons=True, keyed=keyed)

    def say(self, room, built, result_file) -> None:
        posted = card_desk.post(self.bridge_state, room, built, self.log)
        self.publish(result_file, account_switch.ANSWERED if posted else self.quoted(built[1]["text"]) + "\n")

    def offer(self, sent, result_file) -> str:
        self.say(sent.get("channel_id"), self.menu(), result_file)
        return "account-card"

    def asked_for(self, words, sent, result_file) -> str:
        return self.offer(sent, result_file)

    def expired(self, sent, result_file) -> str:
        fresh = card_desk.post(self.bridge_state, sent.get("channel_id"), self.menu(), self.log)
        self.publish(result_file, self.quoted(account_switch.EXPIRED if fresh else account_switch.EXPIRED_NO_CARD) + "\n")
        return "account-card-expired"

    def tapped(self, verb, sent, result_file) -> str:
        room = sent.get("channel_id")
        if verb == GOOGLE:
            try:
                opened = bool(self.sign_in())
            except Exception as e:  # noqa: BLE001 - the owner is told where to sign in instead
                self.log("Google sign-in not opened:", type(e).__name__)
                opened = False
            still = (" The Gemini API key saved for me is still used first: tap \"Stop using the key\" to run on "
                     "the Google sign-in.") if self.keyed() else ""
            if opened:
                text = ("I opened a Terminal window on the computer I run on with Antigravity's Google sign-in. "
                        "Open the link it shows, then paste the code back into that Terminal, not here. %s%s"
                        % (SHARED, still))
                self.say(room, self.card("info", text), result_file)
                return "account-google-started"
            text = ("I could not open the sign-in on the computer I run on. Sign in from %s, or run `agy acp "
                    "--login` in a terminal there. %s%s" % (self.where(), SHARED, still))
            self.say(room, self.card("error", text), result_file)
            return "account-google-not-started"
        if verb == KEY:
            text = ("Add the key in %s, under \"Use a Gemini API key\". My next message then starts a fresh "
                    "session on it. %s" % (self.where(), NO_KEYS_HERE))
            self.say(room, self.card("info", text), result_file)
            return "account-key-where"
        if verb == UNKEY:
            try:
                gone = bool(self.clear())
            except Exception as e:  # noqa: BLE001 - said, never raised into the task
                self.log("Gemini API key not removed:", type(e).__name__)
                gone = False
            if gone:
                self.say(room, self.card("done", "I stopped using the Gemini API key: it is removed from this "
                                                 "Mac's Keychain, and my next message starts a fresh session on the "
                                                 "Google sign-in."), result_file)
                return "account-key-cleared"
            self.say(room, self.card("error", "I could not remove the Gemini API key. Remove it from %s. %s"
                                     % (self.where(), self.using())), result_file)
            return "account-key-not-cleared"
        self.publish(result_file, self.quoted("I couldn't match that button. Send /account for a new card.") + "\n")
        return "account-unknown"
