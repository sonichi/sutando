#!/usr/bin/env python3
"""The owner's "Getting started" page in AG2 Space: render it, and create or refresh it in one room.

  getting_started.py render --name NAME|auto [--core claude|codex|unknown|auto] [--list PATH|URL]
      -> the page's HTML
  getting_started.py plan --room ROOM                   -> {"action": "create"|"update", "kind": ...}
  getting_started.py publish [--room ROOM] [--name NAME|auto] [--owner MXID] [--core ...] [--summon]
      [--room-commons PATH] [--list PATH|URL] [--media PATH] [--if-newer]
      -> {"ok": true, "action": "created"|"updated"|"unchanged", "kind": "html-<id>", "summoned": bool,
          "name": the name used, "revision": the feature list's, "list": "file" or the --list source,
          "media": the asset names the page points at room media for}

The page (tour_page.py) is a short interactive AG2 Space tour rendered from the skill's feature
list (feature-list.json: nine steps for every core, plus the Claude Code history import step when
the core is Claude Code; `--core auto` asks the engine's config resolver, unknown omits it).
`--list` renders from another list instead, a file or an https URL (the backend's served list,
once it exists); a list that cannot be read or does not validate is reported on stderr and the
skill's own file is used. Nothing is fetched unless --list names a URL. `--name auto` uses this
agent's Matrix profile displayname, else a name made from its mxid's localpart, else "Sutando";
it never fails.

Each screenshot in the list is an asset reference. `--media PATH` (a JSON object {asset name:
"mxc://..."}, the room media an upload of that capture returned) makes the page point at the
room's media for those assets; the map is recorded in the room's entry and reused on later runs
there, and an asset with no mapping is inlined from its fallback file, as every one is today.

The page records the list's revision in its ownership tag. `publish` rewrites a page whose text
differs from what it would write (a new name, core, list or prompts), unless someone edited the page
since it was last written: that page is kept (action "kept", a line on stderr) and its entry stands.
With --if-newer it rewrites a complete page only when the list's revision is newer than the page's,
reusing the prompts and roles the page was last built with, so a pass after an engine update
refreshes stale pages and leaves current ones alone. A page built before lists had a revision counts
as older than any list. A rewrite is journalled as `updating` before its first write: the target
page, the page before it, the page after each call, and the entry before it. The next run settles
it from that journal before rendering anything: a page the rewrite left behind is finished to the
target byte for byte (action "resumed", reporting the journalled name and revision; the run's own
inputs, --summon included, wait for the next run), a page still as before means the rewrite never
ran, and any other page is someone's edit, kept with the entry from before the rewrite. Every call
of a rewrite first reads the page and runs only on the page the previous call leaves; the page is
read back against the target before success is recorded. `publish`
refuses to run without a well-formed agent identity (--agent-mxid, $AGENT_MXID or the channel env
file), since a page without the ownership tag cannot be found again.

room_commons.py is found beside this skill, else in the installed skills directory. Its
credentials come from the environment, else from the AG2 Space channel env file
(src/channel_credentials.py); nothing here prints them.

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
    AUDIENCES, CONNECT_PROMPT, PROMPT_SLOTS, clean_prompts, owner_marker, CORES, IMPORT_PROMPT, NAME_MAX, PAGE_TITLE, SECTIONS, clean_name,
    render_page, sections_for, FeatureList, LIST_PATH, clean_media, load_list, newer, parse_list, read_marker,
    role_labels, LIST_MAX_BYTES, read_regular, page_digest_ok, page_shape_ok, strip_digest,
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
LIST_TIMEOUT = 10
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


def capability_names(script: Path) -> tuple:
    """The credential variables room_commons.py reads (its URL_VARS and TOKEN_VARS); () when it
    cannot be loaded, which leaves the credentials to it alone."""
    try:
        sys.path.insert(0, str(script.parent))
        spec = importlib.util.spec_from_file_location("_tutorial_room_commons", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001
        return ()
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(script.parent))
    return (*getattr(mod, "URL_VARS", ()), *getattr(mod, "TOKEN_VARS", ()))


def connect_actions(script: Path, env: Optional[Dict[str, str]] = None):
    """room-commons' own Actions client (hosted MCP), opened with the credentials `env` names;
    the process environment is restored once the client holds its token."""
    import argparse as _argparse
    sys.path.insert(0, str(script.parent))
    saved = dict(os.environ)
    try:
        os.environ.update({k: v for k, v in (env or {}).items() if k not in os.environ})
        spec = importlib.util.spec_from_file_location("room_commons", script)
        rc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rc)
        return rc.mcp_door(_argparse.Namespace(token=None, workspace=None, url=None))()
    finally:
        os.environ.clear()
        os.environ.update(saved)
        with contextlib.suppress(ValueError):
            sys.path.remove(str(script.parent))


def commons_env(script: Path, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment for room_commons.py: this one, plus its credentials from the AG2 Space
    channel env file when the environment names none of them."""
    from channel_credentials import channel_credentials
    env = dict(os.environ if environ is None else environ)
    env.update(channel_credentials(capability_names(script), env))
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
    """This agent's mxid: $AGENT_MXID, else the AG2 Space channel env file the room tooling
    resolves, else the launcher-named device file or channels/ag2space/.env read directly (a file
    that holds the identity but no usable token); '' when none names one."""
    from channel_credentials import channel_value
    from channel_token import token_from_env_file
    from util_paths import claude_home_path
    env = os.environ if environ is None else environ
    found = channel_value("AGENT_MXID", env)
    if found:
        return found
    files = [Path(p) for p in ((env.get("AG2_DEVICE_ENV") or "").strip(),) if p]
    with contextlib.suppress(Exception):
        files.append(claude_home_path("channels") / "ag2space" / ".env")
    return next((v for v in (token_from_env_file("AGENT_MXID", f) for f in files if f.is_file()) if v), "")


