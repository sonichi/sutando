"""claude-local `account`: which Anthropic account an agent's session runs on, and signing it in again.

Both are Claude Code's own: `claude auth status --json` names the account of a config dir, and
`claude auth login` signs that dir in (it opens the browser of the computer it runs on and waits).
No API key is involved anywhere here.

Two agents are not signed in again by this. One with no config dir of its own: the dir is then the
person's own Claude Code login, and a sign-in there would change what they run by hand. And one
whose requests go through the credential proxy (ANTHROPIC_BASE_URL): the proxy replaces the
session's credential with its own, so its sign-in, not this agent's, decides the account. Stdlib only.
"""
from __future__ import annotations

import json
import subprocess

LOGIN_WAIT_S = 600.0        # how long a sign-in in the browser may take
STATUS_WAIT_S = 30.0
PROXY_VAR = "ANTHROPIC_BASE_URL"
SHARED = ("this agent has no Claude config dir of its own (install --claude-config-dir): signing in here would "
          "change your own Claude Code login, so nothing was done")
PROXIED = ("this agent's requests go through the credential proxy on this computer, which uses its own sign-in "
           "for every session behind it: switch the account there (the core's), not here; nothing was done")


class Refused(Exception):
    """Nothing was changed; the message is the one line for the caller."""


def masked(address: str) -> str:
    """An address as it may be shown in a room: the first letter of the name, and the domain."""
    name, at, domain = str(address or "").partition("@")
    return "%s…@%s" % (name[:1], domain) if at and name and domain else ""


def own_dir(cfg: dict) -> str:
    """The agent's own config dir; Refused when it has none."""
    if not cfg.get("CLAUDE_CONFIG_DIR"):
        raise Refused(SHARED)
    return cfg["CLAUDE_CONFIG_DIR"]


def status(cfg: dict, env: dict, run=subprocess.run) -> dict:
    """{signed_in, method, account, plan, proxy}: the account of the agent's config dir, as Claude Code
    reports it. `account` is masked. `proxy` says the session's requests go through the credential
    proxy, whose own sign-in is then the one in use. Unknown reads as signed_in None, never as signed out."""
    out = {"signed_in": None, "method": "", "account": "", "plan": "", "proxy": bool(env.get(PROXY_VAR))}
    try:
        r = run([cfg["CLAUDE"], "auth", "status", "--json"], capture_output=True, text=True, timeout=STATUS_WAIT_S,
                env=env, stdin=subprocess.DEVNULL)
        data = json.loads(r.stdout or "")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return dict(out, detail="claude auth status did not answer (%s)" % type(e).__name__)
    if not isinstance(data, dict) or not isinstance(data.get("loggedIn"), bool):
        return dict(out, detail="claude auth status gave no answer this understands")
    method = str(data.get("authMethod") or "")
    return dict(out, signed_in=data["loggedIn"], method=method, account=masked(data.get("email")),
                plan=str(data.get("subscriptionType") or ""))


def words(state: dict) -> str:
    """One line a person can read, for the result of `account`."""
    if state.get("proxy"):
        return "My requests go through the credential proxy on this computer; its sign-in decides my account."
    if state.get("signed_in") is None:
        return "I could not read which account I'm using."
    if not state["signed_in"]:
        return "I'm not signed in."
    who = state.get("account") or "an Anthropic account"
    return "I'm signed in as %s%s." % (who, " (%s)" % state["plan"] if state.get("plan") else "")


def sign_in(cfg: dict, env: dict, console: bool = False, run=subprocess.run, wait_s: float = LOGIN_WAIT_S) -> "tuple[bool, str]":
    """Run Claude Code's own sign-in for the agent's config dir. It opens the sign-in page in the browser
    of this computer and returns when the person has finished there, or after `wait_s`. (ok, why not)."""
    own_dir(cfg)
    if env.get(PROXY_VAR):
        raise Refused(PROXIED)
    argv = [cfg["CLAUDE"], "auth", "login"] + (["--console"] if console else [])
    try:
        r = run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=wait_s,
                env=env)
    except subprocess.TimeoutExpired:
        return False, "the sign-in was not finished in %d minutes" % (wait_s // 60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, "claude auth login did not run (%s)" % type(e).__name__
    return (True, "") if r.returncode == 0 else (False, "claude auth login ended with exit code %d" % r.returncode)
