"""Adding a Codex account to a local Codex agent from the owner's phone: Codex's own device-code sign-in,
into a staging home, used only once the owner confirms.

The browser sign-in the account card starts (`account/login/start`) only completes in a browser on the
computer the agent runs on. `codex login --device-auth` instead prints a link and a one-time code, and
polls OpenAI itself until the person, on any device, has signed in at the link and entered the code; the
token then goes from OpenAI to the CLI on this computer. Nothing secret travels through a room: the card
carries the link (its domain spelled out) and the code, which is short-lived and useless without the
person's own sign-in.

The sign-in runs with CODEX_HOME set to a staging home, a new owner-only dir inside this sign-in's state,
never the agent's live one, so the agent keeps working on its current account throughout. When Codex
exits 0 the card (shown in place of the one that carried the link and code, so neither stays on screen)
names the staged account's email, read from its id token's email claim, and asks Yes or No. Yes moves the
staged login into the live CODEX_HOME in one rename, the earlier login kept beside it until that rename
has succeeded; No, or no answer within LOGIN_WAIT_S, signs the staged login out (`codex logout` on the
staging home) and deletes it. Only the first answer counts: the sign-in leaves the "confirm" phase before
anything is done, and an answer must reply to the very card that asked.

Every tap is the owner's, in the owner's DM, on a card this agent posted (agent_card.click_verdict,
re-checked here); "Add account" also only on an account card posted within TAP_FRESH_S. One sign-in at a
time per agent: a second tap while one runs shows the same card again, and every card that showed the link
and code is withdrawn when the sign-in ends. The sign-in belongs to a small detached process (`supervise`),
which ends it after LOGIN_WAIT_S. Ending it signals Codex, its descendants and the supervisor by exact pid,
each start time checked first (SIGTERM, then SIGKILL after STOP_WAIT_S); a process that has left Codex's
tree by then is not found. The code is kept only in the sign-in's owner-only state, said nowhere but the
card, and dropped when Codex exits.

Refused, as Claude Code's remote sign-in is, for an agent whose Codex home is not its own: none set
(Codex then uses ~/.codex), ~/.codex itself, a login file that is a link into another home (the core's,
on a desktop install), or a home another installed agent names. Stdlib only.
"""
from __future__ import annotations

import errno
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

from ... import account_switch, card_desk, connector_cards, detached_login, signin_watch
from ...detached_login import ANSI_RE, born, locked, read_json, write_private

# The owner's time to finish signing in, and then to answer Yes or No; "Add account" acts only on a card
# posted within TAP_FRESH_S; past LOGIN_WAIT_S + GRACE_S with no word from the supervisor it is taken as gone.
LOGIN_WAIT_S, TAP_FRESH_S, GRACE_S = 300.0, 300.0, 30.0
LINK_WAIT_S, LOGOUT_WAIT_S, STOP_WAIT_S = 20.0, 30.0, 5.0
STATE = "codex-device-login"
# Card edits still owed (room, event, the replacement's kind), kept apart from the sign-in: never a code or link.
WITHDRAW_FILE = "codex-device-withdraw.json"
STAGING = "home"
DEVICE_CARD, WITHDRAWN_CARD = "account-device", "account-device-withdrawn"
ADD, USE, DISCARD, SIGN_OUT = "add", "use", "discard", "signout"
VERBS = (ADD, USE, DISCARD, SIGN_OUT)
CODE_PHASE, CONFIRM_PHASE, SETTLING = "code", "confirm", "settling"
LINK_HOSTS = ("openai.com", "chatgpt.com")
LINK_RE = re.compile(r"https://[^\s\x07\x1b<>\"]+")
CODE_RE = re.compile(r"[A-Z0-9]{3,12}(?:-[A-Z0-9]{3,12}){0,3}")
CODE_LINE = re.compile(r"one-time code|enter (?:this|the) code", re.I)

SHARED = ("this agent has no Codex home of its own (install --codex-home), so signing in here would change your own "
          "Codex login; nothing was done")
PERSONAL = ("this agent's Codex home is your own Codex login (~/.codex): signing in here would change what you run "
            "by hand, so nothing was done; give the agent a home of its own (install --codex-home)")
