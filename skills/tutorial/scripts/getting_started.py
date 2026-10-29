#!/usr/bin/env python3
"""The owner's "Getting started" page in AG2 Space: render it, and create or refresh it in one room.

  getting_started.py render --name NAME|auto [--core claude|codex|unknown|auto]   -> the page's HTML
  getting_started.py plan --room ROOM                   -> {"action": "create"|"update", "kind": ...}
  getting_started.py publish [--room ROOM] [--name NAME|auto] [--owner MXID] [--core ...] [--summon]
      [--room-commons PATH]
      -> {"ok": true, "action": "created"|"updated"|"unchanged", "kind": "html-<id>", "summoned": bool,
          "name": the name used}

The page (tour_page.py) is a short interactive AG2 Space tour: eight sections, plus a Claude Code
history import section when the core is Claude Code (`--core auto` asks the engine's config
resolver; unknown omits it). `--name auto` uses this agent's Matrix profile displayname, else a
name made from its mxid's localpart, else "Sutando"; it never fails.

room_commons.py is found beside this skill, else in the installed skills directory. Its
credentials are the connection serving the room's server on an install with several
(src/channel_credentials.py room_env), else the environment's, else the AG2 Space channel env
file's; nothing here prints them.

One page per room, created once. <workspace>/state/tutorial/getting-started.json records each
room's entry under a lock: `creating` with an operation_id before the page is made through
`room.commons.pages.create` (a retry with the same id returns the same page and writes its body at
most once), `pending` with its kind until every piece is written, then `complete`. A rerun resumes
whatever it finds. With no entry, a listed "Getting started" page is reused only when its text
carries this agent's ownership marker; more than one such page, or no agent id to check with,
stops the run. The page holds the agent's name and fixed text only; nothing read from the
owner's accounts goes on it.

Inputs the setup app saved: <workspace>/state/onboarding/tutorial-inputs.json (`room`, `owner`,
`agent_mxid`, `roles`) fills what the command line leaves out, so `publish` alone works in the owner's
DM. `owner` and `roles` apply only in that file's own room; a missing, unreadable or malformed file,
or a field of the wrong shape, is ignored. `--name` defaults to auto.

Exit codes: 0 done, 2 bad arguments, 3 the room-commons skill is not installed, 4 a room-commons
call failed or ownership was unclear (the marker keeps what is known, so the next run resumes).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tour_page  # noqa: E402,F401
from tour_page import (  # noqa: E402,F401  (the page lives in tour_page; these names are this CLI's API)
    AUDIENCES, CONNECT_PROMPT, PROMPT_SLOTS, clean_prompts, owner_marker, CORES, IMPORT_PROMPT, IMPORT_SECTION, NAME_MAX, PAGE_TITLE, SECTIONS, clean_name,
    render_page, sections_for,
)

SUMMON_CONTEXT = "A short tour of AG2 Space, with one thing to try in each part."
PROFILE_TIMEOUT = 5
CREATE_ACTION = "room.commons.pages.create"
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|KEY", re.I)
SECRET_SHAPE = re.compile(r"(\bBearer\s+)\S+|\bhttps?://\S+\|\S+", re.I)
# Each piece is one argv string: under Linux's 131,072-byte per-argument cap and Room Commons'
# 196,608-byte write.
CHUNK_BYTES = 120_000
USER_AGENT = "sutando-tutorial/1"
FALLBACK_NAME = "Sutando"
MXID = re.compile(r"^@([^:\s]+):([A-Za-z0-9.\-]+(?::\d+)?)$")
ROOM_ID = re.compile(r"^![A-Za-z0-9._=/+\-]+:[A-Za-z0-9.\-]+(?::\d+)?$")
ID_MAX = 255
ROLES_MAX = 500


class PublishError(RuntimeError):
    pass


def find_room_commons(environ: Optional[Mapping[str, str]] = None) -> Path:
    """room_commons.py beside this skill (the engine checkout it runs from), else the one in the
    installed skills directory; the first candidate when neither exists."""
    from util_paths import claude_home_path
    candidates = [REPO / "skills" / "room-commons" / "scripts" / "room_commons.py"]
    with contextlib.suppress(Exception):
        candidates.append(claude_home_path("skills", "room-commons") / "scripts" / "room_commons.py")
    return next((c for c in candidates if c.is_file()), candidates[0])


def capability_vars(script: Path) -> tuple:
    """(URL_VARS, TOKEN_VARS) of room_commons.py; ((), ()) when it cannot be loaded, which leaves
    the credentials to it alone."""
    try:
        sys.path.insert(0, str(script.parent))
        spec = importlib.util.spec_from_file_location("_tutorial_room_commons", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001
        return (), ()
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(script.parent))
    return tuple(getattr(mod, "URL_VARS", ())), tuple(getattr(mod, "TOKEN_VARS", ()))


def capability_names(script: Path) -> tuple:
    """The credential variables room_commons.py reads (its URL_VARS and TOKEN_VARS)."""
    return tuple(v for names in capability_vars(script) for v in names)


def connect_actions(script: Path, env: Optional[Dict[str, str]] = None):
    """room-commons' own Actions client (hosted MCP), opened with the credentials `env` names;
    the process environment is restored once the client holds its token."""
    import argparse as _argparse
    sys.path.insert(0, str(script.parent))
    saved = dict(os.environ)
    try:
        os.environ.update({k: v for k, v in (env or {}).items() if k not in os.environ or v == ""})
        spec = importlib.util.spec_from_file_location("room_commons", script)
        rc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rc)
        return rc.mcp_door(_argparse.Namespace(token=None, workspace=None, url=None))()
    finally:
        os.environ.clear()
        os.environ.update(saved)
        with contextlib.suppress(ValueError):
            sys.path.remove(str(script.parent))


def commons_env(script: Path, environ: Optional[Mapping[str, str]] = None,
                room: Optional[str] = None) -> Dict[str, str]:
    """The environment for room_commons.py acting in `room`: with several connections, the
    credential of the one serving its server; else this one, plus its credentials from the
    AG2 Space channel env file when the environment names none of them."""
    from channel_credentials import channel_credentials, room_env
    env = dict(os.environ if environ is None else environ)
    url_vars, token_vars = capability_vars(script)
    routed = room_env(url_vars, token_vars, room, environ=env)
    env.update(routed if routed is not None else channel_credentials((*url_vars, *token_vars), env))
    return env


def localpart_name(mxid: str) -> str:
    """A readable name from an mxid's localpart: `@chi-air-blue.agent:x` -> `Chi Air Blue`."""
    m = MXID.match(mxid or "")
    local = re.sub(r"\.agent$", "", m.group(1)) if m else ""
    words = [w for w in re.split(r"[-._=/]+", local) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)[:NAME_MAX]


