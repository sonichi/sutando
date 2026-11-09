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
  build_request.py chunks <page.html>                      -> chunk byte sizes, one per line
plan exits 3 printing the recorded reply when the request_id was already answered, and 4 when
another run holds it. Add --ledger DIR to keep the record somewhere other than the workspace.
"""
from __future__ import annotations

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

_FENCE_RE = re.compile(r"```" + re.escape(FENCE) + r"[ \t]*\n(.*?)\n```", re.S)
_PAGE_RE = re.compile(r"^html(-[a-z0-9]{8})?$")          # the client's own PAGE_RE (appsModel.ts)
_ROOM_RE = re.compile(r"^![^:\s]+:\S+$")
# A bare database id: the backend's DATABASE pattern (actions/commons/reads.py) without "db:".
_DB_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,119}$")
_APP_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,80}$")

# The skill's own folder, for its templates/; not the workspace.
SKILL_DIR = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
TEMPLATE_DIR = SKILL_DIR / "templates"


class RequestError(ValueError):
    """A malformed request. The message is the completion's reason, so keep it short and plain."""


# --------------------------------------------------------------------------- who sent it

_HEADER_RE = re.compile(r"^([a-z_]+): ?(.*)$")


def headers(text: str) -> Dict[str, str]:
    """The task file's own header lines, up to `task:`. They come from the bridge, not the sender."""
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        m = _HEADER_RE.match(line)
        if not m:
            break
        if m.group(1) == "task":
            break
        out[m.group(1)] = m.group(2).strip()
    return out


def authorize(meta: Dict[str, str], request: Dict[str, Any]) -> None:
    """Only the owner asks, and only for the room the task came from.

    The fence is message text anyone could paste; the headers are the bridge's. A request whose
    room is not the task's own would let a pasted fence aim the writes at another room.
    """
    if meta.get("access_tier") != "owner":
        raise RequestError("only the owner can ask for a build")
    trusted = meta.get("source_room_id") or meta.get("channel_id")
    if not trusted:
        raise RequestError("the task does not say which room it came from")
    if trusted != request["room_id"]:
        raise RequestError("`room_id` is not the room this request came from")


# --------------------------------------------------------------------------- the request

def extract(text: str) -> Dict[str, Any]:
    """The fenced JSON object in a task's text. A completion (it has "status") is not a request."""
    blocks = _FENCE_RE.findall(text or "")
    if not blocks:
        raise RequestError(f"no ```{FENCE} block in the task")
    if len(blocks) > 1:
        raise RequestError(f"more than one ```{FENCE} block in the task")
    try:
        data = json.loads(blocks[0])
    except json.JSONDecodeError as error:
        raise RequestError(f"the {FENCE} block is not JSON ({error.msg})") from None
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
    authorize(headers(text), request)
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
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                entry = self.recorded(room_id, request_id) or {}
                if entry.get("reply"):
                    return "done", entry["reply"]
                if now - float(entry.get("claimed_at", 0)) < CLAIM_TTL_S:
                    return "busy", None
                path.unlink(missing_ok=True)       # an abandoned claim: take it over
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"room_id": room_id, "request_id": request_id, "claimed_at": now}, fh)
            return "claimed", None
        return "busy", None

    def record(self, room_id: str, request_id: str, reply: str) -> None:
        path = self._path(room_id, request_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"room_id": room_id, "request_id": request_id, "reply": reply,
                                   "answered_at": time.time()}), encoding="utf-8")
        os.replace(tmp, path)


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
        for view in primary.get("views", []):
            settings = {k: v for k, v in view.items() if k in ("layout", "name", "groupBy", "dateBy")}
            steps.append({"action": "room.commons.databases.mutate", "room_id": room,
                          "arguments": {"database": database, "op": "view_add",
                                        "view_settings": settings}})
    pieces = chunks(page_html)
    steps.append({"action": "room.commons.pages.replace", "room_id": room,
                  "arguments": {"kind": request["page"], "mode": "whole", "new_text": pieces[0]}})
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
        if step.get("expect_empty") and result.get("total_bytes"):
            raise ActionFailed(f"page {step['arguments']['kind']} is not empty; "
                               "a build only writes into a new page")
        if step.get("expect_app"):
            app_id, page = step["expect_app"]
            apps = {a.get("id"): a for a in result.get("apps", [])}
            if app_id not in apps:
                raise ActionFailed(f"app {app_id} is not in this room")
            if apps[app_id].get("page") != page:
                raise ActionFailed(f"app {app_id} is not on page {page}")
            answers["$app_db"] = bare(apps[app_id].get("database"))
        if step.get("expect_page"):
            kinds = {p.get("kind") for p in result.get("pages", [])}
            if step["expect_page"] not in kinds:
                raise ActionFailed(f"page {step['expect_page']} is not in this room")
        if step.get("expect_database"):
            # databases.list: databases[].database (maybe "db:"-prefixed), .properties[].name
            dbs = {bare(d.get("database")): d for d in result.get("databases", [])}
            found = dbs.get(bare(step["expect_database"]))
            if not found:
                raise ActionFailed(f"database {step['expect_database']} is not in this room")
            present.update(p.get("name") for p in found.get("properties", []) if p.get("name"))
        if (step.get("expect_bytes") is not None
                and result.get("total_bytes") != step["expect_bytes"]):
            raise ActionFailed(_partial("the page read back is not the page written", True))
        if step.get("answers") == "$db":
            answers["$db"] = bare(result.get("kind", ""))
        if step.get("answers") == "$app":
            app = result.get("app") or {}
            answers["$app"] = result.get("id") or app.get("id")
            if app.get("database"):
                answers["$app_db"] = bare(app["database"])
    return answers


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
    return f"{human}\n\n```{FENCE}\n{json.dumps(body, ensure_ascii=False)}\n```"


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


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("parse", "plan", "reply", "chunks"):
        print("usage: build_request.py parse|plan|reply|chunks <file> [options]", file=sys.stderr)
        return 2
    command, path = argv[0], argv[1]
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
        ledger = Ledger(Path(folder) if folder else default_ledger())
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
            ledger.record(answered["room_id"], request_id, reply)
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
    state, recorded = ledger.claim(request["room_id"], request["request_id"])
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
