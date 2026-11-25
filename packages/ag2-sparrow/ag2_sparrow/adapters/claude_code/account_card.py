"""claude-local's account card: the owner asks in their DM which Anthropic account the agent runs on, or
to switch it, and is shown the account and where to switch it.

The shared account card (`account_switch`, routed by `card_desk`), with one difference: a sign-in code
never travels as a chat message. The card carries `account_controls`: in the desktop app, on the computer
that runs the agent, the owner switches account right in the card (the app runs the agent's own sign-in,
`ag2-sparrow-claude-local account --start`, and takes the code in the card's own box, never sent to the
chat). "Switch account", for a client without those controls, starts nothing here and says where to open
the card, in place of the card tapped, which keeps the controls for when it is opened there. English only.

Asked by "/account" or "/login" in the owner's DM, a tap on this card, or the session answering the owner
with the card's mark. The account is Claude Code's own answer for the agent's config dir, read by the
adapter's one reader of it (`account.status`); the address is shown as the shared card shows one.
"""
from __future__ import annotations

import os
import subprocess

from ... import account_switch, card_desk, connect_bridge as cb, claude_local_records as records
from ...acp_host import quoted
from . import account as accounts

NAME = "Claude Code"
RUNTIME = "claude"
STATUS_WAIT_S = 10.0     # asked inside the claim: an answer the card cannot wait for reads as unknown
ASKS = ("/account", "/login")
WHERE = account_switch.elsewhere(NAME) + " The sign-in code goes into the card there, never into this chat."
_run = subprocess.run       # Claude Code's own `auth status`, the one command this card runs


def status(cfg: dict, env=None, run=None) -> dict:
    """{signed_in, account, plan, proxy, ...} for the agent's own config dir, as `account.status` reads it,
    the address shown by the shared card's rule. Unknown reads as signed_in None, never as signed out."""
    env = dict(os.environ if env is None else env)
    if cfg.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = cfg["CLAUDE_CONFIG_DIR"]
    return accounts.status(dict(cfg, CLAUDE=cfg.get("CLAUDE") or "claude"), env, run=run or _run,
                           show=account_switch.masked, wait_s=STATUS_WAIT_S)


def label(cfg: dict, env=None, run=None) -> str:
    """The account in use as the card names it; '' when signed out. Raises when it cannot be read, so
    the card says it could not tell (account_switch.using), never that the agent is signed out."""
    st = status(cfg, env, run)
    if st["proxy"]:
        return "the credential proxy's account (it decides mine)"
    if st["signed_in"] is None:
        raise RuntimeError("claude auth status gave no answer")
    if not st["signed_in"]:
        return ""
    return st["account"] or "an Anthropic account"      # the account only, never its plan


class AccountCard(account_switch.AccountSwitch):
    """The account card, its "Switch account" pointing to the app instead of starting a sign-in here."""

    def __init__(self, root, cfg: dict, publish, log, ask: str = "/account", run=None):
        super().__init__(str(records.state_dir(root)), str(cb.bridge_state(cfg)), NAME, publish, quoted, log,
                         read=lambda: label(cfg, run=run), begin=None, cancel=None, opener=None)
        self.ASK = ask

    def card(self, state: str, text: str, url: str = "", ask: bool = False):
        """The shared card; the one asked for also says the controls are below and carries their marker."""
        if not ask:
            return super().card(state, text, url, ask)
        return account_switch.with_controls(super().card(state, "%s %s" % (text, account_switch.BELOW), url, ask), RUNTIME)

    def tapped(self, verb, sent, result_file) -> str:
        """"Switch account": where to switch it, shown in place of the card tapped and still carrying the
        controls, so opened on the agent's computer it is the account card again. Nothing is started here."""
        if verb != "switch":
            self.publish(result_file, self.quoted(account_switch.EXPIRED_NO_CARD) + "\n")
            return "account-unknown"
        self.say(sent.get("channel_id"), account_switch.with_controls(self.card("info", WHERE), RUNTIME),
                 result_file, card_desk.tapped_event(sent))
        return "account-where"


def cards(root, cfg: dict, publish, log, run=None) -> list:
    """The account card once per way of asking for it ("/account", "/login")."""
    return [AccountCard(root, cfg, publish, log, ask=a, run=run) for a in ASKS]