def _https_get(url: str) -> bytes:
    # The homeserver's edge refuses urllib's default User-Agent with a 403.
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=PROFILE_TIMEOUT) as resp:
        return resp.read(65536)


def profile_name(mxid: str, fetch: Optional[Callable[[str], bytes]] = None) -> str:
    """The mxid's Matrix profile displayname from its homeserver; '' on any failure."""
    m = MXID.match(mxid or "")
    if not m:
        return ""
    fetch = fetch or _https_get
    server = m.group(2)
    base = f"https://{server}"
    with contextlib.suppress(Exception):
        known = json.loads(fetch(f"https://{server}/.well-known/matrix/client"))
        url = str(known["m.homeserver"]["base_url"])
        if url.startswith("https://"):
            base = url
    try:
        got = json.loads(fetch(f"{base.rstrip('/')}/_matrix/client/v3/profile/"
                               f"{urllib.parse.quote(mxid, safe='')}/displayname"))
    except Exception:  # noqa: BLE001
        return ""
    name = got.get("displayname") if isinstance(got, dict) else None
    return " ".join(name.split())[:NAME_MAX] if isinstance(name, str) else ""


def agent_mxid(environ: Optional[Mapping[str, str]] = None) -> str:
    """This agent's mxid: $AGENT_MXID, else the AG2 Space channel env file's."""
    from channel_credentials import channel_value
    return channel_value("AGENT_MXID", environ)


