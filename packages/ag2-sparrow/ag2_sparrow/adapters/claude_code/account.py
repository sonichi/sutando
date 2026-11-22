"""claude-local `account`: which Anthropic account an agent's session runs on, and signing it in again.

Both are Claude Code's own: `claude auth status --json` names the account of a config dir, and
`claude auth login` signs that dir in (it opens the browser of the computer it runs on and waits).
No API key is involved anywhere here.

A sign-in can be done in two steps, for a person who is not at that computer: `start` leaves Claude
Code's sign-in waiting and returns its link, and `give_code` hands it the code the sign-in page showed
them. The waiting sign-in belongs to a small detached process (`supervise`), which ends it after
LOGIN_WAIT_S; the code goes to it through a FIFO only its owner can open, and is never logged.

Two agents are not signed in again by this. One with no config dir of its own: the dir is then the
person's own Claude Code login, and a sign-in there would change what they run by hand. And one
whose requests go through the credential proxy (ANTHROPIC_BASE_URL): the proxy replaces the
session's credential with its own, so its sign-in, not this agent's, decides the account. Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

LOGIN_WAIT_S = 600.0        # how long a sign-in in the browser may take
STATUS_WAIT_S = 30.0
LINK_WAIT_S = 20.0          # how long Claude Code may take to print its sign-in link
CODE_WAIT_S = 90.0          # how long it may take to accept or refuse a code
LINK_RE = re.compile(r"https://[^\s\x07\x1b]+/oauth/authorize\?[^\s\x07\x1b]+")
CODE_RE = re.compile(r"[A-Za-z0-9_\-#.~+/=]{8,2048}")
ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]")
NOT_WAITING = "no sign-in is waiting for a code; start one first"
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


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def waiting(state_dir) -> bool:
    """Whether a started sign-in is still waiting for its code."""
    d = Path(state_dir)
    return not (d / "result.json").exists() and _alive(_read_json(d / "pids.json").get("supervisor"))


def cancel(state_dir) -> bool:
    """End a waiting sign-in and forget it; whether there was one."""
    d = Path(state_dir)
    pids = _read_json(d / "pids.json")
    for pid in (pids.get("claude"), pids.get("supervisor")):
        if _alive(pid) and int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
    there = d.exists()
    shutil.rmtree(d, ignore_errors=True)
    return there


def start(cfg: dict, env: dict, state_dir, console: bool = False, popen=subprocess.Popen, wait_s: float = LINK_WAIT_S,
          sleep=time.sleep) -> str:
    """Leave Claude Code's sign-in for the agent's config dir waiting for its code, and return the link
    of its sign-in page. An earlier waiting sign-in is ended first. Refused as `sign_in` is."""
    own_dir(cfg)
    if env.get(PROXY_VAR):
        raise Refused(PROXIED)
    d = Path(state_dir)
    cancel(d)
    d.mkdir(parents=True, mode=0o700)
    os.mkfifo(str(d / "code"), 0o600)
    (d / "out.log").touch(mode=0o600)
    child = dict(env, PYTHONPATH=str(Path(__file__).resolve().parents[3]))
    try:
        popen([sys.executable, "-m", __name__, "supervise", str(d), cfg["CLAUDE"], "console" if console else "claudeai"],
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child,
              start_new_session=True)
    except (OSError, subprocess.SubprocessError) as e:
        cancel(d)
        raise Refused("the sign-in could not be started (%s)" % type(e).__name__)
    end = time.time() + wait_s
    while True:
        link = LINK_RE.search(ANSI_RE.sub("", (d / "out.log").read_text(encoding="utf-8", errors="replace")))
        if link:
            return link.group(0)
        if (d / "result.json").exists() or time.time() >= end:
            cancel(d)
            raise Refused("Claude Code's sign-in gave no link; nothing is waiting")
        sleep(0.2)


def give_code(state_dir, code: str, wait_s: float = CODE_WAIT_S, sleep=time.sleep) -> "tuple[bool, str]":
    """Hand the waiting sign-in the code its page showed. (ok, why not); the sign-in is over either way."""
    d = Path(state_dir)
    code = (code or "").strip()
    if not CODE_RE.fullmatch(code):
        return False, "that does not look like a sign-in code; copy the whole code from the sign-in page"
    if not waiting(d):
        cancel(d)
        return False, NOT_WAITING
    try:
        fd = os.open(str(d / "code"), os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        cancel(d)
        return False, NOT_WAITING
    try:
        os.write(fd, code.encode("ascii") + b"\n")
    finally:
        os.close(fd)
    end = time.time() + wait_s
    while not (d / "result.json").exists() and time.time() < end:
        sleep(0.2)
    result = _read_json(d / "result.json")
    said = ANSI_RE.sub("", (d / "out.log").read_text(encoding="utf-8", errors="replace")) if (d / "out.log").exists() else ""
    cancel(d)
    if result.get("exit") == 0:
        return True, ""
    refused = [ln[ln.index("Couldn't log in"):].strip() for ln in said.splitlines() if "Couldn't log in" in ln]
    if refused:
        return False, refused[-1][:300]
    return False, ("the sign-in did not finish in time" if not result else
                   "Claude Code's sign-in ended with exit code %s" % result.get("exit"))


def supervise(state_dir: str, claude: str, how: str, wait_s: float = LOGIN_WAIT_S) -> int:
    """The detached owner of one waiting sign-in: run it with the FIFO as its input, end it after
    `wait_s`, and record how it ended."""
    d = Path(state_dir)
    fifo = os.open(str(d / "code"), os.O_RDWR)      # read-write: the FIFO never reports end of input
    with open(d / "out.log", "ab") as log:
        p = subprocess.Popen([claude, "auth", "login"] + (["--console"] if how == "console" else []), stdin=fifo,
                             stdout=log, stderr=subprocess.STDOUT)
        (d / "pids.json").write_text(json.dumps({"supervisor": os.getpid(), "claude": p.pid}))
        try:
            code = p.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            code = "timeout"
    os.close(fifo)
    if d.exists():
        (d / "result.json.tmp").write_text(json.dumps({"exit": code}))
        os.replace(str(d / "result.json.tmp"), str(d / "result.json"))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "supervise":
        sys.exit(supervise(sys.argv[2], sys.argv[3], sys.argv[4]))
    sys.exit(2)
