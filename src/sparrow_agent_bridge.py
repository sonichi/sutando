#!/usr/bin/env python3
"""One agent's gateway bridge under sparrowd, from the declaration its claude-local or codex-local install wrote.

A declaration is data the agent's own session could write, so it chooses nothing: everything the
bridge runs with is derived from the declaration's path segment (`<agents>/<segment>.bridge/`) by the
adapter's own functions, and the file is only checked to agree with it. The worker name, the vault
key, the token variable and the exact env (three dirs plus `AGENT_MXID`, whose id must map to the same
segment) are each compared; any difference skips the declaration with one reason line. A seat's declaration
(one `connect` wired) names no vault key: its env is the adapter's launch_env over the seat .env beside the
root, whose token file must be the seat's own, and its bridge runs with exactly that, never a vault token.

`declarations(workspace, vault_keys)` lists the valid ones whose vault key exists. What a declaration's
own bytes can be (not UTF-8, not JSON, too deep, too large, not a regular file) skips that one entry with a
reason; so does a declaration file that exists but cannot be read, whose worker the supervisor keeps as it
is (`held`). An agents directory that cannot be listed or a home that cannot be entered is `ScanUnavailable`
(nothing is known), never "nothing declared": a supervisor keeps what runs until the scan can be made
again. Each declaration carries the digest of its bytes, so an edited one is a changed worker. Run as a script with
a declaration path, it re-checks it, builds the bridge's environment (sparrowd's own, minus the host's
tokens, identity and owner-DM variables, plus the derived env, the host config the owner lists for a
bridge, resolved here where the host's files may be read, and the vault token, never on disk, marked as
handed in so the bridge opens no credential file), injects the agent's dirs and the workspace's task
stamper, and runs the package bridge in this process.
"""
from __future__ import annotations

