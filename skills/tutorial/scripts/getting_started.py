#!/usr/bin/env python3
"""The owner's "Getting started" page in AG2 Space: render it, and create or refresh it in one room.

  getting_started.py render --name NAME|auto [--core claude|codex|unknown|auto]   -> the page's HTML
  getting_started.py plan --room ROOM                   -> {"action": "create"|"update", "kind": ...}
  getting_started.py publish --room ROOM --name NAME|auto [--owner MXID] [--core ...] [--summon]
      [--room-commons PATH]
      -> {"ok": true, "action": "created"|"updated"|"unchanged", "kind": "html-<id>", "summoned": bool,
          "name": the name used}

The page (tour_page.py) is a short interactive AG2 Space tour: eight sections, plus a Claude Code
history import section when the core is Claude Code (`--core auto` asks the engine's config
resolver; unknown omits it). `--name auto` uses this agent's Matrix profile displayname, else a
name made from its mxid's localpart, else "Sutando"; it never fails.

room_commons.py is found beside this skill, else in the installed skills directory. Its
credentials come from the environment, else from the AG2 Space channel env file
(src/channel_credentials.py); nothing here prints them.

One page per room. The marker at <workspace>/state/tutorial/getting-started.json records the page's
kind per room, so a re-link or reinstall rewrites that page instead of adding a second one. The page
holds the agent's name and fixed text only; nothing read from the owner's accounts goes on it.

Exit codes: 0 done, 2 bad arguments, 3 the room-commons skill is not installed, 4 a room-commons
call failed (the marker is unchanged; an empty page this run added is removed again).
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
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tour_page  # noqa: E402,F401
from tour_page import (  # noqa: E402,F401  (the page lives in tour_page; these names are this CLI's API)
    CONNECT_PROMPT, CORES, IMPORT_PROMPT, IMPORT_SECTION, NAME_MAX, PAGE_TITLE, SECTIONS, clean_name,
    render_page, sections_for,
)

SUMMON_CONTEXT = "A short tour of AG2 Space, with one thing to try in each part."
PROFILE_TIMEOUT = 5
# Each piece is one argv string: under Linux's 131,072-byte per-argument cap and Room Commons'
# 196,608-byte write.
CHUNK_BYTES = 120_000
USER_AGENT = "sutando-tutorial/1"
FALLBACK_NAME = "Sutando"
MXID = re.compile(r"^@([^:\s]+):([A-Za-z0-9.\-]+(?::\d+)?)$")


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
    return {r: v for r, v in rooms.items()
            if isinstance(v, dict) and isinstance(v.get("kind"), str) and v["kind"].startswith("html-")}


def save_marker(path: Path, rooms: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".getting-started.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "rooms": rooms}, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, str(path))


def plan(rooms: Dict[str, dict], room: str, listed_kinds: Optional[List[str]] = None) -> dict:
    """Create unless this room already has a page; a recorded page no longer listed is created anew."""
    entry = rooms.get(room)
    if entry and (listed_kinds is None or entry["kind"] in listed_kinds):
        return {"action": "update", "kind": entry["kind"]}
    return {"action": "create", "kind": None}


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / ".getting-started.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


class Commons:
    """The room-commons CLI, one call per subprocess."""

    def __init__(self, script: Path, room: str, env: Optional[Dict[str, str]] = None):
        self.script, self.room, self.env = script, room, env

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
            raise PublishError(f"room-commons {args[0]} failed ({done.returncode}): "
                               f"{(done.stderr or done.stdout).strip()[:400]}")
        return done.stdout

    def listed_kinds(self) -> List[str]:
        pages = json.loads(self.run("pages", self.room, as_json=True))
        return [p["kind"] for p in pages if isinstance(p, dict) and p.get("id")]

    def add_page(self, title: str) -> str:
        kind = json.loads(self.run("page-add", self.room, title, as_json=True)).get("kind")
        if not isinstance(kind, str) or not kind.startswith("html-"):
            raise PublishError(f"room-commons page-add returned no html page kind: {kind!r}")
        return kind

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


def publish(commons: Commons, marker: Path, name: str, owner: Optional[str] = None,
            summon: bool = False, core: str = "unknown") -> dict:
    page = render_page(name, core)
    with _locked(marker):
        rooms = load_marker(marker)
        step = plan(rooms, commons.room, commons.listed_kinds() if commons.room in rooms else None)
        if step["action"] == "update":
            kind = step["kind"]
            current = commons.read(kind)
            if current == page:
                action = "unchanged"
            else:
                commons.rewrite(kind, current, page)
                action = "updated"
        else:
            kind = commons.add_page(PAGE_TITLE)
            try:
                commons.append(kind, page)
            except PublishError as exc:
                with contextlib.suppress(PublishError):
                    commons.run("page-remove", commons.room, kind)
                raise PublishError(f"{exc}; the empty page was taken off the list again") from None
            action = "created"
        rooms[commons.room] = {"kind": kind, "hash": content_hash(page),
                               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        save_marker(marker, rooms)
    result = {"ok": True, "action": action, "kind": kind, "summoned": False}
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


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="getting_started.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("render", help="print the page's HTML")
    r.add_argument("--name", required=True,
                   help="the agent's own name; auto: its Matrix profile name, else its mxid's localpart")
    r.add_argument("--agent-mxid", help="with --name auto: this agent's mxid (default $AGENT_MXID or the "
                                        "channel env file's)")
    r.add_argument("--core", choices=CORES + ("auto",), default="unknown",
                   help="the Sutando core; claude adds the Claude Code import section")
    p = sub.add_parser("plan", help="what publish would do in this room, from the marker alone")
    p.add_argument("--room", required=True)
    u = sub.add_parser("publish", help="create the page in this room, or refresh the one already there")
    u.add_argument("--room", required=True, help="the owner's DM room id")
    u.add_argument("--name", required=True,
                   help="the agent's own name; auto: its Matrix profile name, else its mxid's localpart")
    u.add_argument("--agent-mxid", help="with --name auto: this agent's mxid")
    u.add_argument("--owner", help="the owner's mxid; summoned to the page when it is created")
    u.add_argument("--core", choices=CORES + ("auto",), default="auto",
                   help="the Sutando core (default auto: what the engine resolves); claude adds the "
                        "Claude Code import section, unknown leaves it out")
    u.add_argument("--summon", action="store_true", help="summon the owner on a refresh too")
    u.add_argument("--room-commons", type=Path, default=None,
                   help="path to room_commons.py (default: beside this skill, else the installed skill)")
    args = ap.parse_args(argv)

    try:
        if args.command == "plan":
            print(json.dumps(plan(load_marker(marker_path(_workspace())), args.room)))
            return 0
        name = clean_name(resolve_name(args.name, args.agent_mxid))
        if args.command == "render":
            core = detect_core() if args.core == "auto" else args.core
            sys.stdout.write(render_page(name, core))
            return 0
    except ValueError as exc:
        print(f"getting_started: {exc}", file=sys.stderr)
        return 2
    script = args.room_commons or find_room_commons()
    if not script.is_file():
        print(json.dumps({"ok": False, "reason": "room-commons not installed", "path": str(script)}))
        return 3
    try:
        result = publish(Commons(script, args.room, commons_env(script)), marker_path(_workspace()),
                         name, args.owner, args.summon,
                         detect_core() if args.core == "auto" else args.core)
    except (PublishError, ValueError) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}))
        return 4
    print(json.dumps({**result, "name": name}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