CORES = ("this agent's Codex login is linked to another home's (the core's): signing in here would change that "
         "login too, so nothing was done")
ANOTHER_AGENTS = ("this agent shares its Codex home with another agent: signing in here would change that agent's "
                  "account too, so nothing was done")
NOT_OWNER = "Only the owner can add an account, and only from our direct message."
STALE_CARD = "That card is too old to start a sign-in, so nothing changed. Send /account for a new one."
NO_CARD = "I couldn't show the sign-in card here, so I stopped the sign-in. Send /account in our direct message."
UNANSWERED = "You didn't answer within %d minutes, so I signed that account out again. %s"
ANSWERED_ALREADY = "That sign-in was already answered or is over, so nothing changed."


class Refused(Exception):
    """Nothing was changed; the message is the one line for the owner."""


def _real(path) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _user_home() -> Path:
    return Path(os.path.expanduser("~"))


def installed_homes() -> list:
    """The Codex home every installed codex-local job names, this one's included. Read here, not through
    connect_common, which reaches the gateway credentials this connector never needs."""
    found = []
    try:
        paths = sorted((_user_home() / ".config" / "ag2-sparrow").rglob("*.conf"))
    except OSError:
        return found
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith("CODEX_HOME="):
                try:
                    home = "".join(shlex.split(line.partition("=")[2]))
                except ValueError:
                    continue
                if home:
                    found.append(home)
    return found


def own_home(env: dict, homes=()) -> str:
    """The agent's own CODEX_HOME; Refused when signing in there would change a login that is not this
    agent's alone. `homes` are the homes of every installed job: this one names its home once."""
    if not env.get("CODEX_HOME"):
        raise Refused(SHARED)
    mine = _real(env["CODEX_HOME"])
    if mine == _real(_user_home() / ".codex"):
        raise Refused(PERSONAL)
    if os.path.islink(os.path.join(mine, signin_watch.CODEX_AUTH)):
        raise Refused(CORES)
    if sum(1 for h in homes if _real(h) == mine) > 1:
        raise Refused(ANOTHER_AGENTS)
    return env["CODEX_HOME"]


def link_ok(url) -> bool:
    """A plain https page of OpenAI's own: the only link a sign-in card hands the owner."""
    if not account_switch.page_ok(url):
        return False
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in LINK_HOSTS)


def parse(text: str) -> "tuple[str, str]":
    """(link, code) as Codex printed them, colours removed; '' for either not printed (yet). The code is
    the first thing after the line that asks for it: on that line after a colon, else the next line."""
    lines = [ln.strip() for ln in ANSI_RE.sub("", text or "").splitlines()]
    link = next((m.group(0).rstrip(".,)") for ln in lines for m in LINK_RE.finditer(ln) if link_ok(m.group(0).rstrip(".,)"))), "")
    code = ""
    for i, ln in enumerate(lines):
        if not CODE_LINE.search(ln):
            continue
        after = [ln.rpartition(":")[2].strip()] if ":" in ln else []
        after += [x for x in lines[i + 1:] if x][:1]
        code = next((x for x in after if CODE_RE.fullmatch(x)), "")
        if code:
            break
    return link, code


def _alive(d: Path) -> bool:
    return not (d / "result.json").exists() and bool(detached_login.ours(read_json(d / "pids.json").get("supervisor")))


def _stop(d: Path) -> None:
    """End the sign-in's processes and drop what Codex printed (the code with it)."""
    pids = read_json(d / "pids.json")
    entries = [pids.get("codex"), pids.get("supervisor")]
    for entry in list(entries):
        pid = detached_login.ours(entry)
        if pid:
            entries += detached_login.descendants(pid)
    detached_login.stop_all(entries, STOP_WAIT_S)
    for name in ("out.log", "pids.json"):
        try:
            (d / name).unlink()
        except OSError:
            pass


def _staged_auth(d: Path) -> Path:
    return d / STAGING / signin_watch.CODEX_AUTH


