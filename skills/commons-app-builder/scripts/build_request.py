#!/usr/bin/env python3
"""The build-request envelope the s.space panel sends, and the Action plan that answers it.

The panel sends one private agent task: a line of English, then a ```commons-app-build fence
holding the request (BUILD_REQUEST.md is the spec). This module parses and validates it, turns it
into the ordered Room Commons Actions that build or refine the app with every page write at most
WRITE_LIMIT bytes, and formats the one completion the panel waits for. Choosing and adapting the
page is the agent's judgement; transport, chunking, dedupe and the reply shape are exact here.

Usage (task.txt is the whole task file, headers included: they say who sent it and from where):
  build_request.py parse  <task.txt>                       -> the request as JSON, or exit 1
  build_request.py plan   <task.txt> [--adapt adapt.json]  -> claims the request, prints the plan
  build_request.py reply  <task.txt> --status done|failed [--reason R] [--page P] [--database D]
                          [--app-id A] [--line L]          -> prints the reply and records it
  build_request.py check  <plan.json> <n> <result.json>    -> exit 1 with the reason if step n's
                                                              result fails its check; 0 if it passes
  build_request.py chunks <page.html>                      -> chunk byte sizes, one per line
plan exits 3 printing the recorded reply when the request_id was already answered, and 4 when
another run holds it. Add --ledger DIR to keep the record somewhere other than the workspace.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

FENCE = "commons-app-build"
VERSION = 1
WRITE_LIMIT = 20_000            # bytes per page write: the bridge's per-write cap, kept on every write
DESCRIPTION_MAX = 2_000
TEMPLATES = ("tracker", "poll", "planner", "turn-game")
MODES = ("build", "refine")
CLAIM_TTL_S = 30 * 60            # a claim nobody finished stops blocking a retry after this

# The gateway flattens a task body onto one line, so the fence may be on one line too.
_OPENER_RE = re.compile(r"```" + re.escape(FENCE) + r"\s")
_PAGE_RE = re.compile(r"^html(-[a-z0-9]{8})?$")          # the client's own PAGE_RE (appsModel.ts)
_ROOM_RE = re.compile(r"^![^:\s]+:\S+$")
# A bare database id: the backend's DATABASE pattern (actions/commons/reads.py) without "db:".
_DB_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,119}$")
_APP_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,80}$")

# The skill's own folder, for its templates/; not the workspace.
SKILL_DIR = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
TEMPLATE_DIR = SKILL_DIR / "templates"
_SRC = Path(__file__).resolve().parents[3] / "src"  # lint-workspace-resolution: allow-repo-root
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Where the task-envelope key lives; None is the resolved workspace. Tests point it at a temp one.
WORKSPACE: Optional[Path] = None


class RequestError(ValueError):
    """A malformed request. The message is the completion's reason, so keep it short and plain."""


# --------------------------------------------------------------------------- who sent it

def headers(text: str) -> Dict[str, str]:
    """The headers the bridge attests. The gateway writes some below `task:`; they count only when
    the file declares that layout and its envelope stamp verifies (task_envelope)."""
    from task_envelope import attested_task_headers  # noqa: PLC0415
    return {k: str(v).strip() for k, v in attested_task_headers(text or "", WORKSPACE).headers.items()}


def authorize(meta: Dict[str, str]) -> str:
    """The owner, if the request came from the owner's DM with this agent.

    The fence is message text anyone could paste; the attested headers are the bridge's. Which
    room it may aim at is checked separately, against that room's live membership.
    """
    if meta.get("access_tier") != "owner":
        raise RequestError("only the owner can ask for a build")
    owner = meta.get("user_id", "")
    if meta.get("channel_kind") != "dm" or not owner.startswith("@"):
        raise RequestError("a build request must come from the owner's private chat with the agent")
    return owner


# --------------------------------------------------------------------------- the request

def extract(text: str) -> Dict[str, Any]:
    """The fenced JSON object in a task's text. A completion (it has "status") is not a request."""
    text = text or ""
    openers = list(_OPENER_RE.finditer(text))
    if not openers:
        raise RequestError(f"no ```{FENCE} block in the task")
    if len(openers) > 1:
        raise RequestError(f"more than one ```{FENCE} block in the task")
    # Decoded from the opener, so backticks inside a JSON string do not end the block.
    start = openers[0].end()
    start += len(text[start:]) - len(text[start:].lstrip())
    try:
        data, end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as error:
        raise RequestError(f"the {FENCE} block is not JSON ({error.msg})") from None
    if not text[end:].lstrip().startswith("```"):
        raise RequestError(f"the {FENCE} block is not closed")
    if not isinstance(data, dict):
        raise RequestError(f"the {FENCE} block is not a JSON object")
    if "status" in data and "mode" not in data:
        raise RequestError("this is a completion, not a request")
    return data