def resolve_name(name: str, mxid: Optional[str] = None,
                 fetch: Optional[Callable[[str], bytes]] = None) -> str:
    """`name` itself, or for `auto`: the profile displayname, else the localpart, else Sutando."""
    if name != "auto":
        return name
    mxid = mxid if mxid is not None else agent_mxid()
    return profile_name(mxid, fetch) or localpart_name(mxid) or FALLBACK_NAME


def chunks(text: str, limit: int = 0) -> List[str]:
    """`text` cut into pieces of at most `limit` UTF-8 bytes, never inside a character."""
    limit = limit or CHUNK_BYTES
    out, part, size = [], [], 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        if size + n > limit and part:
            out.append("".join(part))
            part, size = [], 0
        part.append(ch)
        size += n
    if part:
        out.append("".join(part))
    return out


def redact(text: str, env: Optional[Mapping[str, str]]) -> str:
    """`text` with every secret it may carry replaced: the value of each credential-like variable
    in `env` (and each half of a `url|secret` token), and anything shaped like a bearer token or a
    `url|secret` pair, so an error message never carries one."""
    secrets = set()
    for key, value in (env or {}).items():
        if SECRET_NAME.search(key) and value:
            secrets.update(v for v in (value, *value.split("|")) if len(v) >= 8)
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return SECRET_SHAPE.sub(lambda m: (m.group(1) or "") + "[redacted]", text)