def _discard(d: Path, codex: str, env: dict, run) -> bool:
    """Stop the sign-in, sign a staged login out on the staging home only, and delete it all; whether
    there was anything. A logout that fails still deletes the staged login."""
    if not d.exists():
        return False
    _stop(d)
    if _staged_auth(d).is_file():
        try:
            run([codex, "logout"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=dict(env, CODEX_HOME=os.path.abspath(d / STAGING)), timeout=LOGOUT_WAIT_S)
        except (OSError, subprocess.SubprocessError):
            pass
    shutil.rmtree(d, ignore_errors=True)
    return True


def current(state_dir) -> dict:
    """The sign-in in progress: its meta ({id, phase, at, room, edit, and link and code or email}), {}
    when there is none."""
    d = Path(state_dir)
    meta = read_json(d / "meta.json")
    if meta.get("phase") == CODE_PHASE and meta.get("code") and _alive(d):
        return meta
    return meta if meta.get("phase") == CONFIRM_PHASE else {}


def start(codex: str, env: dict, state_dir, popen=subprocess.Popen, run=subprocess.run, wait_s: float = LINK_WAIT_S,
          sleep=time.sleep, homes=(), login_wait_s=None) -> "tuple[dict, bool]":
    """(the sign-in's meta, whether it was already running) of Codex's device-code sign-in into a staging
    home. Refused as `own_home` is, and when Codex printed no usable link and code."""
    own_home(env, homes)
    with locked(state_dir):
        d = Path(state_dir)
        now = current(d)
        if now:
            return now, True
        _discard(d, codex, env, run)
        return _start(codex, env, d, popen, wait_s, sleep, LOGIN_WAIT_S if login_wait_s is None else login_wait_s), False


def _start(codex, env, d, popen, wait_s, sleep, login_wait_s):
    d.mkdir(parents=True, mode=0o700)
    (d / STAGING).mkdir(mode=0o700)
    (d / "out.log").touch(mode=0o600)
    child = dict(env, CODEX_HOME=os.path.abspath(d / STAGING), PYTHONPATH=str(Path(__file__).resolve().parents[3]))
    try:
        popen([sys.executable, "-m", __name__, "supervise", str(d), codex, str(float(login_wait_s))],
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child,
              start_new_session=True)
    except (OSError, subprocess.SubprocessError) as e:
        shutil.rmtree(d, ignore_errors=True)
        raise Refused("Codex's sign-in could not be started (%s)" % type(e).__name__)
    end = time.time() + wait_s
    while True:
        link, code = parse((d / "out.log").read_text(encoding="utf-8", errors="replace"))
        # Not before its processes are on record: an end right after must find them to stop them.
        if link and code and read_json(d / "pids.json"):
            meta = {"id": uuid.uuid4().hex[:12], "phase": CODE_PHASE, "link": link, "code": code, "at": time.time()}
            write_private(d / "meta.json", meta)
            return meta
        if (d / "result.json").exists() or time.time() >= end:
            _stop(d)
            shutil.rmtree(d, ignore_errors=True)
            raise Refused("Codex's sign-in gave no link and code, so nothing is waiting")
        sleep(0.2)


def note_card(state_dir, room: str, edit: str) -> None:
    """Where the sign-in's next word is shown (the room, and the card it replaces), and one more card that
    shows it, to withdraw when it is over."""
    d = Path(state_dir)
    with locked(d):
        meta = read_json(d / "meta.json")
        if meta:
            cards = [c for c in meta.get("cards") or [] if c != edit] + ([edit] if edit else [])
            write_private(d / "meta.json", dict(meta, room=room, edit=edit, cards=cards))


def over(state_dir, codex: str, env: dict, run=subprocess.run, now=None, wait_s=None) -> dict:
    """What became of the sign-in since last asked, {} for nothing new: {phase: "confirm", email} once Codex
    signed the staging home in; {exit, why} when it failed, was ended for taking too long ("timeout") or its
    supervisor is gone ("lost"); {exit: "unanswered"} when nobody said Yes or No in time. Each with the
    room and the card to show it in place of."""
    d = Path(state_dir)
    if not d.exists():
        return {}       # asked on every pass of the connector's loop: no lock taken for nothing
    now = time.time() if now is None else now
    wait_s = LOGIN_WAIT_S if wait_s is None else wait_s
    with locked(d):
        meta = read_json(d / "meta.json")
        where = {"room": meta.get("room") or "", "edit": meta.get("edit") or "", "cards": meta.get("cards") or []}
        if meta.get("phase") == CONFIRM_PHASE:
            if now - float(meta.get("asked_at") or 0) <= wait_s:
                return {}
            _discard(d, codex, env, run)
            return dict(where, exit="unanswered")
        result = read_json(d / "result.json")
        late = now - float(meta.get("at") or 0) > wait_s + GRACE_S
        if meta.get("phase") == CODE_PHASE and not result and _alive(d) and not late:
            return {}
        said = (d / "out.log").read_text(encoding="utf-8", errors="replace") if (d / "out.log").exists() else ""
        _stop(d)
        code = result.get("exit", "lost" if not late else "timeout")
        found = signin_watch.codex_identity(str(d / STAGING)) if code == 0 and _staged_auth(d).is_file() else None
        email = account_switch.shown(found[1]) if found else ""
        if email:
            write_private(d / "meta.json", {"id": meta.get("id"), "phase": CONFIRM_PHASE, "email": email,
                                            "asked_at": now, **where})
            return dict(where, phase=CONFIRM_PHASE, email=email)
        _discard(d, codex, env, run)
    why = _why(said, meta.get("code") or "", meta.get("link") or "") if code != 0 else \
        "Codex finished, but I could not read which account it signed in"
    return dict(where, exit=code, why=why)


def _why(said: str, code: str, link: str) -> str:
    """Codex's last line, for a sign-in that failed; never one that carries the code or the link."""
    lines = [ln.strip() for ln in ANSI_RE.sub("", said).splitlines() if ln.strip()]
    last = next((ln for ln in reversed(lines) if not (code and code in ln) and not (link and link in ln)), "")
    return last[:160]


def answer(state_dir, event: str, use: bool, codex: str, env: dict, run=subprocess.run, homes=(), now=None,
           wait_s=None) -> "tuple[str, str, list]":
    """The owner's Yes or No to the staged account, from the card `event` that asked; only the first answer
    counts, and only within LOGIN_WAIT_S of the question, checked here whatever the connector's loop has
    done. (outcome, the email or why not, every card that showed this sign-in): "used", "discarded",
    "not-used", "expired" or "stale"."""
    d = Path(state_dir)
    now = time.time() if now is None else now
    with locked(d):
        meta = read_json(d / "meta.json")
        cards = meta.get("cards") or []
        if meta.get("phase") != CONFIRM_PHASE or not event or event != meta.get("edit"):
            return "stale", "", []
        write_private(d / "meta.json", dict(meta, phase=SETTLING))      # first answer only, before anything is done
        if now - float(meta.get("asked_at") or 0) > (LOGIN_WAIT_S if wait_s is None else wait_s):
            _discard(d, codex, env, run)
            return "expired", meta["email"], cards
        if not use:
            _discard(d, codex, env, run)
            return "discarded", meta["email"], cards
        try:
            live = own_home(env, homes)
            swap(_staged_auth(d), live)
        except (Refused, OSError) as e:
            _discard(d, codex, env, run)
            why = str(e) if isinstance(e, Refused) else "the login could not be moved (%s)" % type(e).__name__
            return "not-used", why, cards
        shutil.rmtree(d, ignore_errors=True)
        return "used", meta["email"], cards


def swap(staged: Path, live_home) -> None:
    """Move the staged login into `live_home` in one rename; the earlier login is kept beside it until that
    rename has succeeded. The login is moved as a file, never read here. Raises OSError, the live login
    left as it was."""
    if staged.is_symlink() or not staged.is_file():
        raise OSError(errno.ENOENT, "no staged login")
    live = Path(live_home) / signin_watch.CODEX_AUTH
    tmp = Path(live_home) / (".%s.adding-%s" % (signin_watch.CODEX_AUTH, uuid.uuid4().hex[:8]))
    previous = Path(live_home) / (signin_watch.CODEX_AUTH + ".previous")
    try:
        os.replace(str(staged), str(tmp))
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.copyfile(str(staged), str(tmp))      # another file system: copied, then renamed into place
    try:
        os.chmod(str(tmp), 0o600)
        if live.exists():
            if previous.exists():
                previous.unlink()
            os.link(str(live), str(previous))
        os.replace(str(tmp), str(live))
    except OSError:
        if tmp.exists():
            tmp.unlink()
        raise
    finally:
        if previous.exists() and not tmp.exists():
            previous.unlink()


def cancel(state_dir, codex: str = "codex", env=None, run=subprocess.run) -> dict:
    """End and forget the sign-in: {room, cards} of the one there was ({} for none), its cards to withdraw."""
    d = Path(state_dir)
    with locked(d):
        meta = read_json(d / "meta.json")
        if not _discard(d, codex, dict(env or {}), run):
            return {}
        return {"room": meta.get("room") or "", "cards": meta.get("cards") or []}


def sign_out(codex: str, env: dict, run=subprocess.run, homes=(), wait_s: float = LOGOUT_WAIT_S) -> "tuple[bool, str]":
    """`codex logout` for the agent's own home. (ok, why not). Refused as `own_home` is."""
    own_home(env, homes)
    try:
        r = run([codex, "logout"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, timeout=wait_s)
    except (OSError, subprocess.SubprocessError) as e:
        return False, "codex logout did not run (%s)" % type(e).__name__
    return (True, "") if r.returncode == 0 else (False, "codex logout ended with exit code %d" % r.returncode)


def supervise(state_dir: str, codex: str, wait_s: float = LOGIN_WAIT_S) -> int:
    """The detached owner of one device-code sign-in: run it, end it and its descendants after `wait_s`
    (asked first, then killed, each by exact pid), and record how it ended."""
    d = Path(state_dir)
    with open(d / "out.log", "ab") as log:
        p = subprocess.Popen([codex, "login", "--device-auth"], stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT)
        write_private(d / "pids.json", {"supervisor": [os.getpid(), born(os.getpid())], "codex": [p.pid, born(p.pid)]})
        try:
            code = p.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            # Its descendants are listed before it goes: once it has, they no longer hang under it.
            detached_login.stop_all([[p.pid, born(p.pid)]] + detached_login.descendants(p.pid), STOP_WAIT_S)
            p.wait()
            code = "timeout"
    if d.exists():
        write_private(d / "result.json", {"exit": code})
    return 0


class Remote:
    """The account card's "Add account", its Yes and No, and "Sign out" for local Codex: `accounts` is the
    card (account_switch.AccountSwitch), `root` the work root holding the agent's card ledger, `codex` the
    executable, `env` the CLI's environment (its live CODEX_HOME)."""

    def __init__(self, accounts, state_dir, root, codex, env, log, homes=None, popen=subprocess.Popen,
                 run=subprocess.run):
        self.accounts, self.dir, self.root = accounts, Path(state_dir) / STATE, Path(root)
        self.codex, self.env, self.log = codex, env, log
        self.homes, self.popen, self.run = homes or (lambda: installed_homes()), popen, run

    def busy(self) -> bool:
        return bool(current(self.dir))

    def stop(self) -> None:
        was = cancel(self.dir, self.codex, self.env, self.run)
        if was:
            self.withdraw(was["room"], was["cards"])
            self.log("device sign-in ended: another sign-in was started")

    def withdraw(self, room, cards, keep="") -> None:
        """Show every card of this sign-in but `keep` (which gets its outcome) without the link or code. An
        edit that cannot be queued now is kept owed and tried again on each upkeep (`retry`)."""
        owed = self.owed() + [{"room": room, "event": e, "kind": WITHDRAWN_CARD} for e in cards if e != keep and room]
        self._keep_owed(owed)
        self.retry()

    def owed(self) -> list:
        data = read_json(self.dir.parent / WITHDRAW_FILE).get("owed")
        return [o for o in data if isinstance(o, dict) and o.get("room") and o.get("event")] if isinstance(data, list) else []

    def _keep_owed(self, owed) -> None:
        path = self.dir.parent / WITHDRAW_FILE
        if owed:
            write_private(path, {"owed": owed})
        elif path.exists():
            path.unlink()

    def retry(self) -> int:
        """Queue each owed withdrawal; those that still cannot be are kept. How many are left."""
        agent_card, _ = connector_cards._core()
        gone = agent_card.build(WITHDRAWN_CARD, "expired", title="Account", body="Account of %s" % self.accounts.name,
                                text="This sign-in card is no longer in use.")
        owed = self.owed()
        left = [o for o in owed if not self.accounts.post(o["room"], gone, edit=o["event"])]
        for o in left:
            self.log("device sign-in card not withdrawn yet:", str(o["event"])[:40])
        if left != owed:
            self._keep_owed(left)
        return len(left)

    def tapped(self, verb, sent, result_file) -> str:
        agent_card, _ = connector_cards._core()
        if agent_card.owner_may_act(sent) is not None or card_desk.in_room(sent):
            self.accounts.publish(result_file, self.accounts.quoted(NOT_OWNER) + "\n")
            return "account-refused"
        if verb == ADD:
            return self.add(sent, result_file)
        if verb in (USE, DISCARD):
            return self.answer(verb == USE, sent, result_file)
        return self.sign_out(sent, result_file)

    def card(self, state, text, buttons=(), expires_at=None):
        agent_card, _ = connector_cards._core()
        return agent_card.build(DEVICE_CARD, state, title="Account", body="Account of %s" % self.accounts.name,
                                text=text, **({"buttons": list(buttons)} if buttons else {}),
                                **({"expires_at_ms": int(expires_at * 1000)} if expires_at else {}))

    def _reply(self, label, verb):
        agent_card, _ = connector_cards._core()
        return agent_card.button(label, "reply", reply=label, disabled=False,
                                 command=agent_card.reply("account", "agent", verb))

    def code_card(self, meta):
        host = urllib.parse.urlsplit(meta["link"]).hostname
        agent_card, _ = connector_cards._core()
        return self.card("pending", "To add an account, open the sign-in page at %s on this phone or any device, sign "
                                    "in, then enter this one-time code: %s. It works for %d minutes. I'll ask you here "
                                    "before I use the account; until then I keep using the one I have."
                         % (host, meta["code"], int(LOGIN_WAIT_S // 60)),
                         [agent_card.button("Open %s" % host, "url", url=meta["link"])],
                         expires_at=float(meta["at"]) + LOGIN_WAIT_S)

    def confirm_card(self, email):
        return self.card("pending", "Signed in as %s. Use this account for %s? %s" % (email, self.accounts.name,
                                                                                    self.accounts.using()),
                         [self._reply("Yes", USE), self._reply("No", DISCARD)])

    def fresh(self, sent) -> bool:
        """Whether the tapped account card was posted within TAP_FRESH_S."""
        _, issued_cards = connector_cards._core()
        entry = issued_cards.live(self.root, card_desk.tapped_event(sent))
        return bool(entry) and time.time() - float(entry.get("issued_at") or 0) <= TAP_FRESH_S

    def add(self, sent, result_file) -> str:
        """Start Codex's device-code sign-in into a staging home (or find the one running) and show its link
        and code in place of the card tapped. The code goes into the card only: where no card can be shown,
        the sign-in is ended."""
        room, tapped = str(sent.get("channel_id") or ""), card_desk.tapped_event(sent)
        if not self.fresh(sent):
            self.accounts.publish(result_file, self.accounts.quoted(STALE_CARD) + "\n")
            return "account-card-stale"
        waiting = self.accounts.pending()
        if waiting:
            self.accounts.give_up(waiting)      # one sign-in at a time: the browser one on this computer ends
        try:
            meta, again = start(self.codex, self.env, self.dir, popen=self.popen, run=self.run, homes=self.homes())
        except Refused as e:
            self.log("device sign-in not started:", str(e)[:200])
            self.accounts.say(room, self.accounts.card("error", "I could not start a sign-in: %s." % e, ask=True),
                              result_file, tapped)
            return "account-not-started"
        built = self.confirm_card(meta["email"]) if meta["phase"] == CONFIRM_PHASE else self.code_card(meta)
        if not self.accounts.post(room, built, edit=tapped):
            was = cancel(self.dir, self.codex, self.env, self.run)
            self.withdraw(was.get("room"), was.get("cards") or [])
            self.accounts.publish(result_file, self.accounts.quoted(NO_CARD) + "\n")
            return "account-not-started"
        note_card(self.dir, room, tapped)
        self.accounts.publish(result_file, account_switch.ANSWERED)
        self.log("device sign-in", "shown again" if again else "started")
        return "account-device-again" if again else "account-device-started"

    def answer(self, use, sent, result_file) -> str:
        room, tapped = str(sent.get("channel_id") or ""), card_desk.tapped_event(sent)
        outcome, said, cards = answer(self.dir, tapped, use, self.codex, self.env, run=self.run, homes=self.homes())
        if outcome == "stale":
            self.accounts.publish(result_file, self.accounts.quoted(ANSWERED_ALREADY) + "\n")
            return "account-device-stale"
        if outcome == "used":
            built = self.card("done", "Now using %s for %s." % (said, self.accounts.name), [self._reply("Sign out", SIGN_OUT)])
        elif outcome == "expired":
            built = self.accounts.card("error", UNANSWERED % (int(LOGIN_WAIT_S // 60), self.accounts.using()), ask=True)
        elif outcome == "discarded":
            built = self.accounts.card("pending", "Not added: I signed %s out again. %s" % (said, self.accounts.using()),
                                       ask=True)
        else:
            self.log("staged account not used:", said[:200])
            built = self.accounts.card("error", "I could not use that account: %s. %s" % (said, self.accounts.using()),
                                       ask=True)
        self.accounts.say(room, built, result_file, tapped)
        self.withdraw(room, cards, keep=tapped)
        self.log("device sign-in ->", outcome)
        return "account-device-" + outcome

    def sign_out(self, sent, result_file) -> str:
        room, tapped = str(sent.get("channel_id") or ""), card_desk.tapped_event(sent)
        try:
            ok, why = sign_out(self.codex, self.env, run=self.run, homes=self.homes())
        except Refused as e:
            ok, why = False, str(e)
        if ok:
            built = self.accounts.card("pending", "Signed out. I'm not signed in.", ask=True)
        else:
            self.log("sign-out failed:", why[:200])
            built = self.accounts.card("error", "I could not sign out: %s." % why, ask=True)
        self.accounts.say(room, built, result_file, tapped)
        return "account-signed-out" if ok else "account-not-signed-out"

    def tick(self, now=None) -> str:
        """What became of a sign-in, shown in place of its card: the link and code never stay on screen."""
        if self.owed():
            self.retry()
        done = over(self.dir, self.codex, self.env, run=self.run, now=now)
        if not done:
            return ""
        if done.get("phase") == CONFIRM_PHASE:
            built, outcome = self.confirm_card(done["email"]), "account-device-confirm"
        elif done["exit"] == "unanswered":
            built = self.accounts.card("error", UNANSWERED % (int(LOGIN_WAIT_S // 60), self.accounts.using()), ask=True)
            outcome = "account-device-unanswered"
        elif done["exit"] == "timeout":
            built = self.accounts.card("error", "The sign-in expired before it was finished, so I stopped it. Tap Add "
                                                "account to start again.", ask=True)
            outcome = "account-device-expired"
        else:
            why = done["why"] or "Codex's sign-in ended with exit code %s" % done["exit"]
            built = self.accounts.card("error", "The sign-in did not finish (%s). %s" % (why.rstrip("."), self.accounts.using()),
                                       ask=True)
            outcome = "account-device-failed"
        shown = bool(done["room"]) and self.accounts.post(done["room"], built, edit=done["edit"])
        if not shown:
            self.log("device sign-in outcome not shown:", outcome)      # its card is withdrawn instead
        self.withdraw(done["room"], done["cards"], keep=done["edit"] if shown else "")
        self.log("device sign-in ->", outcome)
        return outcome


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "supervise":
        sys.exit(supervise(sys.argv[2], sys.argv[3], float(sys.argv[4])))
    sys.exit(2)