def validate(data: Dict[str, Any]) -> Dict[str, Any]:
    """The request with only the known keys; unknown keys are ignored, as the spec promises."""
    def text(key: str, required: bool = True) -> Optional[str]:
        value = data.get(key)
        if value is None or value == "":
            if required:
                raise RequestError(f"`{key}` is missing")
            return None
        if not isinstance(value, str):
            raise RequestError(f"`{key}` is not a string")
        return value

    if data.get("v") != VERSION:
        raise RequestError(f"`v` must be {VERSION}")
    request_id = text("request_id")
    try:
        uuid.UUID(request_id)
    except ValueError:
        raise RequestError("`request_id` is not a uuid") from None
    mode = text("mode")
    if mode not in MODES:
        raise RequestError("`mode` must be build or refine")
    description = text("description")
    if len(description) > DESCRIPTION_MAX:
        raise RequestError(f"`description` is over {DESCRIPTION_MAX} characters")
    room_id = text("room_id")
    if not _ROOM_RE.match(room_id):
        raise RequestError("`room_id` is not a room id")
    # The panel always creates the page; an absent one is malformed, never ours to create.
    page = text("page")
    if not _PAGE_RE.match(page):
        raise RequestError("`page` must be html or html-<id>")
    database = text("database", required=False)
    if database is not None:
        if database.startswith("db:"):
            raise RequestError("`database` must be the bare id, not db:<id>")
        if not _DB_RE.match(database):
            raise RequestError("`database` is not a database id")
    template = text("template", required=False)
    if template is not None and template not in TEMPLATES:
        raise RequestError("`template` must be one of " + ", ".join(TEMPLATES))
    app_id = text("app_id", required=False)
    if mode == "refine" and app_id is None:
        raise RequestError("`app_id` is required to refine")
    if app_id is not None and not _APP_RE.match(app_id):
        raise RequestError("`app_id` is not an app id")
    return {"v": VERSION, "request_id": request_id, "mode": mode, "description": description,
            "room_id": room_id, "page": page, "database": database, "template": template,
            "app_id": app_id}


def parse(text: str) -> Dict[str, Any]:
    """The validated request, authorized against the task file's headers."""
    request = validate(extract(text))
    request["owner"] = authorize(headers(text))
    return request


def request_id_of(text: str) -> Optional[str]:
    """The request_id even of a malformed request, so its failure can still be matched."""
    try:
        value = extract(text).get("request_id")
    except RequestError:
        return None
    return value if isinstance(value, str) else None


# --------------------------------------------------------------------------- the ledger

def default_ledger() -> Path:
    """<workspace>/state/commons-app-builds, resolved the way every skill resolves it."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))  # lint-workspace-resolution: allow-repo-root
    from workspace_default import resolve_workspace  # noqa: E402
    return Path(resolve_workspace()) / "state" / "commons-app-builds"


class Ledger:
    """One file per (room, request_id): claimed when work starts, holding the reply once sent.

    Durable, so a retry the panel sends after a restart still builds nothing; keyed per room, so a
    request_id seen in one room never answers for another. The claim is an exclusive create, so two
    runs of one request cannot both proceed.
    """

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def _path(self, room_id: str, request_id: str) -> Path:
        room = hashlib.sha256(room_id.encode("utf-8")).hexdigest()[:16]
        return self.folder / room / f"{uuid.UUID(request_id)}.json"

    def recorded(self, room_id: str, request_id: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self._path(room_id, request_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def claim(self, room_id: str, request_id: str,
              now: Optional[float] = None) -> Tuple[str, Optional[str]]:
        """("claimed", None), ("done", reply) or ("busy", None)."""
        now = time.time() if now is None else now
        path = self._path(room_id, request_id)
        with self._locked(path.parent):
            if path.exists():
                entry = self.recorded(room_id, request_id)
                if entry and entry.get("reply"):
                    return "done", entry["reply"]
                # Unreadable counts as busy until it ages out: only a dead claim is taken over.
                started = entry.get("claimed_at") if entry else None
                if not isinstance(started, (int, float)):
                    started = path.stat().st_mtime
                if now - started < CLAIM_TTL_S:
                    return "busy", None
            self._write(path, {"room_id": room_id, "request_id": request_id, "claimed_at": now})
            return "claimed", None

    def record(self, room_id: str, request_id: str, reply: str) -> None:
        path = self._path(room_id, request_id)
        with self._locked(path.parent):
            self._write(path, {"room_id": room_id, "request_id": request_id, "reply": reply,
                               "answered_at": time.time()})

    @staticmethod
    def _locked(folder: Path):
        """One room's claims and records, serialized across processes."""
        folder.mkdir(parents=True, exist_ok=True)
        return _FileLock(folder / ".lock")

    @staticmethod
    def _write(path: Path, entry: Dict[str, Any]) -> None:
        """Whole or not at all: a reader never sees a half-written entry."""
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, path)