def resolve_name(name: str, mxid: Optional[str] = None,
                 fetch: Optional[Callable[[str], bytes]] = None) -> str:
    """`name` itself, or for `auto`: the profile displayname, else the localpart, else Sutando."""
    if name != "auto":
        return name
    mxid = mxid if mxid is not None else agent_mxid()  # callers that resolved the identity pass it
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


class RedirectRefused(Exception):
    pass


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another https URL; urllib's default also follows http and ftp."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            fp.close()
            raise RedirectRefused(f"redirect to {safe_source(newurl)} refused; only https is fetched")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def list_opener(*handlers) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_HttpsRedirects(), *handlers)


def safe_source(source: str) -> str:
    """A list source as it may be printed: a URL keeps scheme, host, port and path only (no
    userinfo, query or fragment); a file path is returned as given. Never raises, so a malformed
    URL is still reported and falls back like any other."""
    if "://" not in source:
        return source
    scheme, _, rest = source.partition("://")
    authority, slash, path = rest.partition("/")
    authority = authority.rsplit("@", 1)[-1]
    return f"{scheme}://{authority}{slash}{path}".split("?", 1)[0].split("#", 1)[0]


def fetch_list(url: str, opener: Optional[urllib.request.OpenerDirector] = None) -> FeatureList:
    """The feature list served at an https URL, validated like the file. Only https is fetched,
    redirects included."""
    if not url.startswith("https://"):
        raise ValueError(f"a served feature list must be an https URL, got {safe_source(url)}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with (opener or list_opener()).open(req, timeout=LIST_TIMEOUT) as resp:
            raw = resp.read(LIST_MAX_BYTES + 1)
    except RedirectRefused as exc:
        raise ValueError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - any transport failure falls back to the file
        raise ValueError(f"cannot fetch the feature list: {type(exc).__name__}") from None
    if len(raw) > LIST_MAX_BYTES:
        raise ValueError(f"the served feature list is over {LIST_MAX_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ValueError(f"the served feature list is not JSON: {exc}") from None
    return parse_list(data)


def resolve_list(source: Optional[str], note: Callable[[str], None] = lambda s: None) -> tuple:
    """(features, label): the list `source` names (a file path, or an https URL), else the skill's
    own file. A source that cannot be read or does not validate is reported through `note` and the
    file is used; the label says which list was used."""
    if source:
        try:
            got = fetch_list(source) if "://" in source else load_list(Path(source))
            return got, safe_source(source)
        except ValueError as exc:
            note(f"getting_started: --list {safe_source(source)} not used ({exc}); using the skill's own list")
    return tour_page.FEATURES, "file"


def load_prompts(path: Optional[Path], slots: Optional[tuple] = None) -> Dict[str, str]:
    """The usable prompts in a --prompts JSON file for `slots` (the skill's own list's by default);
    {} when there is none or it cannot be read, so a bad file never fails the run (every slot keeps
    its default)."""
    if path is None:
        return {}
    try:
        return clean_prompts(json.loads(read_regular(path, LIST_MAX_BYTES, "prompts file").decode("utf-8")), slots)
    except ValueError:
        return {}


def load_media(path: Optional[Path]) -> Dict[str, str]:
    """The raw asset-name to mxc map in a --media JSON file; {} when there is none or it cannot be
    read. `clean_media` keeps only the list's names and well-formed URIs."""
    if path is None:
        return {}
    try:
        got = json.loads(read_regular(path, LIST_MAX_BYTES, "media file").decode("utf-8"))
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


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
    mark = read_marker(text)
    return bool(mark) and mark[0] == agent


def plan(rooms: Dict[str, dict], room: str, pages: Optional[List[dict]] = None) -> dict:
    """Rewrite the recorded page while it is listed (`pending` when its first write never
    finished, or an `updating` rewrite was cut short); with no usable entry, reconcile against
    unrecorded candidates, else create."""
    entry = rooms.get(room)
    if entry and entry.get("state") == "creating":
        return {"action": "resume", "kind": None, "pending": True, "operation_id": entry["operation_id"]}
    if entry and (pages is None or entry["kind"] in [p.get("kind") for p in pages]):
        return {"action": "update", "kind": entry["kind"],
                "pending": entry.get("state") in ("pending", "updating")}
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
        """Replace the page's text with the calls `rewrite_steps` lists, in that order."""
        for cmd, args, _ in rewrite_steps(old, new, env=self.env):
            self.run(cmd, self.room, *args, kind=kind)


def call_budget(env: Optional[Mapping[str, str]] = None) -> tuple:
    """(bytes one argument may hold, bytes left for the two texts of one call) on this host: Linux
    caps an argument at 131,072 bytes; every host caps argv plus environment at ARG_MAX."""
    env = os.environ if env is None else env
    total = os.sysconf("SC_ARG_MAX") if hasattr(os, "sysconf") else 262_144
    used = sum(len(str(k).encode("utf-8")) + len(str(v).encode("utf-8")) + 2 for k, v in env.items()) + 8192
    per_arg = 131_071 if sys.platform.startswith("linux") else total
    return per_arg, max(0, total - used)


def fits_call(a: str, b: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether two texts fit the two arguments of one room-commons call on this host."""
    x, y = len(a.encode("utf-8")), len(b.encode("utf-8"))
    per_arg, total = call_budget(env)
    return x <= per_arg and y <= per_arg and x + y <= total


def one_call(old: str, new: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether `old` -> `new` fits one replace: the needle is then the whole page, so a page that
    changed inside it since it was read is refused as a whole and nothing in between is written."""
    return bool(old) and fits_call(old, new, env)


def piece_limit(env: Optional[Mapping[str, str]] = None) -> int:
    """Bytes one piece of a piecewise write may hold on this host: CHUNK_BYTES, or less when one
    argument may hold less."""
    return max(1, min(CHUNK_BYTES, call_budget(env)[0]))


def rewrite_steps(old: str, new: str, whole: Optional[bool] = None,
                  env: Optional[Mapping[str, str]] = None, piece: int = 0) -> List[tuple]:
    """The room-commons calls that take a page from `old` to `new`, each with the text the page
    holds once it has run: one `replace` of the whole page when it fits a call (`one_call`), else
    `old` cleared piece by piece (each needle a whole piece), then `new` appended in pieces.
    `Commons.rewrite` runs exactly this list, so a journal knows every state a run can leave."""
    whole = one_call(old, new, env) if whole is None else whole
    if whole and old:
        return [("replace", (old, new), new)]
    piece = piece or piece_limit(env)
    steps, text = [], old
    for part in chunks(old, piece):
        text = text.replace(part, "", 1)
        steps.append(("replace", (part, ""), text))
    for part in chunks(new, piece):
        text += part
        steps.append(("append", (part,), text))
    return steps


def fill_steps(first: str, page: str, piece: int = 0) -> List[tuple]:
    """The appends that finish a page whose create wrote `first`, its first piece."""
    steps, text = [], first
    for part in chunks(page[len(first):], piece or CHUNK_BYTES):
        text += part
        steps.append(("append", (part,), text))
    return steps


def _record(marker: Path, rooms: Dict[str, dict], room: str, entry: dict) -> None:
    """Write this room's entry. A marker that cannot be written fails the run; the next run resumes
    from the last entry written, or finds the page again by its ownership marker."""
    rooms[room] = {**entry, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        save_marker(marker, rooms)
    except OSError as exc:
        raise PublishError(f"could not record {entry.get('kind') or entry['state']} in {marker}: {exc}") from None


def _entry(kind: Optional[str], page: str, features: FeatureList, prompts: Dict[str, str], roles: object,
           media: Dict[str, str]) -> dict:
    """A room's complete entry for `page` and the inputs it was built from."""
    entry = {"kind": kind, "state": "complete", "hash": content_hash(page),
             "revision": (read_marker(page) or (None, features.revision))[1]}
    if prompts:
        entry["prompts"] = prompts
    if role_labels(roles):
        entry["roles"] = ", ".join(role_labels(roles))
    if media:
        entry["media"] = media
    return entry


# What a journal (an `updating` rewrite, or a `creating`/`pending` create) carries beyond the
# complete entry it becomes; `done` is the number of calls that ran, `mode` fill or rewrite.
JOURNAL_KEYS = ("target", "before", "source", "steps", "done", "previous", "mode", "whole", "piece", "name", "core",
                "audience", "agent")
HEAD_CHARS = 64  # the page's opening characters: the needle when text is put back at its front


def _steps(journal: dict) -> List[tuple]:
    piece = journal.get("piece") or CHUNK_BYTES
    if journal.get("mode") == "fill":
        return fill_steps(journal["before"], journal["target"], piece)
    return rewrite_steps(journal["before"], journal["target"], whole=bool(journal.get("whole")), piece=piece)


def _journal(entry: dict, current: str, page: str, previous: Optional[dict], name: str, core: str,
             audience: str, agent: Optional[str], mode: str = "rewrite",
             env: Optional[Mapping[str, str]] = None) -> dict:
    journal = {**entry, "state": "updating", "target": page, "before": current, "source": content_hash(current),
               "done": 0, "mode": mode, "whole": mode == "rewrite" and one_call(current, page, env),
               "piece": piece_limit(env), "previous": previous or None, "name": name, "core": core,
               "audience": audience, "agent": agent}
    journal["steps"] = [content_hash(text) for _, _, text in _steps(journal)]
    return journal


def _complete(journal: dict) -> dict:
    return {**{k: v for k, v in journal.items() if k not in JOURNAL_KEYS + ("updated_at", "operation_id")},
            "state": "complete"}


def _restore(marker: Path, rooms: Dict[str, dict], room: str, entry: dict) -> None:
    """Write `entry` as it was, timestamp included."""
    rooms[room] = entry
    try:
        save_marker(marker, rooms)
    except OSError as exc:
        raise PublishError(f"could not restore the record of {entry.get('kind')} in {marker}: {exc}") from None


def _stand_aside(marker: Path, rooms: Dict[str, dict], room: str, journal: dict, now: str,
                 undo: Optional[str] = None) -> None:
    """The page is someone else's now: the entry from before the rewrite stands, literally, with a
    note of how far the rewrite got; a page this script never finished is recorded as foreign (its
    hash, never rewritten unless a render equals it). Nothing of the target counts as written."""
    note = {"interrupted": {"done": journal.get("done", 0), "target": journal.get("hash"), "page": now,
                            **({"undo": undo} if undo else {})}}
    if journal.get("previous"):
        _restore(marker, rooms, room, {**journal["previous"], **note})
    else:
        _record(marker, rooms, room, {"kind": journal["kind"], "state": "complete", "hash": now, "foreign": True,
                                      "revision": (read_marker(journal.get("target", "")) or (None, None))[1],
                                      **note})


def _undo(commons: Commons, kind: str, step: tuple, found: str) -> str:
    """Take back one call whose page read back wrong, so the page is again what the other writer
    had, their edit included: a whole-page replace is replaced back; an appended piece is removed;
    a cleared piece is put back at the front, in pieces each fitting a call, before the page's
    first characters. `undone`, `unfit` (no write: a piece cannot fit this host's call budget) or
    `failed` (a needle was gone by the time it was tried)."""
    cmd, args, left = step
    if cmd == "append" or args[1]:  # an appended piece, or the whole new page
        needle, back = (args[0], "") if cmd == "append" else (args[1], args[0])
        if not fits_call(needle, back, commons.env):
            return "unfit"
        try:
            commons.run("replace", commons.room, needle, back, kind=kind)
        except PublishError:
            return "failed"
        return "undone"
    per_arg, total = call_budget(commons.env)
    head = found[:HEAD_CHARS].encode("utf-8")
    room = min(per_arg, total - 2 * len(head)) - len(head)
    if room <= 0:
        return "unfit"
    for piece in reversed(chunks(args[0], room)):
        current = commons.read(kind)
        head = current[:HEAD_CHARS]
        try:
            if head:
                commons.run("replace", commons.room, head, piece + head, kind=kind)
            else:
                commons.run("append", commons.room, piece, kind=kind)
        except PublishError:
            return "failed"
    return "undone"


def _advance(commons: Commons, marker: Path, rooms: Dict[str, dict], room: str) -> str:
    """Run the journalled calls from the one the journal records. Each call runs only on the page
    the previous call leaves, is refused by Commons when its needle is gone, and is read back
    before the next; a page in no state of the journal stops everything before the next write.
    `finished` once the page reads back as the target, `edited` once it is someone else's."""
    journal = rooms[room]
    kind, steps = journal["kind"], _steps(journal)
    expected = [journal["source"], *journal["steps"]]  # expected[i]: the page's hash before call i
    i = journal.get("done", 0)
    now = content_hash(commons.read(kind))  # read again here: the check is right before the write
    while i < len(steps):
        if now == expected[i + 1]:
            i += 1  # the call applied and its answer was lost
        elif now != expected[i]:
            _stand_aside(marker, rooms, room, journal, now)
            return "edited"
        else:
            cmd, args, _ = steps[i]
            try:
                commons.run(cmd, commons.room, *args, kind=kind)
            except PublishError:
                now = content_hash(commons.read(kind))
                if now == expected[i]:
                    raise  # the page is as expected, so the call itself failed: resumable as is
                if now != expected[i + 1]:
                    _stand_aside(marker, rooms, room, journal, now)  # refused: the page had changed
                    return "edited"
            else:
                now = content_hash(commons.read(kind))
                if now != expected[i + 1]:
                    # Someone wrote outside the needle meanwhile: this call is taken back when it can
                    # be, and the page is theirs, as it stands, with how far the undo got noted.
                    undo = _undo(commons, kind, steps[i], steps[i - 1][2] if i else journal["before"])
                    _stand_aside(marker, rooms, room, journal,
                                 now if undo == "unfit" else content_hash(commons.read(kind)), undo)
                    return "edited"
            i += 1
        journal = {**journal, "done": i}
        _record(marker, rooms, room, journal)
    if now != journal["hash"]:
        _stand_aside(marker, rooms, room, journal, now)
        return "edited"
    _record(marker, rooms, room, _complete(journal))
    return "finished"


def _settle(commons: Commons, marker: Path, rooms: Dict[str, dict], room: str) -> str:
    """What the page of a journal holds now, from the journal alone: `finished` once it is the
    target (written here, call by call, when the page is where the journal left it), `untouched`
    when a rewrite never ran, `edited` when the page is in no state of the journal."""
    journal = rooms[room]
    kind, target, before = journal["kind"], journal.get("target"), journal.get("before")
    if not isinstance(target, str) or content_hash(target) != journal.get("hash") \
            or not isinstance(before, str) or content_hash(before) != journal.get("source"):
        raise PublishError(f"{kind} is recorded as {journal.get('state')} but its journal does not hash to itself; "
                           "it stays recorded")
    now = content_hash(commons.read(kind))
    if now == journal["hash"]:
        _record(marker, rooms, room, _complete(journal))
        return "finished"
    if journal.get("state") == "updating" and now == journal["source"] and not journal.get("done"):
        return "untouched"
    return _advance(commons, marker, rooms, room)


def _recover(commons: Commons, marker: Path, rooms: Dict[str, dict], owner: Optional[str]) -> Optional[dict]:
    """Settle this room's journal, if any, from the journal, the room and the identity alone, before
    anything of a run's own request is read or rendered. The result of a run that settled one, or
    None when there is nothing to settle (a journal whose page is gone is dropped)."""
    journal = rooms.get(commons.room, {})
    if journal.get("state") not in ("updating", "pending", "creating") or "target" not in journal:
        return None
    pages = commons.listed_pages()
    if journal["state"] == "creating":
        # The create may have landed and its answer been lost: a page this agent tagged is that page.
        found = commons.reconcile(pages, journal.get("agent")) if candidates(pages) else None
        if not found:
            return None  # publish re-issues the create with the same operation id
        journal = {**journal, "kind": found, "state": "pending"}
        _record(marker, rooms, commons.room, journal)
    elif journal["kind"] not in [p.get("kind") for p in pages]:
        if journal.get("previous"):
            _restore(marker, rooms, commons.room, journal["previous"])
        else:
            rooms.pop(commons.room, None)
            save_marker(marker, rooms)
        return None
    creating = journal["state"] == "pending"
    settled = _settle(commons, marker, rooms, commons.room)
    if settled == "untouched":
        _restore(marker, rooms, commons.room, journal["previous"])
        return None
    if settled == "edited":
        kept = rooms[commons.room]
        note = kept.get("interrupted") or {}
        return {"ok": True, "action": "kept", "edited": True, "kind": journal["kind"], "summoned": False,
                "revision": kept.get("revision"), "media": sorted(kept.get("media") or {}),
                **({"landed": note["page"]} if note.get("undo") and note["undo"] != "undone" else {})}
    result = {"ok": True, "action": "resumed", "resumed": True, "kind": journal["kind"], "summoned": False,
              "revision": journal.get("revision"), "media": sorted(journal.get("media") or {}),
              **{k: journal.get(k) for k in ("name", "core", "audience", "agent")}}
    if creating and owner:  # a creation finishing: the owner was never summoned to it
        try:
            commons.run("summon", commons.room, owner, "--context", SUMMON_CONTEXT, "--page-title", PAGE_TITLE,
                        kind=journal["kind"])
            result["summoned"] = True
        except PublishError as exc:
            result["summon_error"] = str(exc)
    return result


def recover(commons: Commons, marker: Path, owner: Optional[str] = None) -> Optional[dict]:
    """`publish`'s first step on its own: finish or stand aside from a run cut short in this room.
    Needs nothing of a request but the room and the owner to summon."""
    with _locked(marker):
        return _recover(commons, marker, load_marker(marker), owner)


def publish(commons: Commons, marker: Path, name: str, owner: Optional[str] = None,
            summon: bool = False, core: str = "unknown", audience: str = "dm",
            agent: Optional[str] = None, prompts: object = None, roles: object = None,
            features: Optional[FeatureList] = None, if_newer: bool = False,
            media: object = None) -> dict:
    features = features or tour_page.FEATURES
    slots = tour_page.prompt_slots(features)
    with _locked(marker):
        rooms = load_marker(marker)
        settled = _recover(commons, marker, rooms, owner)
        if settled:
            return settled
        if if_newer and not clean_prompts(prompts, slots):
            # No one is in the loop to pass the owner's prompts again: keep the page's own.
            prompts = rooms.get(commons.room, {}).get("prompts")
        if if_newer and not role_labels(roles):
            roles = rooms.get(commons.room, {}).get("roles")
        prompts = clean_prompts(prompts, slots)
        recorded = rooms.get(commons.room, {}).get("media")
        media = clean_media({**(recorded if isinstance(recorded, dict) else {}),
                             **(media if isinstance(media, dict) else {})}, features)
        page = render_page(name, core, audience, agent, prompts, roles, features, media)
        entry = _entry(None, page, features, prompts, roles, media)
        pages = commons.listed_pages()
        step = plan(rooms, commons.room, pages)
        adopted = upgrade = False
        if step["action"] == "reconcile":
            found = commons.reconcile(pages, agent)
            if found:
                # A page found by its tag is this agent's as written only when its own digest says
                # so; otherwise it is adopted as it stands and never rewritten.
                live = commons.read(found)
                tag = read_marker(live)
                ours = page_digest_ok(live)
                if ours is None and tag and tag[1]:
                    # From before tags carried a digest: ours when it is this render less the
                    # digest, or the only tutorial page here and shaped exactly as one is written.
                    ours = upgrade = live == strip_digest(page) or (page_shape_ok(live) and len(candidates(pages)) == 1)
                _record(marker, rooms, commons.room,
                        {"kind": found, "state": "complete", "hash": content_hash(live), "revision": tag[1] if tag else None,
                         **({} if ours else {"foreign": True})})
                step, adopted = {"action": "update", "kind": found, "pending": False}, True
            else:
                step = {"action": "create", "kind": None, "pending": False}
        kind = step["kind"]
        if step["action"] in ("create", "resume"):
            op = step.get("operation_id") or f"tutorial-{uuid.uuid4().hex}"
            first, earlier = chunks(page, piece_limit(commons.env))[0], rooms.get(commons.room, {}).get("before")
            journal = _journal(entry, first, page, None, name, core, audience, agent, mode="fill")
            _record(marker, rooms, commons.room, {**journal, "state": "creating", "operation_id": op})
            kind = commons.create_page(PAGE_TITLE, first, op)
            live = commons.read(kind)
            if live != first:
                # The same operation id answered an earlier attempt's page: its first piece is known
                # from that journal and rewritten from there; any other text is not ours to replace.
                if live == earlier:
                    journal = _journal(entry, live, page, None, name, core, audience, agent, env=commons.env)
                else:
                    _record(marker, rooms, commons.room, {"kind": kind, "state": "complete", "hash": content_hash(live),
                                                          "foreign": True})
                    return {"ok": True, "action": "kept", "edited": True, "kind": kind, "summoned": False,
                            "revision": features.revision, "media": sorted(media)}
            _record(marker, rooms, commons.room, {**journal, "kind": kind, "state": "pending", "operation_id": op})
            try:
                outcome = _advance(commons, marker, rooms, commons.room)
            except PublishError as exc:
                raise PublishError(f"{exc}; {kind} stays recorded as pending and the next run "
                                   "finishes it") from None
            action = "created" if outcome == "finished" else "kept"
            current = page if outcome == "finished" else None
        else:
            current = commons.read(kind)
            was = read_marker(current)
            page_revision = was[1] if was else None
            known = rooms.get(commons.room, {})
            written = None if known.get("foreign") or step["pending"] else known.get("hash")
            if current == page:
                action = "unchanged"
            elif not written or content_hash(current) != written:
                action = "kept"  # a page this script cannot vouch for, or edited since: theirs stays
            elif if_newer and not upgrade and not newer(features.revision, page_revision):
                action = "unchanged"
            else:
                # Every call of the rewrite checks the page it expects and is read back; a run cut
                # short finishes or stands aside from the call the journal recorded.
                _record(marker, rooms, commons.room,
                        _journal({**entry, "kind": kind}, current, page, known, name, core, audience, agent,
                                 env=commons.env))
                action = "updated" if _advance(commons, marker, rooms, commons.room) == "finished" else "kept"
        # A page equal to this run's render records its inputs (a revision-only bump catches up);
        # a gated unchanged or kept run records nothing: inputs that were not applied stay out.
        if action in ("created", "updated") or action == "unchanged" and current == page:
            _record(marker, rooms, commons.room, _entry(kind, page, features, prompts, roles, media))
    result = {"ok": True, "action": action, "kind": kind, "summoned": False, "revision": features.revision,
              "media": sorted(media)}
    if adopted:
        result["adopted"] = True
    if action == "kept":
        result["edited"] = True
        note = rooms.get(commons.room, {}).get("interrupted") or {}
        if note.get("undo") and note["undo"] != "undone":
            result["landed"] = note["page"]  # the page as left: a write landed on it and stayed
    # Summoned once, when this page is created; a creation finished by a later run summons there.
    if owner and (summon or action == "created"):
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
    parser.add_argument("--media", type=Path, metavar="PATH",
                        help="JSON object {asset name: \"mxc://...\"}: the room media to point those "
                             "screenshots at; an asset not mapped is inlined from its fallback file")
    parser.add_argument("--list", dest="feature_list", metavar="PATH|URL",
                        help=f"render from this feature list (a JSON file, or an https URL) instead of "
                             f"{LIST_PATH.name}; one that cannot be read or does not validate is reported "
                             "on stderr and the skill's own file is used")


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
    u.add_argument("--if-newer", action="store_true",
                   help="rewrite a complete page only when the feature list's revision is newer than the "
                        "page's (a pass after an engine update); the prompts it was built with are kept")
    u.add_argument("--room-commons", type=Path, default=None,
                   help="path to room_commons.py (default: beside this skill, else the installed skill)")
    args = ap.parse_args(argv)
    if args.command == "publish":
        args = with_inputs(args, load_inputs(inputs_path(_workspace())))
        if not args.room:
            print("getting_started: no --room, and no saved tutorial inputs name one", file=sys.stderr)
            return 2

    if args.command == "plan":
        print(json.dumps(plan(load_marker(marker_path(_workspace())), args.room)))
        return 0
    # The identity is resolved, trimmed and checked once; the name, the tag and the journal use it.
    agent = (args.agent_mxid or agent_mxid() or "").strip()
    if args.command == "render":
        try:
            name = clean_name(resolve_name(args.name, agent))
            features, listed = resolve_list(args.feature_list, lambda s: print(s, file=sys.stderr))
            prompts = load_prompts(args.prompts, tour_page.prompt_slots(features))
            core = detect_core() if args.core == "auto" else args.core
            sys.stdout.write(render_page(name, core, args.audience, None, prompts, args.roles, features,
                                         load_media(args.media)))
        except ValueError as exc:
            print(f"getting_started: {exc}", file=sys.stderr)
            return 2
        return 0
    script = args.room_commons or find_room_commons()
    if not script.is_file():
        print(json.dumps({"ok": False, "reason": "room-commons not installed", "path": str(script)}))
        return 3
    env = commons_env(script)
    if not agent:
        # A page without the ownership tag can never be found again: refuse rather than write one.
        print(json.dumps({"ok": False, "reason": "no agent identity: pass --agent-mxid, or set AGENT_MXID in the "
                                                 "environment or the AG2 Space channel env file"}))
        return 4
    if not MXID.fullmatch(agent):
        print(json.dumps({"ok": False, "reason": f"the agent identity {agent[:80]!r} is not an mxid "
                                                 "(@localpart:server); nothing was published"}))
        return 4
    commons, marker = Commons(script, args.room, env), marker_path(_workspace())
    try:
        # A run cut short in this room is settled first, before this run's name, list, prompts or
        # media are even looked at; settling it is then this run's whole work.
        result = recover(commons, marker, args.owner)
    except (PublishError, ValueError) as exc:
        print(json.dumps({"ok": False, "reason": redact(str(exc), env)}))
        return 4
    if result is None:
        try:
            name = clean_name(resolve_name(args.name, agent))
            features, listed = resolve_list(args.feature_list, lambda s: print(s, file=sys.stderr))
            prompts = load_prompts(args.prompts, tour_page.prompt_slots(features))
            media = load_media(args.media)
        except ValueError as exc:
            print(f"getting_started: {exc}", file=sys.stderr)
            return 2
        try:
            result = publish(commons, marker, name, args.owner, args.summon,
                             detect_core() if args.core == "auto" else args.core, args.audience,
                             agent, prompts, args.roles, features, args.if_newer, media)
        except (PublishError, ValueError) as exc:
            print(json.dumps({"ok": False, "reason": redact(str(exc), env)}))
            return 4
        result = {**result, "name": name, "list": listed}
    if result["action"] == "resumed":
        print(f"getting_started: finished the rewrite of {result['kind']} in {args.room} that an earlier run "
              "left cut short, from its record; this run's own inputs were not applied; run again to apply them",
              file=sys.stderr)
    if result.get("edited"):
        print(f"getting_started: {result['kind']} in {args.room} was edited since this script last wrote it, "
              "or is not a page it wrote; kept as is, not rewritten", file=sys.stderr)
        if result.get("landed"):
            print(f"getting_started: one write had landed on it before the edit was seen and could not be taken "
                  f"back; the page is as left (sha256 {result['landed']})", file=sys.stderr)
    print(json.dumps({"list": "journal", **result} if result.get("resumed") else result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
