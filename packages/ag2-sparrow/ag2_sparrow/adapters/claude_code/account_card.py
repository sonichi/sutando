"""claude-local's account card: the owner asks in their DM which Anthropic account the agent runs on, or
to switch it, and is shown the account (masked) and where to switch it.

Same design as the model card (#6685's `account_switch` card, routed by `card_desk`), with one difference:
a sign-in code never travels as a chat message. "Switch account" therefore starts nothing here; it points
to the desktop app (Settings → this agent's Connections card → Switch account), which runs the agent's own
sign-in (`ag2-sparrow-claude-local account --start`) and takes the code in the app's own box.

Asked by "/account" or "/login" in the owner's DM, a tap on this card, or the session answering the owner
with the card's mark. The account is Claude Code's own answer for the agent's config dir
(`claude auth status --json`, CLAUDE_CONFIG_DIR); only a masked address and the plan are shown.
"""
from __future__ import annotations

import json
import os
import subprocess

from ... import account_switch, connect_bridge as cb, claude_local_records as records
from ...acp_host import quoted

NAME = "Claude Code"
STATUS_WAIT_S = 10.0     # asked inside the claim: an answer the card cannot wait for reads as unknown
ASKS = ("/account", "/login")
WHERE = ("To switch it, open Settings in the S.space app, then this agent's Connections card, then Switch "
         "account. The sign-in happens there and the code goes into the app's own box, never into this chat.")
_run = subprocess.run       # Claude Code's own `auth status`, the one command this card runs


def status(cfg: dict, env=None, run=None) -> dict:
    """{signed_in, account, plan, proxy} for the agent's own config dir, as Claude Code reports it (masked).
    Unknown reads as signed_in None, never as signed out."""
    run = run or _run
    env = dict(os.environ if env is None else env)
    if cfg.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = cfg["CLAUDE_CONFIG_DIR"]
    out = {"signed_in": None, "account": "", "plan": "", "proxy": bool(env.get("ANTHROPIC_BASE_URL"))}
    try:
        r = run([cfg.get("CLAUDE") or "claude", "auth", "status", "--json"], capture_output=True, text=True,
                timeout=STATUS_WAIT_S, env=env, stdin=subprocess.DEVNULL)
        data = json.loads(r.stdout or "")
    except (OSError, subprocess.SubprocessError, ValueError):
        return out
    if not isinstance(data, dict) or not isinstance(data.get("loggedIn"), bool):
        return out
    return dict(out, signed_in=data["loggedIn"], account=account_switch.masked(str(data.get("email") or "")),
                plan=str(data.get("subscriptionType") or ""))


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
    who = st["account"] or "an Anthropic account"
    return "%s (%s)" % (who, st["plan"]) if st["plan"] else who


class AccountCard(account_switch.AccountSwitch):
    """The account card, its "Switch account" pointing to the app instead of starting a sign-in here."""

    def __init__(self, root, cfg: dict, publish, log, ask: str = "/account", run=None):
        super().__init__(str(records.state_dir(root)), str(cb.bridge_state(cfg)), NAME, publish, quoted, log,
                         read=lambda: label(cfg, run=run), begin=None, cancel=None, opener=None)
        self.ASK = ask

    def card(self, state: str, text: str, url: str = "", ask: bool = False):
        return super().card(state, "%s %s" % (text, WHERE) if ask else text, "", ask)

    def tapped(self, verb, sent, result_file) -> str:
        """"Switch account": where to switch it, in a card of its own. Nothing is started here."""
        if verb != "switch":
            self.publish(result_file, self.quoted(account_switch.EXPIRED_NO_CARD) + "\n")
            return "account-unknown"
        self.say(sent.get("channel_id"), self.card("info", WHERE), result_file)
        return "account-where"


def cards(root, cfg: dict, publish, log, run=None) -> list:
    """The account card once per way of asking for it ("/account", "/login")."""
    return [AccountCard(root, cfg, publish, log, ask=a, run=run) for a in ASKS]