class _FileLock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


# --------------------------------------------------------------------------- the template

class ActionFailed(RuntimeError):
    """An Action refused, or the build cannot be planned; its message becomes the reason."""


_PICK = (
    ("turn-game", r"\b(game|tic[- ]?tac|turn|board game|gomoku|checkers|chess|connect four)\b"),
    ("poll", r"\b(poll|vote|voting|survey|choose|pick (one|a)|preference|which (day|option))\b"),
    ("planner", r"\b(plan|planner|week|weekly|schedule|rota|calendar|itinerary|meal)\b"),
    ("tracker", r"\b(track|tracker|task|todo|to-do|issue|bug|request|queue|chore|status)\b"),
    # Last, so a planner or tracker word wins: a sign-up is one choice per person among fixed
    # slots, poll's model (templates/README.md).
    ("poll", r"\b(sign[- ]?ups?|sign-up sheet|rsvps?)\b"),
)


def pick_template(description: str, requested: Optional[str] = None,
                  adapted: Optional[str] = None) -> str:
    """The request's template, else the one the agent's adaptation names, else the first whose
    words the description uses, else tracker. A request and an adaptation naming two different
    templates is refused: the panel's choice is the owner's."""
    for value, where in ((requested, "the request"), (adapted, "the adaptation")):
        if value is not None and value not in TEMPLATES:
            raise ActionFailed(f"{where} names template {value!r}; it is one of " + ", ".join(TEMPLATES))
    if requested and adapted and requested != adapted:
        raise ActionFailed(f"the request asks for template {requested}, the adaptation for {adapted}")
    if requested or adapted:
        return requested or adapted
    lower = description.lower()
    for name, pattern in _PICK:
        if re.search(pattern, lower):
            return name
    return "tracker"


def load_template(name: str) -> Tuple[str, Dict[str, Any]]:
    folder = TEMPLATE_DIR / name
    page = (folder / "page.html").read_text(encoding="utf-8")
    schema = json.loads((folder / "schema.json").read_text(encoding="utf-8"))
    return page, schema


