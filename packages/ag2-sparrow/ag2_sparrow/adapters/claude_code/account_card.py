"""claude-local's account card: the owner asks in their DM which Anthropic account the agent runs on, or
to switch it, and is shown the account and where to switch it.

The shared account card (`account_switch`, routed by `card_desk`), with one difference: a sign-in code
never travels as a chat message. The card carries `account_controls`: in the desktop app, on the computer
that runs the agent, the owner switches account right in the card (the app runs the agent's own sign-in,
`ag2-sparrow-claude-local account --start`, and takes the code in the card's own box, never sent to the
chat). "Switch account", for a client without those controls, starts nothing here and says where to open
the card, in place of the card tapped, which keeps the controls for when it is opened there. English only.

The card also offers a "Use <address>" button for each account the desktop app already saved for this
agent on this computer (its non-secret `index.json`, key -> label; only labels are read), so the owner can
switch from a phone. A tap on one asks the app to switch: it writes `.ag2-switch-request.json` in the
agent's own config dir, shows "Switching to <address>…" in place of the card, and a detached `await`
watches for the app's `.ag2-switch-result.json` (same nonce) for AWAIT_S, then says in that same card
whether it switched. The session's restart on the new account is signin_watch's, not this card's.

Asked by "/account" or "/login" in the owner's DM, a tap on this card, or the session answering the owner
with the card's mark. The account is Claude Code's own answer for the agent's config dir, read by the
adapter's one reader of it (`account.status`); the address is shown as the shared card shows one.

    python3 -m ag2_sparrow.adapters.claude_code.account_card await --root ROOT --nonce NONCE   # the detached step
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ... import account_switch, card_desk, connect_bridge as cb, connector_cards, claude_local_records as records
from ...acp_host import quoted
from ...bounded_file_read import read_regular
from . import account as accounts

NAME = "Claude Code"
RUNTIME = "claude"
STATUS_WAIT_S = 10.0     # asked inside the claim: an answer the card cannot wait for reads as unknown
ASKS = ("/account", "/login")
WHERE = account_switch.elsewhere(NAME) + " The sign-in code goes into the card there, never into this chat."
_run = subprocess.run       # Claude Code's own `auth status`, the one command this card runs
_popen = subprocess.Popen   # the detached `await` of a switch request

# The desktop app's saved accounts for this agent, and the request/answer files it watches in the config dir.
STORE = ("accounts", "claude-local")
INDEX, INDEX_MAX = "index.json", 64 << 10
REQUEST, RESULT, RESULT_MAX = ".ag2-switch-request.json", ".ag2-switch-result.json", 16 << 10
USE_BUTTONS = 5          # with "Switch account", the six buttons a card may carry
AWAIT_S, AWAIT_POLL_S = 90.0, 1.0
PENDING = "account-remote"      # state/<PENDING>/<nonce>.json: what the detached `await` tells, and where
KEY_RE = re.compile(r"[0-9a-f]{16}")
NONCE_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
NO_ANSWER = "the app on this computer did not answer — is it running?"
NOT_SAVED = "That account is no longer saved on this computer. Send /account for a new card."
UNMATCHED = "I couldn't match that button. Send /account for a new card."


def status(cfg: dict, env=None, run=None, show=None) -> dict:
    """{signed_in, account, plan, proxy, ...} for the agent's own config dir, as `account.status` reads it,
    the address shown by the shared card's rule (or by `show`). Unknown reads as signed_in None, never as
    signed out."""
    env = dict(os.environ if env is None else env)
    if cfg.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = cfg["CLAUDE_CONFIG_DIR"]
    return accounts.status(dict(cfg, CLAUDE=cfg.get("CLAUDE") or "claude"), env, run=run or _run,
                           show=show or account_switch.masked, wait_s=STATUS_WAIT_S)


# What `label` last read for a config dir: the full address ("email") and whether the proxy decides the
# account ("proxy"), so the card leaves the account in use out of its "Use" buttons without asking again.
SEEN: dict = {}


def label(cfg: dict, env=None, run=None) -> str:
    """The account in use as the card names it; '' when signed out. Raises when it cannot be read, so
    the card says it could not tell (account_switch.using), never that the agent is signed out."""
    st = status(cfg, env, run, show=str)
    SEEN[str(cfg.get("CLAUDE_CONFIG_DIR") or "")] = {"email": st["account"] if st["signed_in"] else "",
                                                     "proxy": st["proxy"]}
    if st["proxy"]:
        return "the credential proxy's account (it decides mine)"
    if st["signed_in"] is None:
        raise RuntimeError("claude auth status gave no answer")
    if not st["signed_in"]:
        return ""
    return account_switch.masked(st["account"]) or "an Anthropic account"      # the account only, never its plan


# ── the accounts the desktop app saved for this agent ───────────────────────────────────────────────────

def saved_key(text: str) -> str:
    """The desktop's store key of an agent id or an account's address: 16 hex of its SHA-256."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def agent_slug(agent: str) -> str:
    """The desktop's folder name for `agent` under `<app data>/sparrow/agents` (sparrow_local.rs agent_slug)."""
    readable = "".join(c if (c.isascii() and c.isalnum()) or c in "._-" else "_" for c in agent.lstrip("@"))[:40]
    return "%s-%s" % (readable, hashlib.sha256(agent.encode("utf-8")).hexdigest()[:12])