import hashlib
import json
import os
import runpy
import stat
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
_REPO = _SRC.parent
for _p in (str(_SRC), str(_REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ag2_sparrow import agent_identity, gateway_credential_sources as _sources  # noqa: E402
from ag2_sparrow import connect_bridge as adapter  # noqa: E402
from ag2_sparrow.adapters.claude_code.shape import LOCAL as CLAUDE_LOCAL  # noqa: E402
from ag2_sparrow.bounded_file_read import read_regular  # noqa: E402
from ag2_sparrow.adapters.codex.connect_cli import COMMAND as CODEX_LOCAL_COMMAND  # noqa: E402

WRITTEN_BY = f"{CLAUDE_LOCAL.command} install"
WRITERS = (WRITTEN_BY, f"{CODEX_LOCAL_COMMAND} install")
BRIDGE_MODULE = "ag2_sparrow.remote_gateway_bridge"
SUFFIX = ".bridge"
MAX_DECLARATION_BYTES = _sources.MAX_DECLARATION_BYTES
# Never inherited from sparrowd: the host's token files, its agent identity and its owner DM (and its
# gateway tokens and endpoints, under every name _sources.bridge_strip_keys lists).
HOST_ONLY = ("AG2_DEVICE_ENV", "REMOTE_TASK_TOKEN_FILE", "AGENT_MXID", "AGENT_ID", "AG2SPACE_USER_ID",
             "GATEWAY_INSTANCE", "REMOTE_PROACTIVE_ROOM", "AGENT_CONNECT_TASK_DIR", "AGENT_CONNECT_RESULT_DIR",
             "AGENT_CONNECT_STATE_DIR")


class ScanUnavailable(Exception):
    """The declarations (or what they need) could not be read: not an empty set, an unknown one."""


class DeclarationUnreadable(ScanUnavailable):
    """One declaration file exists but cannot be read: unknown for that entry only."""


def _stat(path: Path):
    """The stat, None when absent; any other failure to look is ScanUnavailable."""
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ScanUnavailable(f"{path}: unreadable ({type(e).__name__})") from e


def _resolved(env: dict) -> dict:
    return {k: str(Path(v).resolve()) if k.startswith("AGENT_CONNECT_") else v for k, v in env.items()}


def seat_env(root: str, mxid: str) -> "tuple[dict, str]":
    """(the seat bridge's env, '') or ({}, why): the adapter's launch_env over the seat beside `root`, whose
    token file must be the seat's own."""
    seat = adapter.seat_file(root)
    env, problem = adapter.launch_env({"ROOT": root, "AGENT_ID": mxid, "TOKEN_ENV_FILE": seat,
                                       "BRIDGE_STATE_DIR": str(adapter.bridge_home(root) / "state")})
    own = os.path.join(os.path.dirname(seat), _sources.SEAT_TOKEN_NAME)
    if not problem and not _sources.is_seat(env):
        problem = f"{seat} is not a seat .env"
    if not problem and _sources.named_token_file(env) != own:
        problem = f"the seat {seat} names a token file other than its own, {own}"
    return ({}, problem) if problem else (env, "")


def load(path: Path, agents: Path) -> "tuple[dict | None, str]":
    """(the derived declaration, '') or (None, why it is not run). The returned name, vault key and env
    come from the path's segment, never from the file."""
    path = Path(path)
    home = path.parent
    if path.name != adapter.DECLARATION or not home.name.endswith(SUFFIX) or \
            home.resolve().parent != Path(agents).resolve():
        return None, f"{path}: not a declaration directly under {agents}"
    segment = home.name[:-len(SUFFIX)]
    # What the file's own bytes can do is a skip with a reason; only a failure to look is unavailable.
    st = _stat(path)
    if st is None:
        return None, f"{path}: absent"
    if not stat.S_ISREG(st.st_mode):
        return None, f"{path}: not a regular file"
    if st.st_size > MAX_DECLARATION_BYTES:
        return None, f"{path}: {st.st_size} bytes is not a declaration (cap {MAX_DECLARATION_BYTES})"
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise DeclarationUnreadable(f"{path}: unreadable ({type(e).__name__})") from e
    try:
        decl = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError is a ValueError
        return None, f"{path}: malformed ({type(e).__name__})"
    if not isinstance(decl, dict) or decl.get("written_by") not in WRITERS:
        return None, f"{path}: not written by {' or '.join(WRITERS)}"
    env = decl.get("env")
    mxid = env.get("AGENT_MXID") if isinstance(env, dict) else None
    try:
        # The bridge reports the id verbatim, so it must be exact: no surrounding whitespace to strip.
        own = isinstance(mxid, str) and mxid == mxid.strip() and agent_identity.root_segment(mxid) == segment
    except ValueError:
        own = False
    if not own:
        return None, f"{path}: AGENT_MXID {mxid!r} is not the agent the segment {segment} names"
    root = str(Path(agents).resolve() / segment)
    seat = _sources.is_seat(env)
    if not seat and decl["written_by"] != WRITTEN_BY:
        return None, f"{path}: {decl['written_by']} declares only a seat's bridge"
    want_env, problem = seat_env(root, mxid) if seat else (adapter.bridge_env(root, mxid), "")
    if problem:
        return None, f"{path}: {problem}"
    want = {"name": adapter.worker_name(segment), "token_vault_key": "" if seat else adapter.vault_key(segment),
            **_sources.bridge_declaration_fields(), "module": BRIDGE_MODULE, "env": want_env}
    for key in [k for k in want if k != "env"]:
        if decl.get(key) != want[key]:
            return None, f"{path}: {key} {decl.get(key)!r} is not the derived {want[key]!r}"
    if sorted(env) != sorted(want["env"]):
        extra = sorted(set(env) ^ set(want["env"]))
        return None, f"{path}: env keys differ from the bridge's own ({', '.join(map(str, extra))})"
    try:
        same = _resolved(env) == _resolved(want["env"])
    except (TypeError, OSError, ValueError):
        same = False
    if not same:
        return None, f"{path}: env is not the canonical one for {segment}"
    return dict(want, url_vault_key=adapter.url_vault_key(segment), digest=hashlib.sha256(raw).hexdigest()), ""


def bridge_environ(decl: dict, token: str, base=None, url: str = "") -> dict:
    """What the bridge runs under: sparrowd's environment minus the host-only variables, every name the
    owner lists for a host's gateway tokens and endpoints and every key it carries, plus the derived env, an empty owner
    DM, the owner's carried config (resolved here, from the host's files if need be) and the agent's token
    (with `url`, the agent's own gateway from endpoint(), when the token embeds none). A seat's is the
    derived env alone: its credential is its own token file, so no token, URL or carried config is added."""
    # The carried keys come only from the owner's result below, never from the generic base.
    strip = _sources.bridge_strip_keys() + _sources.BRIDGE_CARRIED_CONFIG
    host = os.environ if base is None else base
    env = {k: v for k, v in host.items() if k not in HOST_ONLY and k not in strip}
    env.update(decl["env"], REMOTE_PROACTIVE_ROOM="")
    if is_seat(decl):
        return env
    token_env = _sources.bridge_token_environ(token, url)
    env.update(_sources.bridge_carried_config(host, read_regular))
    env.update(token_env)
    return env


def is_seat(decl: dict) -> bool:
    """Whether a loaded declaration is a seat's: its credential in its own token file, no vault key."""
    return _sources.is_seat(decl.get("env") or {})


def declarations(workspace: Path, vault_keys, held=None) -> "tuple[list, list]":
    """([(path, declaration)], [reason]) for the agent bridges under `workspace`. An absent agents
    directory declares nothing; one that cannot be listed, or a home that cannot be entered, raises
    ScanUnavailable. A declaration file that cannot be read is a skip whose worker name is appended to
    `held`: its state is unknown, so the caller keeps whatever runs under that name."""
    agents, found, skipped = Path(workspace) / "agents", [], []
    names = set(vault_keys)
    try:
        homes = sorted(p for p in agents.iterdir() if p.name.endswith(SUFFIX))
    except FileNotFoundError:
        return [], []
    except OSError as e:
        raise ScanUnavailable(f"{agents}: unreadable ({type(e).__name__})") from e
    for home in homes:
        path = home / adapter.DECLARATION
        if _stat(path) is None:
            continue
        try:
            decl, why = load(path, agents)
        except DeclarationUnreadable as e:
            decl, why = None, str(e)
            if held is not None:
                held.append(adapter.worker_name(home.name[:-len(SUFFIX)]))
        if decl is not None and not is_seat(decl) and decl["token_vault_key"] not in names:
            decl, why = None, f"{path}: the vault has no {decl['token_vault_key']}; vault set it to the agent's relay token"
        if decl is None:
            skipped.append(why)
        else:
            found.append((path, decl))
    return found, skipped


def endpoint(decl: dict, token: str, get_key) -> "tuple[str, str]":
    """(the agent's gateway URL for a bare-secret token, '') or ('', why the bridge cannot start). A
    combined token carries its own URL, checked here: ('', ''). Any other token, a URL-shaped opaque bearer
    included, takes its URL from the owner-written vault key only, never the host's environment or the
    declaration, which the agent's own session could write."""
    key, token_key = decl["url_vault_key"], decl["token_vault_key"]
    if _sources.token_secret_empty(token):
        return "", (f"the token under {token_key} carries no secret: store the agent's relay token there "
                    f"(https://<gateway>|<secret>, or a bare secret with {key})")
    if _sources.token_embeds_url(token):
        problem = _sources.gateway_url_problem(_sources.token_gateway(token))
        return ("", f"the gateway inside the token under {token_key} {problem}") if problem else ("", "")
    try:
        url = (get_key(key) or "").strip()
    except KeyError:
        url = ""
    if not url and _sources.token_url_shaped(token):
        return "", (f"the token under {token_key} has no gateway: it starts with a URL scheme but has no "
                    f"url|secret separator. If it is a combined token that lost its '|', store it again as "
                    f"https://<gateway>|<secret> under {token_key}; if it is an opaque bearer, run "
                    f"`vault set {key} <the agent's gateway URL>`")
    if not url:
        return "", (f"the token under {token_key} is a bare secret and the vault has no {key}: run "
                    f"`vault set {key} <the agent's gateway URL>`, or store the combined "
                    f"https://<gateway>|<secret> token under {token_key}")
    problem = _sources.gateway_url_problem(url)
    return ("", f"{key} {problem}") if problem else (url, "")


def preflight(workspace, get_key) -> int:
    """One line per declared agent bridge: whether it can start (its token in the vault, and its gateway in
    the token or under its URL key). 1 when any cannot. No value is printed."""
    agents, bad = Path(workspace) / "agents", []
    try:
        homes = sorted(p for p in agents.iterdir() if p.name.endswith(SUFFIX))
    except (FileNotFoundError, NotADirectoryError):
        homes = []
    except OSError as e:
        print(f"WILL NOT START: {agents}: unreadable ({type(e).__name__})")
        return 1
    for home in homes:
        path = home / adapter.DECLARATION
        try:
            if _stat(path) is None:
                continue
            decl, why = load(path, agents)
        except ScanUnavailable as e:
            decl, why = None, str(e)
        if decl is not None and is_seat(decl):
            print(f"ok: {decl['name']} (gateway from its seat's token file)")
            continue
        if decl is not None:
            try:
                token = get_key(decl["token_vault_key"])
            except KeyError:
                token, why = None, f"the vault has no {decl['token_vault_key']}"
            if token is not None:
                url, why = endpoint(decl, token, get_key)
                if not why:
                    where = "its token" if not url else decl["url_vault_key"]
                    print(f"ok: {decl['name']} (gateway from {where})")
                    continue
            why = f"{decl['name']}: {why}"
        bad.append(why)
        print(f"WILL NOT START: {why}")
    return 1 if bad else 0


def main(argv=None, workspace=None, get_key=None, run=runpy.run_module) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: sparrow_agent_bridge.py <workspace>/agents/<agent>.bridge/worker.json | --preflight",
              file=sys.stderr)
        return 2
    if workspace is None:
        from workspace_default import resolve_workspace
        workspace = resolve_workspace()
    if get_key is None and args[0] == "--preflight":
        from vault_intercept import get_vault_key as get_key
    if args[0] == "--preflight":
        return preflight(workspace, get_key)
    try:
        decl, why = load(Path(args[0]), Path(workspace) / "agents")
    except ScanUnavailable as e:
        decl, why = None, str(e)
    if decl is None:
        print(f"sparrow-agent-bridge: not starting: {why}", file=sys.stderr)
        return 2
    token, url = "", ""
    if not is_seat(decl):
        if get_key is None:
            from vault_intercept import get_vault_key as get_key
        try:
            token = get_key(decl["token_vault_key"])
        except KeyError:
            print(f"sparrow-agent-bridge: not starting: the vault has no {decl['token_vault_key']}", file=sys.stderr)
            return 1
        url, problem = endpoint(decl, token, get_key)
        if problem:
            print(f"sparrow-agent-bridge: not starting: {problem}", file=sys.stderr)
            return 1
    environ = bridge_environ(decl, token, url=url)
    os.environ.clear()
    os.environ.update(environ)
    from ag2_sparrow._dirs import set_dirs
    from ag2_sparrow.local_task_protocol import set_task_stamper
    from task_envelope import stamp_text
    set_dirs(task_dir=decl["env"]["AGENT_CONNECT_TASK_DIR"], result_dir=decl["env"]["AGENT_CONNECT_RESULT_DIR"],
             state_dir=decl["env"]["AGENT_CONNECT_STATE_DIR"])
    set_task_stamper(stamp_text)
    run(BRIDGE_MODULE, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