def _html_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def adapt(page: str, schema: Dict[str, Any], adaptation: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """The template renamed for this app: its title, its database's name, and option lists.

    adaptation: {"app_name", "database_name", "options": {"<prop>": ["<option>", ...]}}, each
    optional. Anything beyond this is the agent's own edit, passed whole as adaptation["html"].
    """
    schema = json.loads(json.dumps(schema))
    db = next(d for d in schema["databases"] if d.get("role") == "primary")
    old_db_name = db["name"]
    app_name = (adaptation.get("app_name") or schema.get("app") or "App").strip()
    new_db_name = (adaptation.get("database_name")
                   or f"{app_name} — {old_db_name.split(' — ', 1)[-1]}").strip()
    db["name"] = new_db_name
    for prop in db.get("props", []):
        renamed = (adaptation.get("options") or {}).get(prop.get("name"))
        if renamed and prop.get("options"):
            groups = [o.get("group") for o in prop["options"]]
            prop["options"] = [
                dict({"name": name}, **({"group": groups[min(i, len(groups) - 1)]}
                                        if groups[min(i, len(groups) - 1)] else {}))
                for i, name in enumerate(renamed)
            ]
    if adaptation.get("html"):
        return adaptation["html"], schema

    safe = _html_escape(app_name)
    page = re.sub(r"<title>[^<]*</title>", f"<title>{safe}</title>", page, count=1)
    page = re.sub(r"<h1>[^<]*</h1>", f"<h1>{safe}</h1>", page, count=1)
    # The page finds its database by this exact name in a single-quoted JS string (GUIDE step 3).
    js = new_db_name.replace("\\", "\\\\").replace("'", "\\'")
    page = page.replace(f"'{old_db_name}'", f"'{js}'")
    return page, schema


# The column types the backend's prop_add takes, less the title (a blank database mints it) and
# a relation (it needs a target database); and bounds well inside the backend's own.
SCHEMA_TYPES = ("text", "number", "checkbox", "url", "email", "select", "multi_select", "status",
                "date", "person", "files", "created_time", "created_by", "edited_time", "edited_by")
OPTION_TYPES = ("select", "multi_select", "status")
LAYOUTS = ("table", "board", "calendar", "list", "gallery")
MAX_COLUMNS, MAX_OPTIONS, MAX_VIEWS, MAX_NAME = 50, 100, 10, 100
TITLE_NAME = "Name"  # the title column a blank database is created with


def _name(value: Any, what: str) -> str:
    """A name the planner may send: a non-empty string of at most MAX_NAME characters that is not
    one of the plan's own placeholders ($db, $app)."""
    if not isinstance(value, str) or not value.strip():
        raise ActionFailed(f"{what} needs a name")
    if len(value) > MAX_NAME:
        raise ActionFailed(f"{what} is longer than {MAX_NAME} characters")
    if value.strip().startswith("$"):
        raise ActionFailed(f"{what} may not start with $ ({value[:20]!r})")
    return value


def _checked_schema(schema: Any) -> Dict[str, Any]:
    """An adaptation's own schema, checked in full before anything is claimed or created: the
    templates' schema.json shape, one primary database, at most MAX_COLUMNS columns of a known type
    with unique names, none of them the title, options only where a type has them, and at most
    MAX_VIEWS views over declared columns."""
    dbs = schema.get("databases") if isinstance(schema, dict) else None
    if not isinstance(dbs, list):
        raise ActionFailed("the adaptation's schema needs a databases list")
    primary = [d for d in dbs if isinstance(d, dict) and d.get("role") == "primary"]
    if len(primary) != 1:
        raise ActionFailed("the adaptation's schema needs exactly one primary database with a name")
    db = primary[0]
    _name(db.get("name"), "the adaptation's database")
    props = db.get("props") if db.get("props") is not None else []
    if not isinstance(props, list) or len(props) > MAX_COLUMNS:
        raise ActionFailed(f"the adaptation's schema has more than {MAX_COLUMNS} columns, or no list of them")
    seen: set = set()
    for prop in props:
        if not isinstance(prop, dict):
            raise ActionFailed("every column in the adaptation's schema needs a name and a type")
        name = _name(prop.get("name"), "a column in the adaptation's schema")
        if not isinstance(prop.get("type"), str) or not prop["type"]:
            raise ActionFailed("every column in the adaptation's schema needs a name and a type")
        if prop["type"] not in SCHEMA_TYPES:
            raise ActionFailed(f"column {name} has type {prop['type']!r}; a column is one of " + ", ".join(SCHEMA_TYPES))
        folded = name.strip().casefold()
        if folded == TITLE_NAME.casefold():
            raise ActionFailed(f"column {name} is the title column every database already has; leave it out")
        if folded in seen:
            raise ActionFailed(f"the adaptation's schema names column {name} twice")
        seen.add(folded)
        options = prop.get("options")
        if options is None:
            continue
        if prop["type"] not in OPTION_TYPES:
            raise ActionFailed(f"column {name} is {prop['type']}; only " + ", ".join(OPTION_TYPES) + " have options")
        if not isinstance(options, list) or len(options) > MAX_OPTIONS:
            raise ActionFailed(f"column {name} needs a list of at most {MAX_OPTIONS} options")
        names: set = set()
        for option in options:
            if not isinstance(option, dict):
                raise ActionFailed(f"each option of {name} needs a name")
            oname = _name(option.get("name"), f"an option of {name}").strip().casefold()
            if oname in names:
                raise ActionFailed(f"column {name} lists the option {option['name']} twice")
            names.add(oname)
            if option.get("group") is not None and not isinstance(option["group"], str):
                raise ActionFailed(f"the group of option {option['name']} of {name} must be text")
    views = db.get("views") if db.get("views") is not None else []
    if not isinstance(views, list) or len(views) > MAX_VIEWS:
        raise ActionFailed(f"the adaptation's schema needs a list of at most {MAX_VIEWS} views")
    for view in views:
        if not isinstance(view, dict) or view.get("layout") not in LAYOUTS:
            raise ActionFailed("each view needs a layout: one of " + ", ".join(LAYOUTS))
        if view.get("name") is not None:
            _name(view["name"], "a view")
        for key in ("groupBy", "dateBy"):
            if view.get(key) is not None and (not isinstance(view[key], str)
                                              or view[key].strip().casefold() not in seen):
                raise ActionFailed(f"a view's {key} names {view[key]!r}, which is not one of its columns")
    return json.loads(json.dumps(schema))


def build_inputs(request: Dict[str, Any], adaptation: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """(page html, schema, app name) for this request. A refine edits the page and no database.
    A build takes the template the request or the adaptation names (else one matching the
    description); a page the agent wrote itself (`html`) is laid over the schema the adaptation
    gives (`schema`), else the named template's, renamed and with its options as adapted."""
    if request["mode"] == "refine":
        if not adaptation.get("html"):
            raise ActionFailed("refine needs the edited page")
        return adaptation["html"], None, adaptation.get("app_name", "")
    template = pick_template(request["description"], request.get("template"), adaptation.get("template"))
    raw_page, raw_schema = load_template(template)
    if adaptation.get("schema") is not None:
        if not adaptation.get("html"):
            raise ActionFailed("an adaptation's own schema needs its own page (html) written against it")
        raw_schema = _checked_schema(adaptation["schema"])
    page_html, schema = adapt(raw_page, raw_schema, adaptation)
    name = (adaptation.get("app_name") or raw_schema.get("app") or "App").strip()
    return page_html, schema, name


# --------------------------------------------------------------------------- the writes

def chunks(html: str, limit: int = WRITE_LIMIT) -> List[str]:
    """The page cut into pieces of at most `limit` UTF-8 bytes, joined back exactly.

    Cuts fall on a line break where one is near, so each step of the live preview ends on a whole
    line; never inside a character.
    """
    if limit < 4:
        raise ValueError("limit is too small to hold a character")
    data = html.encode("utf-8")
    out: List[str] = []
    start = 0
    while start < len(data):
        end = min(start + limit, len(data))
        if end < len(data):
            newline = data.rfind(b"\n", start, end)
            if newline > start + limit // 2:
                end = newline + 1
            while end > start and (data[end] & 0xC0) == 0x80:   # step back off a continuation byte
                end -= 1
        out.append(data[start:end].decode("utf-8"))
        start = end
    return out


def plan(request: Dict[str, Any], page_html: str, schema: Optional[Dict[str, Any]],
         app_name: str) -> List[Dict[str, Any]]:
    """The Room Commons Actions, in order. `$db` is the bare id a database step answers; `$app`
    is the app an apps step answers. Every page write is at most WRITE_LIMIT bytes."""
    room = request["room_id"]
    steps: List[Dict[str, Any]] = [
        {"action": "commons.app_guide", "read": True, "target_kind": "account", "arguments": {}},
        # The owner can aim the agent only at a room the owner is in (the agent's own calls
        # fail in a room it is not in).
        {"action": "room.members.list", "read": True, "room_id": room, "arguments": {},
         "expect_member": request.get("owner")},
        {"action": "room.commons.pages.list", "read": True, "room_id": room, "arguments": {},
         "expect_page": request["page"]},
    ]
    # Every check comes before the first write. Nothing is overwritten unless it is ours: a build writes only into the panel's empty page,
    # and a refine only into the page of the app it names.
    if request["mode"] == "build":
        steps.append({"action": "room.commons.pages.read", "read": True, "room_id": room,
                      "arguments": {"kind": request["page"]}, "expect_empty": True})
    else:
        steps.append({"action": "room.commons.apps.list", "read": True, "room_id": room,
                      "arguments": {}, "expect_app": [request["app_id"], request["page"]]})
    database = request.get("database")
    if request["mode"] == "build" and schema is not None:
        primary = next(d for d in schema["databases"] if d.get("role") == "primary")
        if database is None:
            steps.append({"action": "room.commons.pages.create", "room_id": room, "answers": "$db",
                          "arguments": {"kind": "db", "title": primary["name"],
                                        "template": "blank"}})
            database = "$db"
        else:
            steps.append({"action": "room.commons.databases.list", "read": True, "room_id": room,
                          "arguments": {}, "expect_database": database})
        for prop in primary.get("props", []):
            definition = {"name": prop["name"], "type": prop["type"]}
            if prop.get("options"):
                definition["options"] = [
                    {k: v for k, v in option.items() if k in ("name", "group")}
                    for option in prop["options"]]
            steps.append({"action": "room.commons.databases.mutate", "room_id": room,
                          "skip_if_present": prop["name"],
                          "arguments": {"database": database, "op": "prop_add",
                                        "property": definition}})
        # A database the panel chose keeps its own views: they cannot be listed to check first.
        for view in primary.get("views", []) if request.get("database") is None else []:
            settings = {k: v for k, v in view.items() if k in ("layout", "name", "groupBy", "dateBy")}
            steps.append({"action": "room.commons.databases.mutate", "room_id": room,
                          "arguments": {"database": database, "op": "view_add",
                                        "view_settings": settings}})
    pieces = chunks(page_html)
    if request["mode"] == "build":
        # Again, right before the first page write: a write in the room since the first read
        # would otherwise be replaced.
        steps.append({"action": "room.commons.pages.read", "read": True, "room_id": room,
                      "arguments": {"kind": request["page"]}, "expect_empty": True})
    # A whole replace answers the version it displaced: a build must have displaced none.
    steps.append({"action": "room.commons.pages.replace", "room_id": room,
                  "arguments": {"kind": request["page"], "mode": "whole", "new_text": pieces[0]},
                  "expect_replaced_empty": request["mode"] == "build"})
    for piece in pieces[1:]:
        steps.append({"action": "room.commons.pages.append", "room_id": room,
                      "arguments": {"kind": request["page"], "text": piece}})
    steps.append({"action": "room.commons.pages.read", "read": True, "room_id": room,
                  "arguments": {"kind": request["page"]}, "expect_bytes": len(page_html.encode())})
    if request["mode"] == "refine":
        steps.append({"action": "room.commons.apps.update", "room_id": room, "answers": "$app",
                      "arguments": {"app": request["app_id"], "page": request["page"]}})
    else:
        steps.append({"action": "room.commons.apps.create", "room_id": room, "answers": "$app",
                      "arguments": {"name": app_name, "database": database,
                                    "page": request["page"]}})
    return steps


# --------------------------------------------------------------------------- run and reply

def execute(steps: List[Dict[str, Any]], call: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """Runs the plan through `call(step) -> result`, substituting $db and $app as they are answered.

    The agent drives the same steps through the s.space MCP; tests drive them through a fake room.
    """
    answers: Dict[str, Any] = {}

    def fill(value: Any) -> Any:
        if isinstance(value, str) and value in answers:
            return answers[value]
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fill(v) for v in value]
        return value

    present: set = set()
    written = False
    for step in steps:
        if step.get("skip_if_present") and step["skip_if_present"] in present:
            step = existing_column(step, answers)
            if step is None:
                continue
        concrete = dict(step, arguments=fill(step["arguments"]))
        try:
            result = call(concrete) or {}
        except ActionFailed as error:
            raise ActionFailed(_partial(str(error), written)) from None
        if step["action"] in ("room.commons.pages.replace", "room.commons.pages.append"):
            written = True
        verify(step, result, answers, present)
    return answers


def verify(step: Dict[str, Any], result: Dict[str, Any], answers: Optional[Dict[str, Any]] = None,
           present: Optional[set] = None) -> None:
    """Raises ActionFailed when a step's result fails the step's check; records what it answers.

    Every check needs positive evidence: a result that lacks what the check reads fails it."""
    answers = {} if answers is None else answers
    present = set() if present is None else present
    result = unwrap(result)
    kind = step["arguments"].get("kind")
    if step.get("expect_empty"):
        size = result.get("total_bytes")
        if type(size) is not int:
            raise ActionFailed(f"could not tell whether page {kind} is empty")
        if size:
            raise ActionFailed(f"page {kind} is not empty; a build only writes into a new page")
    if step.get("expect_replaced_empty") and result.get("version_id"):
        raise ActionFailed(_partial(f"page {kind} had content when it was replaced; its previous "
                                    f"version is {result['version_id']}", True))
    if step.get("expect_app"):
        app_id, page = step["expect_app"]
        apps = {a.get("id"): a for a in _listed(result, "apps")}
        if app_id not in apps:
            raise ActionFailed(f"app {app_id} is not in this room")
        if apps[app_id].get("page") != page:
            raise ActionFailed(f"app {app_id} is not on page {page}")
        answers["$app_db"] = bare(apps[app_id].get("database"))
    if "expect_member" in step:
        joined = {m.get("user_id") for m in _listed(result, "members")}
        if not step["expect_member"] or step["expect_member"] not in joined:
            raise ActionFailed(f"the owner is not in room {step.get('room_id')}")
    if step.get("expect_page"):
        kinds = {p.get("kind") for p in _listed(result, "pages")}
        if step["expect_page"] not in kinds:
            raise ActionFailed(f"page {step['expect_page']} is not in this room")
    if step.get("expect_database"):
        # databases.list: databases[].database (maybe "db:"-prefixed), .properties[].name
        dbs = {bare(d.get("database")): d for d in _listed(result, "databases")}
        found = dbs.get(bare(step["expect_database"]))
        if not found:
            raise ActionFailed(f"database {step['expect_database']} is not in this room")
        present.update(p.get("name") for p in found.get("properties", []) if p.get("name"))
        if answers is not None:
            answers["$columns"] = {p["name"].casefold(): {"name": p["name"], "type": p.get("type"),
                                                           "options": list(p.get("options") or [])}
                                   for p in found.get("properties", []) if isinstance(p.get("name"), str)}
            answers["$rows"] = found.get("rows") if type(found.get("rows")) is int else None
    if (step.get("expect_bytes") is not None
            and (type(result.get("total_bytes")) is not int
                 or result["total_bytes"] != step["expect_bytes"])):
        raise ActionFailed(_partial("the page read back is not the page written", True))
    if step.get("answers") == "$db":
        answers["$db"] = bare(result.get("kind", ""))
    if step.get("answers") == "$app":
        app = result.get("app") or {}
        answers["$app"] = result.get("id") or app.get("id")
        if app.get("database"):
            answers["$app_db"] = bare(app["database"])


def existing_column(step: Dict[str, Any], answers: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What a prop_add becomes when its column is already in the database the panel sent: None
    (nothing to do: the same type, every option there), a prop_update adding the options it lacks
    (only on a database with no rows, so no value can be cleared), or ActionFailed: another type,
    or options missing from a database that already has rows."""
    want = step["arguments"]["property"]
    have = (answers.get("$columns") or {}).get(want["name"].casefold())
    if have is None:
        return None
    if have.get("type") != want["type"]:
        raise ActionFailed(f"the database already has a {have.get('type')} column {have['name']}; "
                           f"this app needs it to be {want['type']}")
    missing = [o for o in want.get("options") or []
               if o["name"].casefold() not in {n.casefold() for n in have.get("options") or []}]
    if not missing:
        return None
    if answers.get("$rows") != 0:
        raise ActionFailed(f"column {have['name']} lacks the options "
                           + ", ".join(o["name"] for o in missing)
                           + "; the database already has rows, so they were not changed: add them in its table")
    options = [{"name": n} for n in have.get("options") or []] + missing
    return dict(step, skip_if_present=None, arguments={
        "database": step["arguments"]["database"], "op": "prop_update", "prop": have["name"],
        "property": {"options": options}})


def unwrap(result: Any) -> Dict[str, Any]:
    """The Action's own result, also when given as the MCP tool's {"status", "result"} reply."""
    if isinstance(result, dict) and isinstance(result.get("result"), dict) and "status" in result:
        result = result["result"]
    if not isinstance(result, dict):
        raise ActionFailed("the Action returned no result")
    return result


def _listed(result: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    items = result.get(key)
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise ActionFailed(f"the Action's result has no {key} list")
    return items


def _partial(reason: str, written: bool) -> str:
    """A failure after the page was written says so: it may now be half a page."""
    return f"{reason} (the page may be partly written)" if written else reason


def bare(database: Optional[str]) -> Optional[str]:
    """A database id without the "db:" the Actions sometimes add; the envelope's form."""
    if isinstance(database, str) and database.startswith("db:"):
        return database[3:]
    return database


def completion(request_id: Optional[str], status: str, reason: str = "",
               line: str = "", **found: Optional[str]) -> str:
    """The panel's reply: one human line, a blank line, then the same fence with a status."""
    body: Dict[str, Any] = {"v": VERSION, "request_id": request_id, "status": status,
                            "reason": reason}
    body.update({k: v for k, v in found.items() if v})
    human = line or (f"Could not build the app: {reason}" if status == "failed" else "Done.")
    # Compact, as BUILD_REQUEST.md shows it, so the reply matches the spec byte for byte.
    return f"{human}\n\n```{FENCE}\n{json.dumps(body, ensure_ascii=False, separators=(',', ':'))}\n```"


def build(task_text: str, call: Callable[[Dict[str, Any]], Dict[str, Any]],
          adaptation: Optional[Dict[str, Any]] = None,
          ledger: Optional[Ledger] = None) -> str:
    """The whole answer to one task: parse, authorize, dedupe, plan, execute, reply. Never raises.

    The ledger makes a resend of a request_id answer with the recorded reply and build nothing,
    so the panel's retry is safe; without one, nothing is remembered between calls.
    """
    adaptation = adaptation or {}
    request_id = request_id_of(task_text)
    try:
        request = parse(task_text)
    except RequestError as error:
        return completion(request_id, "failed", str(error))
    if ledger is not None:
        state, recorded = ledger.claim(request["room_id"], request["request_id"])
        if state == "done":
            return recorded
        if state == "busy":
            return completion(request["request_id"], "failed",
                              "this request is already being built")
    try:
        page_html, schema, name = build_inputs(request, adaptation)
        answers = execute(plan(request, page_html, schema, name), call)
        database = request.get("database") or answers.get("$db") or answers.get("$app_db")
        app_id = answers.get("$app") or request.get("app_id")
        verb = "Updated" if request["mode"] == "refine" else "Built"
        what = f'"{name}"' if name else "the app"
        line = f"{verb} {what} on page {request['page']}" + (f", database {database}." if database else ".")
        reply = completion(request["request_id"], "done", "", line,
                           page=request["page"], database=database, app_id=app_id)
    except (ActionFailed, OSError, KeyError, ValueError) as error:
        reply = completion(request["request_id"], "failed", str(error) or type(error).__name__)
    except Exception as error:  # noqa: BLE001 - after the claim every outcome is a recorded reply
        reply = completion(request["request_id"], "failed", f"the build failed unexpectedly ({type(error).__name__})")
    if ledger is not None:
        ledger.record(request["room_id"], request["request_id"], reply)
    return reply


# --------------------------------------------------------------------------- CLI

def _flag(argv: List[str], name: str) -> Optional[str]:
    """The value after --name, or None; a flag given last with no value is a usage error."""
    if name not in argv:
        return None
    i = argv.index(name)
    if i + 1 >= len(argv):
        raise SystemExit(f"build_request: {name} needs a value")
    return argv[i + 1]


def _check(args: List[str]) -> int:
    """`check <plan.json> <n> <result.json>`: step n's check, in code; the agent writes nothing
    until every read before the first write has passed it."""
    if len(args) != 3:
        print("usage: build_request.py check <plan.json> <n> <result.json>", file=sys.stderr)
        return 2
    try:
        steps = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        step = steps[int(args[1])]
        result = json.loads(Path(args[2]).read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError, TypeError) as error:
        print(f"build_request: cannot check: {error}", file=sys.stderr)
        return 2
    answers: Dict[str, Any] = {}
    present: set = set()
    try:
        verify(step, result, answers, present)
    except ActionFailed as error:
        print(str(error))
        return 1
    # What later steps use: a prop_add whose column is listed here is skipped, or replaced by the
    # prop_update in `columns`; a column that cannot be used fails here, before anything is written.
    columns: Dict[str, Any] = {}
    if step.get("expect_database"):
        for i, later in enumerate(steps):
            if i > int(args[1]) and later.get("skip_if_present") in present:
                try:
                    columns[str(i)] = existing_column(later, answers) or "skip"
                except ActionFailed as error:
                    print(str(error))
                    return 1
    shown = {k: v for k, v in answers.items() if not k.startswith("$columns") and k != "$rows"}
    print(json.dumps({"present": sorted(present), "answers": shown, "columns": columns}, ensure_ascii=False))
    return 0


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("parse", "plan", "reply", "check", "chunks"):
        print("usage: build_request.py parse|plan|reply|check|chunks <file> [options]",
              file=sys.stderr)
        return 2
    command, path = argv[0], argv[1]
    if command == "check":
        return _check(argv[1:])
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        print(f"build_request: cannot read {path}: {error}", file=sys.stderr)
        return 2
    if command == "chunks":
        for piece in chunks(text):
            print(len(piece.encode("utf-8")))
        return 0
    try:
        folder = _flag(argv, "--ledger")
        ledger = (lambda: Ledger(Path(folder) if folder else default_ledger()))
        if command == "reply":
            try:
                answered = parse(text)
            except RequestError as error:
                print(f"build_request: cannot record a reply to this task: {error}", file=sys.stderr)
                return 2
            request_id = answered["request_id"]
            status = _flag(argv, "--status")
            if status not in ("done", "failed"):
                print("build_request: --status must be done or failed", file=sys.stderr)
                return 2
            reply = completion(request_id, status, _flag(argv, "--reason") or "",
                               _flag(argv, "--line") or "", page=_flag(argv, "--page"),
                               database=bare(_flag(argv, "--database")),
                               app_id=_flag(argv, "--app-id"))
            ledger().record(answered["room_id"], request_id, reply)
            print(reply)
            return 0
        adapt_path = _flag(argv, "--adapt")
        route = _flag(argv, "--route") or "mcp"
        if route not in ("mcp", "cli"):
            raise SystemExit("build_request: --route is mcp or cli")
    except SystemExit as error:
        print(str(error), file=sys.stderr)
        return 2
    try:
        request = parse(text)
    except RequestError as error:
        print(completion(request_id_of(text), "failed", str(error)))
        return 1
    if command == "parse":
        print(json.dumps(request, indent=2, ensure_ascii=False))
        return 0
    adaptation: Dict[str, Any] = {}
    if adapt_path:
        adaptation = json.loads(Path(adapt_path).read_text(encoding="utf-8"))
    try:
        page_html, schema, name = build_inputs(request, adaptation)
        steps = plan(request, page_html, schema, name)  # before the claim: a plan that fails holds nothing
    except (ActionFailed, KeyError, ValueError, TypeError, AttributeError) as error:
        print(completion(request["request_id"], "failed", str(error) or type(error).__name__))
        return 1
    state, recorded = ledger().claim(request["room_id"], request["request_id"])
    if state == "done":
        print(recorded)
        return 3
    if state == "busy":
        print("build_request: another run is building this request", file=sys.stderr)
        return 4
    if route == "cli":
        steps = [dict(step, cli=cli_argv(step)) for step in steps]
    print(json.dumps(steps, indent=2, ensure_ascii=False))
    return 0


def cli_argv(step: Dict[str, Any]) -> List[str]:
    """The step as room-commons' `action` command (room_commons.py action …): the same Action on
    the deployment the CLI's lane reaches, its result printed in the shape `check` reads. `$db`
    and `$app` stay placeholders, filled from the answers `check` prints, as for an MCP host."""
    target = "account" if step.get("target_kind") == "account" else step["room_id"]
    argv = ["action", target, step["action"], "--args",
            json.dumps(step["arguments"], ensure_ascii=False, separators=(",", ":"))]
    return argv + (["--read"] if step.get("read") else [])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
