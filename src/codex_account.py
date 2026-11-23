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
pass a worker's own home with `--codex-home` when it has one. `verified` is true only
when Codex names an account; otherwise the file is read (`verified` false), and when neither names
one `verified` is null: no sign-in was found, which is not proof of a signed-out home. `codex` is found on the launcher's PATH.
With `--task-file` nothing is looked up unless the task is attested as the owner's, from the owner's
checked DM (exit 3). Exit 0 an account was read, 1 none was found.
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
import tempfile
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
    """None only when the task's envelope verifies and its attested headers (`task_envelope`) say the
    owner sent it from the owner's checked DM; else why not.

    The same tests the owner's card taps pass (`agent_card.owner_may_act`, DM only): a missing or other
    tier, a collaborator, a sender other than the connection's owner, or a room that is not that
    connection's checked owner DM is refused, and so is anything that cannot be read."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import agent_card
        import connections
        from task_envelope import attested_task_headers, verify_text
        text = Path(task_file).read_text(encoding="utf-8")
        if workspace is None:
            from workspace_default import resolve_workspace
            workspace = resolve_workspace(migrate=False)
        if verify_text(text, Path(workspace)).get("verdict") != "verified":
            return "the task's envelope is not verified; the account is the owner's data"
        headers = dict(attested_task_headers(text, Path(workspace)).headers)
        rows = connections.read_rows(Path(workspace))
    except Exception as exc:  # noqa: BLE001 - anything unreadable refuses
        return f"the task's origin cannot be checked ({type(exc).__name__})"
    room = str(headers.get("channel_id") or "").strip()
    for row in rows or []:
        owner = str(row.get("owner_mxid") or "").strip()
        try:
            dm = connections.owner_dm(row, Path(workspace))
        except Exception:  # noqa: BLE001 - a row without a readable DM attests nothing
            continue
        if owner and dm and room == dm:
            why = agent_card.owner_may_act(headers, owner, dm_only=True)
            return f"{why}; the account is the owner's data" if why else None
    return "not asked in the owner's checked DM; the account is the owner's data"


def _answer(account: dict, source: str) -> dict:
    """Only the fields that may be shown, from an `account/read` account or decoded claims."""
    kind = str(account.get("type") or "")
    out = {"email": account.get("email") or None, "plan": account.get("planType") or None,
           "workspace": account.get("workspaceName") or None, "kind": kind or None, "source": source}
    return {k: (str(v) if v is not None else None) for k, v in out.items()}


def launch_path(repo: Optional[Path] = None) -> str:
    """PATH as the Codex core launcher builds it (src/agent/launch-path.sh, the one owner): the bundled
    node, the Codex install dir, the desktop's cli/codex/bin. The caller's PATH when that cannot run."""
    repo = Path(repo or Path(__file__).resolve().parents[1])  # lint-workspace-resolution: allow-repo-root
    try:
        r = subprocess.run(["/bin/bash", str(repo / "src" / "agent" / "launch-path.sh"), "--codex-path", str(repo)],
                           capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        r = None
    path = r.stdout.strip() if r is not None and r.returncode == 0 else ""
    return path or os.environ.get("PATH", "")


# The files an `account/read` needs; the lookup reads copies of these and nothing else of the home.
READ_FILES = ("auth.json", "config.toml")


def _copy_into(scratch: str, codex_home: Path) -> None:
    for name in READ_FILES:
        src = Path(codex_home) / name
        if src.is_file():
            fd = os.open(str(Path(scratch) / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(src.read_bytes())


def account_of(envelope) -> Optional[dict]:
    """The account a response envelope names, or None when it names none for certain.

    Only a JSON-RPC response with no `error` and a `result` object whose `account` is an object with
    a non-empty string `type` names one. Anything else, `account: null` included, is no answer: the
    lookup runs on a copy of the home, which cannot reach a sign-in Codex keeps under the original
    home's keyring entry, so a null there does not prove the home is signed out."""
    if not isinstance(envelope, dict) or "error" in envelope or not isinstance(envelope.get("result"), dict):
        return None
    account = envelope["result"].get("account")
    if isinstance(account, dict) and isinstance(account.get("type"), str) and account["type"]:
        return account
    return None


def from_app_server(codex_home: Path, codex: str = "codex", timeout: float = 20.0) -> Optional[dict]:
    """The account by Codex's own `account/read`, None when Codex names none (signed out, or a sign-in a
    copy of the home cannot reach) or cannot answer. It runs on a private copy of READ_FILES that lives,
    preparation included, only inside this call, so nothing is written into the running core's home."""
    from codex_rpc import Rpc
    path = launch_path()
    if os.sep not in codex:
        codex = shutil.which(codex, path=path) or codex
    scratch = None
    rpc = None
    try:
        scratch = tempfile.mkdtemp(prefix="codex-account-")
        _copy_into(scratch, Path(codex_home))
        rpc = Rpc([codex, "app-server", "--listen", "stdio://"], env=dict(os.environ, CODEX_HOME=scratch, PATH=path),
                  cwd=scratch, stderr=subprocess.DEVNULL)
        init = rpc.call("initialize", {"clientInfo": CLIENT, "capabilities": {}}, timeout=timeout)
        if "error" in init:
            return None
        rpc.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        account = account_of(rpc.call("account/read", {"refreshToken": False}, timeout=timeout))
    except (OSError, RuntimeError, ValueError):
        return None
    finally:
        if rpc is not None:
            try:
                rpc.p.kill()
                rpc.close()
            except (OSError, ValueError, subprocess.SubprocessError):  # pragma: no cover - a child that will not die
                pass
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    if account is None:
        return None
    out = _answer(account, "app-server")
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
        return f"I found no sign-in for Codex under {codex_home}."
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
        where = Path(os.path.abspath(os.path.expanduser(a.codex_home))) if a.codex_home else home()
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
