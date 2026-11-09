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

_PICK = (
    ("turn-game", r"\b(game|tic[- ]?tac|turn|board game|gomoku|checkers|chess|connect four)\b"),
    ("poll", r"\b(poll|vote|voting|survey|choose|pick (one|a)|preference|which (day|option))\b"),
    ("planner", r"\b(plan|planner|week|weekly|schedule|rota|calendar|itinerary|meal)\b"),
    ("tracker", r"\b(track|tracker|task|todo|to-do|issue|bug|request|queue|chore|status)\b"),
)


def pick_template(description: str, requested: Optional[str] = None) -> str:
    """The requested template, else the first whose words the description uses, else tracker."""
    if requested:
        return requested
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

class ActionFailed(RuntimeError):
    """An Action refused; its message becomes the completion's reason."""


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
        if request["mode"] == "refine":
            if not adaptation.get("html"):
                raise ActionFailed("refine needs the edited page")
            page_html, schema, name = adaptation["html"], None, adaptation.get("app_name", "")
        else:
            template = pick_template(request["description"], request.get("template"))
            raw_page, raw_schema = load_template(template)
            page_html, schema = adapt(raw_page, raw_schema, adaptation)
            name = (adaptation.get("app_name") or raw_schema.get("app") or "App").strip()
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
    # What later steps use: skip a prop_add whose skip_if_present name is listed here.
    print(json.dumps({"present": sorted(present), "answers": answers}, ensure_ascii=False))
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
    if request["mode"] == "refine":
        page_html, schema, name = adaptation.get("html", ""), None, adaptation.get("app_name", "")
        if not page_html:
            print(completion(request["request_id"], "failed", "refine needs the edited page"))
            return 1
    else:
        template = pick_template(request["description"], request.get("template"))
        raw_page, raw_schema = load_template(template)
        page_html, schema = adapt(raw_page, raw_schema, adaptation)
        name = (adaptation.get("app_name") or raw_schema.get("app") or "App").strip()
    state, recorded = ledger().claim(request["room_id"], request["request_id"])
    if state == "done":
        print(recorded)
        return 3
    if state == "busy":
        print("build_request: another run is building this request", file=sys.stderr)
        return 4
    print(json.dumps(plan(request, page_html, schema, name), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
