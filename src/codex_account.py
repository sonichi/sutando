#!/usr/bin/env python3
"""Which account a Codex home is signed in to: the email and plan, never a token.

`codex login status` only says "Logged in using ChatGPT", so a Codex core asked which account it is on
could not tell. This asks Codex itself first: a short-lived `codex app-server` under that CODEX_HOME,
`initialize`, then `account/read` (Codex 0.167: `{account: {type, email, planType, workspaceName}}`).
When that cannot run, it reads `<home>/auth.json` in this process and decodes only the `email` and
plan claims of its `id_token` (no signature check: the file is the home's own; nothing is sent
anywhere). Only the email, plan, workspace name and login kind are printed or returned; the file and
its tokens are never printed, logged or passed on, and the app-server's stderr is discarded.

    python3 src/codex_account.py [--codex-home DIR] [--codex PATH] [--json] [--no-app-server]
                                 [--task-file FILE]

Without `--codex-home` the home is the one the Codex core launcher uses (`codex_home.effective`:
the same resolution and desktop-host checks, read-only), and a home the launcher would refuse is
refused here too. Pool Codex workers run under the core's home today, so the same answer is theirs;
pass a worker's own home with `--codex-home` when it has one. A signed-out answer from Codex is
final; only when Codex cannot answer is the file read, and that answer is marked unverified.
With `--task-file` nothing is looked up unless the task is the owner's and was asked outside a room
other than the owner's DM (exit 3). Exit 0 an account was read, 1 signed out or unreadable.
Stdlib only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import shutil
import threading
from pathlib import Path
from typing import Optional

CLIENT = {"name": "sutando-codex-account", "title": "Sutando Codex account", "version": "1"}
PLAN_CLAIMS = "https://api.openai.com/auth"
FIELDS = ("email", "plan", "workspace", "kind", "source", "verified")


class NoHome(Exception):
    """The launcher would not use a Codex home here; the message says why."""


def home(repo: Optional[Path] = None) -> Path:
    """The Codex core's home as its launcher resolves and checks it, never creating or changing it."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import codex_home
    try:
        return Path(codex_home.effective(repo or Path(__file__).resolve().parents[1], Path.home()))  # lint-workspace-resolution: allow-repo-root
    except codex_home.Refused as exc:
        raise NoHome(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - an unreadable config gives the launcher no home either
        raise NoHome(f"the Codex home configuration cannot be read ({exc})") from None


def owner_dm_task(task_file: Path, workspace: Optional[Path] = None) -> Optional[str]:
    """None when this task may be told the owner's account; else why not."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from local_task_protocol import parse_task_headers
    try:
        headers = parse_task_headers(Path(task_file).read_text(encoding="utf-8")).headers
    except (OSError, ValueError) as exc:
        return f"the task file cannot be read ({exc})"
    tier = (headers.get("access_tier") or "owner").strip()
    if tier != "owner":
        return "the account is the owner's data; this task is not the owner's"
    room = (headers.get("channel_id") or headers.get("source_room_id") or "").strip()
    if not room:
        return None
    import owner_room_access
    if workspace is None:
        from workspace_default import resolve_workspace
        workspace = resolve_workspace(migrate=False)
    dm = owner_room_access.owner_dm(owner_room_access.owner_routing(Path(workspace)))
    if dm and room == dm:
        return None
    return "asked in a room that is not the owner's DM; answer it in the owner's DM"


def _answer(account: dict, source: str) -> dict:
    """Only the fields that may be shown, from an `account/read` account or decoded claims."""
    kind = str(account.get("type") or "")
    out = {"email": account.get("email") or None, "plan": account.get("planType") or None,
           "workspace": account.get("workspaceName") or None, "kind": kind or None, "source": source}
    return {k: (str(v) if v is not None else None) for k, v in out.items()}


def launch_path(repo: Optional[Path] = None, environ=None) -> str:
    """PATH as the Codex core launcher extends it: the bundled node, then the Codex install dir, appended."""
    env = os.environ if environ is None else environ
    repo = Path(repo or Path(__file__).resolve().parents[1])  # lint-workspace-resolution: allow-repo-root
    extra = [env.get("CODEX_NODE_BIN") or str(repo.parent / "runtime" / "node" / "bin"),
             env.get("CODEX_INSTALL_BIN") or os.path.expanduser("~/.npm-global/bin")]
    parts = [d for d in (env.get("PATH") or "").split(os.pathsep) if d]
    parts += [d for d in extra if d and os.path.isdir(d) and d not in parts]
    return os.pathsep.join(parts)


def from_app_server(codex_home: Path, codex: str = "codex", timeout: float = 20.0) -> Optional[dict]:
    """The account by Codex's own `account/read`, None when the app-server cannot answer. A signed-out
    home answers {"kind": None, ...} with no email."""
    path = launch_path()
    env = dict(os.environ, CODEX_HOME=str(codex_home), PATH=path)
    if os.sep not in codex:
        codex = shutil.which(codex, path=path) or codex
    try:
        p = subprocess.Popen([codex, "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
                             cwd=str(codex_home) if Path(codex_home).is_dir() else None)
    except OSError:
        return None
    result: dict = {}

    def talk():
        try:
            for rid, method, params in ((1, "initialize", {"clientInfo": CLIENT, "capabilities": {}}),
                                        (None, "initialized", {}),
                                        (2, "account/read", {"refreshToken": False})):
                msg = {"jsonrpc": "2.0", "method": method, "params": params}
                if rid is not None:
                    msg["id"] = rid
                p.stdin.write(json.dumps(msg) + "\n")
                p.stdin.flush()
                if rid is None:
                    continue
                for line in p.stdout:
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(m, dict) and m.get("id") == rid and "method" not in m:
                        if "error" in m:
                            return
                        if rid == 2:
                            result["r"] = m.get("result") or {}
                        break
                else:
                    return
        except (OSError, ValueError):
            return

    t = threading.Thread(target=talk, daemon=True)
    t.start()
    t.join(timeout)
    try:
        p.kill()
        p.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - a child that will not die
        pass
    for pipe in (p.stdin, p.stdout):
        try:
            pipe.close()
        except (OSError, ValueError):  # pragma: no cover - a pipe already broken
            pass
    r = result.get("r")
    if not isinstance(r, dict):
        return None
    account = r.get("account")
    out = _answer(account if isinstance(account, dict) else {}, "app-server")
    out["verified"] = True
    return out


def _claims(jwt: str) -> dict:
    """The payload of a JWT, decoded without verification; {} when it is not one."""
    try:
        payload = jwt.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def from_auth_file(codex_home: Path) -> Optional[dict]:
    """The account from `<home>/auth.json`'s id_token claims, None when there is no readable login."""
    try:
        auth = json.loads((Path(codex_home) / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(auth, dict):
        return None
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    id_token = tokens.get("id_token")
    if isinstance(id_token, dict):  # some Codex versions keep the parsed token beside the raw one
        id_token = id_token.get("raw_jwt") or ""
    claims = _claims(id_token) if isinstance(id_token, str) else {}
    if claims:
        auth_claims = claims.get(PLAN_CLAIMS) if isinstance(claims.get(PLAN_CLAIMS), dict) else {}
        out = _answer({"type": "chatgpt", "email": claims.get("email"),
                       "planType": auth_claims.get("chatgpt_plan_type")}, "auth.json")
    elif auth.get("OPENAI_API_KEY"):
        out = _answer({"type": "apiKey"}, "auth.json")
    else:
        return None
    out["verified"] = False
    return out


def read(codex_home: Path, codex: str = "codex", app_server: bool = True) -> Optional[dict]:
    """The signed-in account of `codex_home`: Codex's own answer, signed out included; the auth file's
    claims, unverified, only when Codex cannot answer."""
    if app_server:
        got = from_app_server(codex_home, codex)
        if got is not None:
            return got
    return from_auth_file(codex_home)


def words(acct: Optional[dict], codex_home: Path) -> str:
    if not acct or not acct.get("kind"):
        return f"Codex is not signed in under {codex_home}."
    if acct["kind"] == "apiKey":
        note = "" if acct.get("verified") else " (from its saved sign-in file)"
        return f"Codex under {codex_home} uses an API key (no ChatGPT account){note}."
    who = acct.get("email") or "an account whose email Codex does not report"
    plan = f", {acct['plan']} plan" if acct.get("plan") else ""
    ws = f", workspace {acct['workspace']}" if acct.get("workspace") else ""
    note = "" if acct.get("verified") else " (from its saved sign-in file; Codex itself did not confirm it)"
    return f"Codex under {codex_home} is signed in as {who}{plan}{ws}{note}."


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="codex_account.py", description=__doc__.splitlines()[0])
    ap.add_argument("--codex-home", default="", help="default: the Codex core's home")
    ap.add_argument("--codex", default="codex", help="the codex binary")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-app-server", action="store_true", help="read only the auth file's claims")
    ap.add_argument("--task-file", default="", help="the task asking; looked up only for the owner, outside shared rooms")
    a = ap.parse_args(argv)
    if a.task_file:
        why = owner_dm_task(Path(a.task_file))
        if why:
            print(f"Not looked up: {why}.")
            return 3
    try:
        where = Path(os.path.expanduser(a.codex_home)) if a.codex_home else home()
    except NoHome as exc:
        print(f"No Codex home to read: {exc}.")
        return 1
    acct = read(where, a.codex, app_server=not a.no_app_server)
    if a.json:
        print(json.dumps({"codex_home": str(where), **{k: (acct or {}).get(k) for k in FIELDS}}, sort_keys=True))
    else:
        print(words(acct, where))
    return 0 if acct and acct.get("kind") else 1


if __name__ == "__main__":
    sys.exit(main())