def app_data_dirs(cfg: dict, home=None, environ=None) -> list:
    """Where the desktop app keeps its data, most likely first: the one the agent's config dir sits in, when
    the app made that dir (`<app data>/sparrow/agents/<slug>/claude-config`), then the app's folders for
    this user as Tauri lays them out (macOS: `space.ag2.desktop`, then the older `space.ag2.app`)."""
    env = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    found = []
    agent, config = str(cfg.get("AGENT_ID") or ""), str(cfg.get("CLAUDE_CONFIG_DIR") or "")
    if agent and config:
        p = Path(config)
        if p.name == "claude-config" and p.parent.name == agent_slug(agent) and p.parent.parent.name == "agents" \
                and p.parent.parent.parent.name == "sparrow":
            found.append(p.parents[3])
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        found += [base / "space.ag2.desktop", base / "space.ag2.app"]
    elif sys.platform.startswith("win"):
        if env.get("APPDATA"):
            found.append(Path(env["APPDATA"]) / "space.ag2.app")
    else:
        found.append(Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "space.ag2.app")
    return list(dict.fromkeys(found))


def email_only(text: str) -> str:
    """A saved label without its " (plan)" (the desktop's email_only)."""
    t = text.strip()
    i = t.rfind(" (")
    return t[:i].rstrip() if i >= 0 and t.endswith(")") and not any(c in t[i + 2:-1] for c in "()") else t


def saved_accounts(cfg: dict, dirs=None) -> dict:
    """{key: address} of the accounts the desktop saved for this agent, from the first app data dir that has
    its index; {} for none. A label that is not a whole address (a masked copy) is left to the app."""
    agent = str(cfg.get("AGENT_ID") or "")
    if not agent:
        return {}
    for d in (app_data_dirs(cfg) if dirs is None else dirs):
        text, _ = read_regular(str(Path(d).joinpath(*STORE) / saved_key(agent) / INDEX), INDEX_MAX)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        out = {}
        for key, value in (data.items() if isinstance(data, dict) else ()):
            email = email_only(value) if isinstance(value, str) else ""
            if email and KEY_RE.fullmatch(str(key)) and "…" not in email and "...@" not in email and account_switch.shown(email) == email:
                out[key] = email
        return out
    return {}


def others(cfg: dict) -> dict:
    """The saved accounts that are not the one in use (`cfg`'s "email", as the card last read it)."""
    using = str(cfg.get("email") or "").lower()
    return {k: v for k, v in saved_accounts(cfg).items()
            if not using or (v.lower() != using and k != saved_key(cfg.get("email") or ""))}


def config_dir_for_switch(cfg: dict) -> str:
    """The agent's own config dir; accounts.Refused when it has none, or it is not this agent's alone, or the
    credential proxy decides the account (the rules of a sign-in, `account.own_dir`)."""
    try:
        from .cli import other_agents_dirs
        from .shape import LOCAL
        dirs = other_agents_dirs(LOCAL)
    except Exception:  # noqa: BLE001 - the other checks still hold
        dirs = []
    mine = accounts.own_dir(cfg, dirs)
    if os.environ.get(accounts.PROXY_VAR):
        raise accounts.Refused(accounts.PROXIED)
    return mine


def write_request(config_dir, key: str, now=None) -> str:
    """Ask the desktop app to switch this agent to the saved account `key`: `.ag2-switch-request.json` in
    its config dir, owner-only and whole (written aside, then renamed in). The request's nonce."""
    nonce = str(uuid.uuid4())
    body = json.dumps({"v": 1, "key": key, "nonce": nonce, "at": int(time.time() if now is None else now)})
    path = Path(config_dir) / REQUEST
    tmp = path.with_name("%s.%s.tmp" % (REQUEST, nonce))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return nonce