def load_prompts(path: Optional[Path]) -> Dict[str, str]:
    """The usable prompts in a --prompts JSON file; {} when there is none or it cannot be read, so
    a bad file never fails the run (every slot keeps its default)."""
    if path is None:
        return {}
    try:
        return clean_prompts(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def inputs_path(workspace: Path) -> Path:
    return workspace / "state" / "onboarding" / "tutorial-inputs.json"


def load_inputs(path: Path) -> Dict[str, str]:
    """The well-formed fields of the setup app's tutorial inputs: `room`, `owner`, `agent_mxid`,
    `roles`. {} when the file is missing or unreadable; a field of the wrong shape is left out."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    for key, shape in (("room", ROOM_ID), ("owner", MXID), ("agent_mxid", MXID)):
        value = data.get(key)
        if isinstance(value, str) and len(value) <= ID_MAX and shape.fullmatch(value):
            out[key] = value
    roles = data.get("roles")
    if isinstance(roles, list) and all(isinstance(r, str) for r in roles):
        roles = ", ".join(r.strip() for r in roles if r.strip())
    if isinstance(roles, str) and roles.strip() and len(roles) <= ROLES_MAX and "\n" not in roles:
        out["roles"] = roles.strip()
    return out


def with_inputs(args: argparse.Namespace, inputs: Mapping[str, str]) -> argparse.Namespace:
    """`args` with what the command line left out taken from `inputs`. The owner and roles belong
    to the inputs' own room, so they apply only when publishing there."""
    room = args.room or inputs.get("room")
    same_room = bool(room) and room == inputs.get("room")
    return argparse.Namespace(**{
        **vars(args), "room": room,
        "owner": args.owner or (inputs.get("owner") if same_room else None),
        "roles": args.roles or (inputs.get("roles", "") if same_room else ""),
        "agent_mxid": args.agent_mxid or inputs.get("agent_mxid"),
    })


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def marker_path(workspace: Path) -> Path:
    return workspace / "state" / "tutorial" / "getting-started.json"


def load_marker(path: Path) -> Dict[str, dict]:
    """{room: {"kind", "hash", "updated_at"}}; an unreadable marker reads as no pages."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rooms = data.get("rooms") if isinstance(data, dict) else None
    if not isinstance(rooms, dict):
        return {}
    return {r: v for r, v in rooms.items() if isinstance(v, dict) and (
        isinstance(v.get("kind"), str) and v["kind"].startswith("html-")
        or v.get("state") == "creating" and isinstance(v.get("operation_id"), str))}


def save_marker(path: Path, rooms: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".getting-started.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "rooms": rooms}, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, str(path))


def candidates(pages: List[dict]) -> List[dict]:
    """Listed pages that could be an unrecorded tutorial page: titled "Getting started", not a
    published copy."""
    return [p for p in pages if p.get("title") == PAGE_TITLE and not p.get("copy_of")
            and str(p.get("kind", "")).startswith("html-")]


def owned(page: dict, text: str, agent: Optional[str]) -> Optional[bool]:
    """Whether a candidate is this agent's tutorial page: its text carries this agent's marker (the
    create writes it with the first body, so an empty page is never one). None: no agent id."""
    if not agent:
        return None
    by = page.get("by") or ""
    if by and by != agent:
        return False
    return owner_marker(agent) in text


def plan(rooms: Dict[str, dict], room: str, pages: Optional[List[dict]] = None) -> dict:
    """Rewrite the recorded page while it is listed (`pending` when its first write never
    finished); with no usable entry, reconcile against unrecorded candidates, else create."""
    entry = rooms.get(room)
    if entry and entry.get("state") == "creating":
        return {"action": "resume", "kind": None, "pending": True, "operation_id": entry["operation_id"]}
    if entry and (pages is None or entry["kind"] in [p.get("kind") for p in pages]):
        return {"action": "update", "kind": entry["kind"], "pending": entry.get("state") == "pending"}
    if candidates(pages or []):
        return {"action": "reconcile", "kind": None, "pending": True}
    return {"action": "create", "kind": None, "pending": False}


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / ".getting-started.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


class Commons:
    """The room-commons CLI, one call per subprocess."""

    def __init__(self, script: Path, room: str, env: Optional[Dict[str, str]] = None, actions=None):
        self.script, self.room, self.env = script, room, env
        self._actions, self._door = actions, None

    def create_page(self, title: str, body: str, operation_id: str) -> str:
        """`room.commons.pages.create` with a first body. The same operation_id again returns the
        same page and writes its body at most once, so an unclear outcome is retried, never redone."""
        try:
            if self._door is None:
                self._door = (self._actions or (lambda: connect_actions(self.script, self.env)))()
            got = self._door.execute(self.room, CREATE_ACTION, {"title": title, "kind": "html", "body": body},
                                     operation_id=operation_id)
        except Exception as exc:  # noqa: BLE001 - any failure leaves the create unresolved, not redone
            raise PublishError(f"page create {operation_id} did not complete: "
                               f"{redact(str(exc), self.env)}") from None
        kind = got.get("kind") if isinstance(got, dict) else None
        if not isinstance(kind, str) or not kind.startswith("html-"):
            raise PublishError(f"page create {operation_id} returned no html page kind: "
                               f"{redact(repr(kind), self.env)}")
        return kind

    def run(self, *args: str, kind: Optional[str] = None, as_json: bool = False) -> str:
        argv = [sys.executable, str(self.script)]
        if as_json:
            argv.append("--json")
        if kind:
            argv += ["--kind", kind]
        argv += list(args)
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=self.env)
        except subprocess.TimeoutExpired:
            raise PublishError(f"room-commons {args[0]} timed out") from None
        if done.returncode != 0:
            said = redact((done.stderr or done.stdout).strip(), self.env)
            raise PublishError(f"room-commons {args[0]} failed ({done.returncode}): {said[:400]}")
        return done.stdout

    def reconcile(self, pages: List[dict], agent: Optional[str]) -> Optional[str]:
        """The one unrecorded page that is this agent's tutorial page, else None; refuses to guess
        when ownership is unclear or more than one page qualifies."""
        mine, unclear = [], []
        for p in candidates(pages):
            verdict = owned(p, self.read(p["kind"]), agent)
            (mine if verdict else unclear if verdict is None else []).append(p["kind"])
        if unclear or len(mine) > 1:
            raise PublishError(
                f"cannot tell which \"{PAGE_TITLE}\" page is this agent's ({len(mine)} carry its "
                f"marker: {mine}; ownership unknown for {unclear})"
                + ("; pass --agent-mxid" if not agent else "; remove the extra page(s) or record one"))
        return mine[0] if mine else None

    def listed_pages(self) -> List[dict]:
        pages = json.loads(self.run("pages", self.room, as_json=True))
        return [p for p in pages if isinstance(p, dict) and p.get("id")]

    def read(self, kind: str) -> str:
        return json.loads(self.run("read", self.room, kind=kind, as_json=True)).get("text") or ""

    def append(self, kind: str, text: str) -> None:
        for part in chunks(text):
            self.run("append", self.room, part, kind=kind)

    def rewrite(self, kind: str, old: str, new: str) -> None:
        """Replace the page's text: one edit when both texts fit one argument, else clear the page
        piece by piece and append the new text in pieces."""
        if not old:
            self.append(kind, new)
        elif len(old.encode("utf-8")) <= CHUNK_BYTES and len(new.encode("utf-8")) <= CHUNK_BYTES:
            self.run("replace", self.room, old, new, kind=kind)
        else:
            for part in chunks(old):
                self.run("replace", self.room, part, "", kind=kind)
            self.append(kind, new)


def _record(marker: Path, rooms: Dict[str, dict], room: str, entry: dict) -> None:
    """Write this room's entry. A marker that cannot be written fails the run; the next run resumes
    from the last entry written, or finds the page again by its ownership marker."""
    rooms[room] = {**entry, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        save_marker(marker, rooms)
    except OSError as exc:
        raise PublishError(f"could not record {entry.get('kind') or entry['state']} in {marker}: {exc}") from None


def _fill(commons: Commons, kind: str, page: str) -> None:
    """Bring a page the create just wrote (or wrote earlier) to `page`: append the rest after a
    first body that matches, else rewrite whatever is there."""
    current, first = commons.read(kind), chunks(page)[0]
    if current == first:
        commons.append(kind, page[len(first):])
    elif current != page:
        commons.rewrite(kind, current, page)


def publish(commons: Commons, marker: Path, name: str, owner: Optional[str] = None,
            summon: bool = False, core: str = "unknown", audience: str = "dm",
            agent: Optional[str] = None, prompts: object = None, roles: object = None) -> dict:
    page = render_page(name, core, audience, agent, prompts, roles)
    with _locked(marker):
        rooms = load_marker(marker)
        pages = commons.listed_pages()
        step = plan(rooms, commons.room, pages)
        if step["action"] == "reconcile":
            found = commons.reconcile(pages, agent)
            step = ({"action": "adopt", "kind": found, "pending": True} if found
                    else {"action": "create", "kind": None, "pending": False})
        elif step["action"] == "resume" and candidates(pages):
            # The create may have landed and the server forgotten its operation id: a page this
            # agent tagged is that page, so finish it rather than re-issue the create.
            found = commons.reconcile(pages, agent)
            if found:
                _record(marker, rooms, commons.room, {"kind": found, "state": "pending"})
                step = {"action": "update", "kind": found, "pending": True}
        kind = step["kind"]
        if step["action"] in ("create", "resume"):
            op = step.get("operation_id") or f"tutorial-{uuid.uuid4().hex}"
            _record(marker, rooms, commons.room, {"state": "creating", "operation_id": op})
            kind = commons.create_page(PAGE_TITLE, chunks(page)[0], op)
            _record(marker, rooms, commons.room, {"kind": kind, "state": "pending", "operation_id": op})
            try:
                _fill(commons, kind, page)
            except PublishError as exc:
                raise PublishError(f"{exc}; {kind} stays recorded as pending and the next run "
                                   "finishes it") from None
            action = "created"
        else:
            if step["action"] == "adopt":
                _record(marker, rooms, commons.room, {"kind": kind, "state": "pending"})
            current = commons.read(kind)
            if current == page:
                action = "unchanged"
            else:
                commons.rewrite(kind, current, page)
                action = "updated"
        _record(marker, rooms, commons.room, {"kind": kind, "state": "complete", "hash": content_hash(page)})
    result = {"ok": True, "action": action, "kind": kind, "summoned": False}
    if step["action"] == "adopt":
        result["adopted"] = True
    # Summoned once: when this page's creation finishes (here, or resumed from our own pending
    # entry). Adopting a page found by its ownership marker is not a creation.
    first = action == "created" or step["action"] == "update" and step["pending"]
    if owner and (summon or first):
        try:
            commons.run("summon", commons.room, owner, "--context", SUMMON_CONTEXT,
                        "--page-title", PAGE_TITLE, kind=kind)
            result["summoned"] = True
        except PublishError as exc:
            result["summon_error"] = str(exc)
    return result


def detect_core() -> str:
    """The core runtime the engine resolves (launcher stamp, else config); unknown on failure."""
    try:
        from sutando_config import resolve_core_runtime
        core = resolve_core_runtime()
    except Exception:  # noqa: BLE001
        return "unknown"
    return core if core in CORES else "unknown"


def _workspace() -> Path:
    from workspace_default import resolve_workspace
    return resolve_workspace()


def _personal_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompts", type=Path, help="JSON object of tailored prompts by slot ("
                        + ", ".join(PROMPT_SLOTS) + "); bad or missing entries keep the default")
    parser.add_argument("--roles", default="", help="the first-contact \"I picked: ...\" payload as given, "
                        "e.g. \"Founder, Product Manager.\" (labels, or slugs)")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="getting_started.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("render", help="print the page's HTML")
    r.add_argument("--name", required=True,
                   help="the agent's own name; auto: its Matrix profile name, else its mxid's localpart")
    r.add_argument("--agent-mxid", help="with --name auto: this agent's mxid (default $AGENT_MXID or the "
                                        "channel env file's)")
    r.add_argument("--audience", choices=AUDIENCES, default="dm",
                   help="dm: the owner's DM, which names the agent; room: any other room, which says "
                        "\"your agent\"")
    _personal_args(r)
    r.add_argument("--core", choices=CORES + ("auto",), default="unknown",
                   help="the Sutando core; claude adds the Claude Code import section")
    p = sub.add_parser("plan", help="what publish would do in this room, from the marker alone")
    p.add_argument("--room", required=True)
    u = sub.add_parser("publish", help="create the page in this room, or refresh the one already there")
    u.add_argument("--room", help="the owner's DM room id (default: the setup app's saved inputs)")
    u.add_argument("--name", default="auto",
                   help="the agent's own name; auto (default): its Matrix profile name, else its mxid's "
                        "localpart")
    u.add_argument("--agent-mxid", help="with --name auto: this agent's mxid")
    u.add_argument("--owner", help="the owner's mxid; summoned to the page when it is created")
    u.add_argument("--core", choices=CORES + ("auto",), default="auto",
                   help="the Sutando core (default auto: what the engine resolves); claude adds the "
                        "Claude Code import section, unknown leaves it out")
    u.add_argument("--audience", choices=AUDIENCES, default="dm",
                   help="dm (default): the owner's DM; room: any other room, where the page says \"your agent\"")
    _personal_args(u)
    u.add_argument("--summon", action="store_true", help="summon the owner on a refresh too")
    u.add_argument("--room-commons", type=Path, default=None,
                   help="path to room_commons.py (default: beside this skill, else the installed skill)")
    args = ap.parse_args(argv)
    if args.command == "publish":
        args = with_inputs(args, load_inputs(inputs_path(_workspace())))
        if not args.room:
            print("getting_started: no --room, and no saved tutorial inputs name one", file=sys.stderr)
            return 2

    try:
        if args.command == "plan":
            print(json.dumps(plan(load_marker(marker_path(_workspace())), args.room)))
            return 0
        name = clean_name(resolve_name(args.name, args.agent_mxid))
        if args.command == "render":
            core = detect_core() if args.core == "auto" else args.core
            sys.stdout.write(render_page(name, core, args.audience, None, load_prompts(args.prompts),
                                         args.roles))
            return 0
    except ValueError as exc:
        print(f"getting_started: {exc}", file=sys.stderr)
        return 2
    script = args.room_commons or find_room_commons()
    if not script.is_file():
        print(json.dumps({"ok": False, "reason": "room-commons not installed", "path": str(script)}))
        return 3
    try:
        env = commons_env(script, room=args.room)
    except Exception as exc:  # noqa: BLE001 - the room's server has no usable connection
        print(json.dumps({"ok": False, "reason": redact(str(exc), os.environ)}))
        return 4
    try:
        result = publish(Commons(script, args.room, env), marker_path(_workspace()),
                         name, args.owner, args.summon,
                         detect_core() if args.core == "auto" else args.core, args.audience,
                         args.agent_mxid or agent_mxid() or None, load_prompts(args.prompts),
                         args.roles)
    except (PublishError, ValueError) as exc:
        print(json.dumps({"ok": False, "reason": redact(str(exc), env)}))
        return 4
    print(json.dumps({**result, "name": name}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
