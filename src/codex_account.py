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

Without `--codex-home` the home is the Codex core's (the `core_config_dirs` entry of type codex,
else $CODEX_HOME, else ~/.codex). Pool Codex workers run under the core's home today, so the same
answer is theirs; pass a worker's own home with `--codex-home` when it has one.
Exit 0 an account was read, 1 signed out or unreadable. Stdlib only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

CLIENT = {"name": "sutando-codex-account", "title": "Sutando Codex account", "version": "1"}
PLAN_CLAIMS = "https://api.openai.com/auth"
FIELDS = ("email", "plan", "workspace", "kind", "source")


def home(repo: Optional[Path] = None) -> Path:
    """The Codex core's home, without creating it."""
    value = ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import codex_home
        value = codex_home.configured(repo or Path(__file__).resolve().parents[1])[1]  # lint-workspace-resolution: allow-repo-root
    except Exception:  # noqa: BLE001 - an unreadable config only means the default home
        value = ""
    return Path(os.path.expanduser(value or os.environ.get("CODEX_HOME") or "~/.codex"))


def _answer(account: dict, source: str) -> dict:
    """Only the fields that may be shown, from an `account/read` account or decoded claims."""
    kind = str(account.get("type") or "")
    out = {"email": account.get("email") or None, "plan": account.get("planType") or None,
           "workspace": account.get("workspaceName") or None, "kind": kind or None, "source": source}
    return {k: (str(v) if v is not None else None) for k, v in out.items()}


def from_app_server(codex_home: Path, codex: str = "codex", timeout: float = 20.0) -> Optional[dict]:
    """The account by Codex's own `account/read`, None when the app-server cannot answer. A signed-out
    home answers {"kind": None, ...} with no email."""
    env = dict(os.environ, CODEX_HOME=str(codex_home))
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
    return _answer(account if isinstance(account, dict) else {}, "app-server")


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
        return _answer({"type": "chatgpt", "email": claims.get("email"),
                        "planType": auth_claims.get("chatgpt_plan_type")}, "auth.json")
    if auth.get("OPENAI_API_KEY"):
        return _answer({"type": "apiKey"}, "auth.json")
    return None


def read(codex_home: Path, codex: str = "codex", app_server: bool = True) -> Optional[dict]:
    """The signed-in account of `codex_home`: Codex's own answer first, else the auth file's claims."""
    if app_server:
        got = from_app_server(codex_home, codex)
        if got is not None and got.get("kind"):
            return got
    return from_auth_file(codex_home)


def words(acct: Optional[dict], codex_home: Path) -> str:
    if not acct or not acct.get("kind"):
        return f"Codex is not signed in under {codex_home}."
    if acct["kind"] == "apiKey":
        return f"Codex under {codex_home} uses an API key (no ChatGPT account)."
    who = acct.get("email") or "an account whose email Codex does not report"
    plan = f", {acct['plan']} plan" if acct.get("plan") else ""
    ws = f", workspace {acct['workspace']}" if acct.get("workspace") else ""
    return f"Codex under {codex_home} is signed in as {who}{plan}{ws}."


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="codex_account.py", description=__doc__.splitlines()[0])
    ap.add_argument("--codex-home", default="", help="default: the Codex core's home")
    ap.add_argument("--codex", default="codex", help="the codex binary")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-app-server", action="store_true", help="read only the auth file's claims")
    a = ap.parse_args(argv)
    where = Path(os.path.expanduser(a.codex_home)) if a.codex_home else home()
    acct = read(where, a.codex, app_server=not a.no_app_server)
    if a.json:
        print(json.dumps({"codex_home": str(where), **{k: (acct or {}).get(k) for k in FIELDS}}, sort_keys=True))
    else:
        print(words(acct, where))
    return 0 if acct and acct.get("kind") else 1


if __name__ == "__main__":
    sys.exit(main())