def note(state: str, text: str):
    """A card with no buttons, shown in place of the account card the owner tapped."""
    agent_card, _ = connector_cards._core()
    return agent_card.build(account_switch.NOTE, state, title="Account", body="Account of %s" % NAME, text=text)


def one_line(text, limit: int = 300) -> str:
    return " ".join(str(text or "").split())[:limit]


class AccountCard(account_switch.AccountSwitch):
    """The account card, its "Switch account" pointing to the app instead of starting a sign-in here, and a
    "Use <address>" button for each other account the app saved for this agent."""

    def __init__(self, root, cfg: dict, publish, log, ask: str = "/account", run=None, popen=None):
        self.root, self.cfg = root, cfg
        self.popen = popen or _popen
        super().__init__(str(records.state_dir(root)), str(cb.bridge_state(cfg)), NAME, publish, quoted, log,
                         read=lambda: label(cfg, run=run), begin=None, cancel=None, opener=None, runtime=RUNTIME)
        self.ASK = ask
        self.seen: dict = {}

    def status(self) -> tuple:
        """The shared card's reading, keeping what it read of the account in use for `offers`."""
        SEEN.pop(str(self.cfg.get("CLAUDE_CONFIG_DIR") or ""), None)
        try:
            return super().status()
        finally:
            self.seen = SEEN.pop(str(self.cfg.get("CLAUDE_CONFIG_DIR") or ""), {})

    def offers(self) -> dict:
        """{key: address} the card offers: the saved accounts but the one in use, none when a switch here
        would be refused, sorted by address, at most USE_BUTTONS."""
        if self.seen.get("proxy"):
            return {}
        try:
            config_dir_for_switch(self.cfg)
        except accounts.Refused:
            return {}
        found = others(dict(self.cfg, email=self.seen.get("email") or ""))
        return dict(sorted(found.items(), key=lambda kv: kv[1].lower())[:USE_BUTTONS])

    def card(self, state: str, text: str, url: str = "", ask: bool = False, signed_out: bool = False):
        body, card = super().card(state, text, url, ask, signed_out)
        if not ask:
            return body, card
        agent_card, _ = connector_cards._core()
        uses = []
        for key, email in self.offers().items():
            words = ("Use " + email)[:agent_card.LIMITS["label"]]
            uses.append(agent_card.button(words, "reply", style="secondary", reply=words,
                                          command=agent_card.reply(self.KIND, "agent", key)))
        if uses:
            card = dict(card, buttons=uses + list(card.get("buttons") or []))
        return body, card

    def tapped(self, verb, sent, result_file) -> str:
        """"Switch account": where to switch it, shown in place of the card tapped and still carrying the
        controls, so opened on the agent's computer it is the account card again. Nothing is started here.
        A saved account's key: ask the app to switch to it (`use`)."""
        if verb == "switch":
            self.say(sent.get("channel_id"), account_switch.with_controls(self.card("info", WHERE), RUNTIME),
                     result_file, card_desk.tapped_event(sent))
            return "account-where"
        if KEY_RE.fullmatch(str(verb or "")):
            return self.use(verb, sent, result_file)
        self.publish(result_file, self.quoted(account_switch.EXPIRED_NO_CARD) + "\n")
        return "account-unknown"

    def offered(self, event: str, key: str) -> bool:
        """Whether the card at `event` is one this agent posted that offers `key` (its issued record)."""
        agent_card, issued_cards = connector_cards._core()
        entry = issued_cards.live(Path(self.root), event) if event else None
        return bool(entry) and agent_card.reply(self.KIND, "agent", key) in entry["offers"].values()

    def use(self, key, sent, result_file) -> str:
        """The owner tapped "Use <address>" on a card that offered it: write the app's switch request, show
        "Switching to <address>…" in place of the card, and leave the answer to the detached `await`."""
        room, tapped = str(sent.get("channel_id") or ""), card_desk.tapped_event(sent)
        if not self.offered(tapped, key):
            self.publish(result_file, self.quoted(UNMATCHED) + "\n")
            return "account-unknown"
        email = saved_accounts(self.cfg).get(key)
        if not email:
            self.say(room, note("error", NOT_SAVED), result_file, tapped)
            return "account-not-saved"
        try:
            config_dir = config_dir_for_switch(self.cfg)
            nonce = write_request(config_dir, key)
        except accounts.Refused as e:
            self.say(room, note("error", "Could not switch: %s" % e), result_file, tapped)
            return "account-refused"
        except OSError as e:
            self.log("switch request not written:", "%s: %s" % (type(e).__name__, str(e)[:200]))
            self.say(room, note("error", "Could not switch: the request for the app could not be written."),
                     result_file, tapped)
            return "account-not-asked"
        text = "Switching to %s…" % email
        if not self.watch(nonce, email, room, tapped, config_dir):
            text += " I could not start watching for the app's answer, so I won't confirm here."
        self.say(room, note("pending", text), result_file, tapped)
        return "account-switching"

    def watch(self, nonce, email, room, tapped, config_dir) -> bool:
        """Start `await` for this request, detached, its matter kept owner-only in the agent's state dir."""
        rec = {"nonce": nonce, "label": email, "room": room, "edit": tapped, "config_dir": str(config_dir),
               "bridge_state": self.bridge_state, "at": time.time()}
        folder = records.state_dir(self.root) / PENDING
        try:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = folder / (nonce + ".json")
            with os.fdopen(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                json.dump(rec, f)
            with open(records.state_dir(self.root) / "account-remote.log", "ab") as fh:
                self.popen([sys.executable, "-m", __name__, "await", "--root", str(self.root), "--nonce", nonce],
                           stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            self.log("switch answer not awaited:", "%s: %s" % (type(e).__name__, str(e)[:200]))
            return False
        return True


def answer(config_dir, nonce: str):
    """The app's answer to the request `nonce` ({ok, label, error}), taken (the file removed); None while
    there is none for it. An answer to another request is left for its own watcher."""
    path = Path(config_dir) / RESULT
    text, _ = read_regular(str(path), RESULT_MAX, follow=False)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("nonce") != nonce:
        return None
    path.unlink(missing_ok=True)
    return data


def told(data, label_: str) -> tuple:
    """(state, words) for the app's answer, `data` None when it did not answer in time."""
    if data is None:
        return "error", "Could not switch: %s" % NO_ANSWER
    if data.get("ok") is True:
        return "done", "Switched to %s." % (account_switch.shown(email_only(str(data.get("label") or ""))) or label_)
    return "error", "Could not switch: %s" % (one_line(data.get("error")) or "the app gave no reason")


def await_answer(root, nonce: str, *, sleep=time.sleep, clock=time.monotonic, wait_s: float = AWAIT_S, log=None) -> str:
    """The detached step: wait for the app's answer to request `nonce` (at most `wait_s`), take it, and say
    in place of the card tapped how it ended. What it said; '' for no such request."""
    log = log or (lambda *a: print("account-card:", *a, file=sys.stderr))
    if not NONCE_RE.fullmatch(str(nonce or "")):
        return ""
    path = records.state_dir(root) / PENDING / (nonce + ".json")
    text, _ = read_regular(str(path), RESULT_MAX, follow=False)
    try:
        rec = json.loads(text) if text else None
    except ValueError:
        rec = None
    if not isinstance(rec, dict):
        return ""
    end, data = clock() + wait_s, None
    while True:
        data = answer(rec["config_dir"], nonce)
        if data is not None or clock() >= end:
            break
        sleep(AWAIT_POLL_S)
    if data is None:        # unanswered: the request is withdrawn, so a late app does not switch unseen
        req = Path(rec["config_dir"]) / REQUEST
        got, _ = read_regular(str(req), RESULT_MAX, follow=False)
        try:
            if got and json.loads(got).get("nonce") == nonce:
                req.unlink(missing_ok=True)
        except (ValueError, AttributeError, OSError):
            pass
    state, words = told(data, str(rec.get("label") or "that account"))
    if not card_desk.post(rec.get("bridge_state") or "", rec.get("room") or "", note(state, words), log,
                          key="account-remote:" + nonce, at_ns=time.time_ns(), edit=str(rec.get("edit") or "")):
        log("answer not posted:", words)
    path.unlink(missing_ok=True)
    return words


def cards(root, cfg: dict, publish, log, run=None) -> list:
    """The account card once per way of asking for it ("/account", "/login")."""
    return [AccountCard(root, cfg, publish, log, ask=a, run=run) for a in ASKS]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="account_card", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("await")
    a.add_argument("--root", required=True)
    a.add_argument("--nonce", required=True)
    args = p.parse_args(argv)
    print(await_answer(args.root, args.nonce) or "no such request")
    return 0


if __name__ == "__main__":
    sys.exit(main())
