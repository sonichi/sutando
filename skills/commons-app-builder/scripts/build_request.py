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
plan exits 5 printing the recorded reply when the request_id was already answered (never 3:
room-commons' `action` exits 3 for an outcome it cannot know), and 4 when another run holds it. Add --ledger DIR to keep the record somewhere other than the workspace.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
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
# plan's exit for a request already answered: not 3, room-commons' `action` code for "pending".
ANSWERED = 5
MODES = ("build", "refine")
CLAIM_TTL_S = 30 * 60            # a claim nobody finished stops blocking a retry after this

# The gateway flattens a task body onto one line, so the fence may be on one line too.
_OPENER_RE = re.compile(r"```" + re.escape(FENCE) + r"\s")
_PAGE_RE = re.compile(r"^html(-[a-z0-9]{8})?$")          # the client's own PAGE_RE (appsModel.ts)
_ROOM_RE = re.compile(r"^![^:\s]+:\S+$")
# A bare database id: the backend's DATABASE pattern (actions/commons/reads.py) without "db:".
_DB_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,119}$")
_APP_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,80}$")
_EVENT_RE = re.compile(r"^\$[A-Za-z0-9_\-+/=.:]{1,254}$")  # a Matrix event id

ASK_TTL_S = 24 * 3600  # an unanswered question fails its request after this
WAITING = 6  # plan's exit while the owner's answer is awaited
_MANIFEST = Path(__file__).resolve().parents[1] / "manifest.json"

# A room's ledger lock is held for one read-modify-write; past this another process gets LedgerBusy.
LOCK_TIMEOUT_S = 10.0


def thread_conversation() -> bool:
    """The skill's switch BUILD_THREAD_CONVERSATION, read env > the manifest's config (default
    off), as every skill's config is (skills/MANIFEST.md). Off, nothing reads the thread."""
    try:
        from skill_config import enabled  # noqa: PLC0415  (src/ is on the path)
    except ImportError:
        return False
    return enabled("BUILD_THREAD_CONVERSATION", _MANIFEST)

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
    # Off, the field is ignored as an unknown key is: never validated, never in the request.
    thread_root = text("thread_root", required=False) if thread_conversation() else None
    if thread_root is not None and not _EVENT_RE.match(thread_root):
        raise RequestError("`thread_root` is not an event id")
    thread_message = text("thread_message", required=False) if thread_conversation() else None
    if thread_message is not None and not _EVENT_RE.match(thread_message):
        raise RequestError("`thread_message` is not an event id")
    app_id = text("app_id", required=False)
    if mode == "refine" and app_id is None:
        raise RequestError("`app_id` is required to refine")
    if app_id is not None and not _APP_RE.match(app_id):
        raise RequestError("`app_id` is not an app id")
    return {"v": VERSION, "request_id": request_id, "mode": mode, "description": description,
            "room_id": room_id, "page": page, "database": database, "template": template,
            "app_id": app_id, **({"thread_root": thread_root} if thread_root else {}),
            **({"thread_message": thread_message} if thread_message else {})}


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

    def __init__(self, folder: Path, lock_timeout: float = LOCK_TIMEOUT_S):
        self.folder, self.lock_timeout = Path(folder), lock_timeout

    def _path(self, room_id: str, request_id: str) -> Path:
        room = hashlib.sha256(room_id.encode("utf-8")).hexdigest()[:16]
        return self.folder / room / f"{uuid.UUID(request_id)}.json"

    def recorded(self, room_id: str, request_id: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self._path(room_id, request_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def claim(self, room_id: str, request_id: str,
              now: Optional[float] = None, fence: Optional[Dict[str, Any]] = None) -> Tuple[str, Optional[str]]:
        """("claimed", None), ("done", reply), ("busy", None), ("asked", None) while the owner's
        answer to its question is awaited, or ("expired", None) once that wait is over."""
        now = time.time() if now is None else now
        path = self._path(room_id, request_id)
        with self._locked(path.parent):
            entry = None
            if path.exists():
                entry = self.recorded(room_id, request_id)
                if entry and entry.get("reply"):
                    return "done", entry["reply"]
                if entry and entry.get("asked") and not entry.get("answer"):
                    return ("asked" if now - entry["asked"]["at"] < ASK_TTL_S else "expired"), None
                if entry and entry.get("answer") and not entry.get("resumed"):
                    # The owner answered: this run takes the request up again, once.
                    self._write(path, dict(entry, claimed_at=now, resumed=True))
                    return "claimed", None
                # Unreadable counts as busy until it ages out: only a dead claim is taken over.
                # An entry holding only a fence's facts (noted when a plan failed early) is no claim.
                started = entry.get("claimed_at") if entry else None
                noted_only = bool(entry) and set(entry) <= {"room_id", "request_id", "fence", "slot_used_by"}
                if not isinstance(started, (int, float)) and not noted_only:
                    started = path.stat().st_mtime
                if not noted_only and now - started < CLAIM_TTL_S:
                    return "busy", None
            # A dead claim taken over keeps what it held: an asked question stays asked for good.
            kept = {k: v for k, v in (entry or {}).items()
                    if k in ("asked", "answer", "answered_at", "resumed", "fence", "slot_used_by")}
            if fence and "fence" not in kept and fence.get("request_id") == request_id:
                kept["fence"] = fence  # its thread, owner and time, for thread-task; never in any output
            self._write(path, dict(kept, room_id=room_id, request_id=request_id, claimed_at=now))
        if kept.get("fence"):
            self.index_thread(kept["fence"])
        return "claimed", None

    def ask(self, room_id: str, request_id: str, *, thread_root: str, owner: str, question: str,
            task_text: str, now: Optional[float] = None) -> bool:
        """Turn this run's claim into one question awaiting the owner in the thread; False when the
        request already asked one (a restart never asks twice) or is not this run's to ask."""
        now = time.time() if now is None else now
        path = self._path(room_id, request_id)
        with self._locked(path.parent):
            entry = self.recorded(room_id, request_id) or {}
            if entry.get("reply") or entry.get("asked") or "claimed_at" not in entry:
                return False
            self._write(path, dict(entry, asked={"thread_root": thread_root, "owner": owner,
                                                 "question": question, "at": now}))
            path.with_suffix(".task.txt").write_text(task_text, encoding="utf-8")
            return True

    def answer(self, room_id: str, thread_root: str, owner: str, text: str,
               now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """The open question this room's thread holds, answered by its owner: the request it
        belongs to, now ready to be taken up again; None when there is none to answer."""
        now = time.time() if now is None else now
        room = self.folder / hashlib.sha256(room_id.encode("utf-8")).hexdigest()[:16]
        if not room.is_dir():
            return None
        with self._locked(room):
            for path in sorted(room.glob("*.json")):
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                asked = entry.get("asked") or {}
                if (asked.get("thread_root") != thread_root or asked.get("owner") != owner
                        or entry.get("answer") or entry.get("reply")):
                    continue
                if now - asked.get("at", 0) >= ASK_TTL_S:
                    # Too late: the request lapsed. Recorded once, so the thread hears it once.
                    reply = completion(entry["request_id"], "failed", LAPSED)
                    self._write(path, dict(entry, reply=reply, answered_at=now))
                    return {"request_id": entry["request_id"], "lapsed": True,
                            "task_file": str(path.with_suffix(".task.txt"))}
                self._write(path, dict(entry, answer=text, answered_at=now))
                return {"request_id": entry["request_id"], "answer": text,
                        "question": asked.get("question"), "task_file": str(path.with_suffix(".task.txt"))}
        return None

    def record(self, room_id: str, request_id: str, reply: str) -> None:
        path = self._path(room_id, request_id)
        with self._locked(path.parent):
            old = self.recorded(room_id, request_id) or {}
            kept = {k: old[k] for k in ("fence", "slot_used_by", "asked") if k in old}
            self._write(path, {"room_id": room_id, "request_id": request_id, "reply": reply,
                               "answered_at": time.time(), **kept})

    def note_fence(self, fence: Dict[str, Any]) -> None:
        """Keep a fence's thread facts even when its plan failed before any claim, so the thread
        message that goes with it is still known for what it is."""
        try:
            path = self._path(fence["room_id"], fence["request_id"])
        except (KeyError, TypeError, ValueError):
            return
        with self._locked(path.parent):
            entry = self.recorded(fence["room_id"], fence["request_id"]) or {}
            if "fence" not in entry:
                self._write(path, dict(entry, room_id=fence["room_id"], request_id=fence["request_id"], fence=fence))
        self.index_thread(fence)

    THREADS = "app-threads.json"

    def index_thread(self, fence: Dict[str, Any]) -> None:
        """Record a fence's thread in THREADS, {room_id: {thread_root: until}}, the one file the
        gateway bridge reads in its own process to tell an app's thread from any other: a thread
        stays an app's until ASK_TTL_S after the newest fence that named it. Best effort."""
        room, root, at = fence.get("room_id"), fence.get("thread_root"), fence.get("at")
        if not (isinstance(room, str) and isinstance(root, str) and isinstance(at, (int, float))):
            return
        path = self.folder / self.THREADS
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with _FileLock(self.folder / ".threads.lock", self.lock_timeout):
                try:
                    known = json.loads(path.read_text(encoding="utf-8"))
                    known = known if isinstance(known, dict) else {}
                except (OSError, ValueError):
                    known = {}
                now = time.time()
                known = {r: {t: u for t, u in (ts or {}).items()
                             if isinstance(u, (int, float)) and math.isfinite(u) and u > now}
                         for r, ts in known.items() if isinstance(ts, dict)}
                threads = known.setdefault(room, {})
                threads[root] = max(threads.get(root, 0), at + ASK_TTL_S)
                self._write(path, {r: ts for r, ts in known.items() if ts})
        except LedgerBusy as error:
            print(f"build_request: {error}; the thread index was not updated", file=sys.stderr)
        except OSError:
            return

    def use_slot(self, room_id: str, request_id: str, message_id: str) -> bool:
        """Give a build fence's one slot to this message; True when it is (or already was) this
        message's, False when another message holds it."""
        path = self._path(room_id, request_id)
        with self._locked(path.parent):
            entry = self.recorded(room_id, request_id) or {}
            used = entry.get("slot_used_by")
            if used and used != message_id:
                return False
            if not used:
                self._write(path, dict(entry, slot_used_by=message_id))
            return True

    def first_seen(self, room_id: str, message_id: str, now: float) -> float:
        """When thread-task first saw this message (recorded the first time it is asked)."""
        room = self.folder / hashlib.sha256(room_id.encode("utf-8")).hexdigest()[:16]
        path = room / f"seen-{hashlib.sha256(message_id.encode('utf-8')).hexdigest()[:24]}.txt"
        with self._locked(room):
            try:
                return float(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                path.write_text(repr(now), encoding="utf-8")
                return now

    def entries(self, room_id: str) -> List[Dict[str, Any]]:
        """Every readable entry of one room."""
        room = self.folder / hashlib.sha256(room_id.encode("utf-8")).hexdigest()[:16]
        out = []
        for path in sorted(room.glob("*.json")) if room.is_dir() else []:
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def _locked(self, folder: Path):
        """One room's claims and records, serialized across processes (LedgerBusy past the timeout)."""
        folder.mkdir(parents=True, exist_ok=True)
        return _FileLock(folder / ".lock", self.lock_timeout)

    @staticmethod
    def _write(path: Path, entry: Dict[str, Any]) -> None:
        """Whole or not at all: a reader never sees a half-written entry."""
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, path)


class LedgerBusy(OSError):
    """Another process held a room's ledger lock past the timeout."""


class _FileLock:
    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_S):
        self.path, self.timeout = path, timeout

    def __enter__(self):
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise LedgerBusy(f"the build ledger {self.path.parent} is busy") from None
                time.sleep(0.02)

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
    if len(dbs) != 1:  # the plan builds the primary database only; a second one would be dropped
        raise ActionFailed("the adaptation's schema has more than one database; an app here has one")
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
    checked = json.loads(json.dumps(schema))
    for d in checked["databases"]:
        if d.get("role") == "primary":
            d["props"], d["views"] = d.get("props") or [], d.get("views") or []  # null is none
    return checked


def _checked_adaptation(adaptation: Dict[str, Any], template_schema: Dict[str, Any]) -> None:
    """The adaptation's own names, under the rules an own schema's are: app_name and
    database_name, and `options` only for the template's option columns, a list of names each."""
    for key in ("app_name", "database_name"):
        if adaptation.get(key) is not None:
            _name(adaptation[key], f"the adaptation's {key}")
    options = adaptation.get("options")
    if options is None:
        return
    if not isinstance(options, dict):
        raise ActionFailed("the adaptation's options must map a column to its list of options")
    primary = next(d for d in template_schema["databases"] if d.get("role") == "primary")
    with_options = {p["name"] for p in primary.get("props", []) if p.get("options")}
    for column, names in options.items():
        if column not in with_options:
            raise ActionFailed(f"the adaptation gives options for {column!r}, which has none in this template")
        if not isinstance(names, list) or not names or len(names) > MAX_OPTIONS:
            raise ActionFailed(f"the options for {column} must be a list of 1 to {MAX_OPTIONS} names")
        seen: set = set()
        for n in names:
            folded = _name(n, f"an option of {column}").strip().casefold()
            if folded in seen:
                raise ActionFailed(f"the options for {column} list {n} twice")
            seen.add(folded)


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
    _checked_adaptation(adaptation, raw_schema)  # against the columns this build actually uses
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
    decided: Dict[int, Any] = {}
    written = False
    for n, step in enumerate(steps):
        if step.get("skip_if_present") and step["skip_if_present"].strip().casefold() in present:
            step = decided.get(id(step), "skip")
            if step == "skip":
                continue
        concrete = dict(step, arguments=fill(step["arguments"]))
        try:
            result = call(concrete) or {}
        except Exception as error:  # noqa: BLE001 - a best-effort line never fails the build
            if step.get("best_effort"):
                continue  # a line in the thread; the DM answer stays the source of truth
            if not isinstance(error, ActionFailed):
                raise
            raise ActionFailed(_partial(str(error), written)) from None
        if step["action"] in ("room.commons.pages.replace", "room.commons.pages.append"):
            written = True
        verify(step, result, answers, present)
        if step.get("expect_database"):
            # Every column the database already has is decided now, before any column is written.
            decided = column_decisions(steps, n, answers, present)
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
        if found.get("properties_truncated") is not False:  # complete only when it says so; no read shows more
            raise ActionFailed(f"the room lists database {step['expect_database']} without all of its "
                               "columns or options, so they cannot be checked before anything is written")
        present.update(p["name"].strip().casefold() for p in found.get("properties", [])
                       if isinstance(p.get("name"), str))
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
    have = (answers.get("$columns") or {}).get(want["name"].strip().casefold())
    if have is None:
        return None
    if have["name"] != want["name"]:
        # The page reads and writes its columns by exact name.
        raise ActionFailed(f"the database has a column {have['name']}; this app reads it as {want['name']}: "
                           f"rename it to {want['name']} in its table")
    if have.get("type") != want["type"]:
        raise ActionFailed(f"the database already has a {have.get('type')} column {have['name']}; "
                           f"this app needs it to be {want['type']}")
    missing = [o for o in want.get("options") or []
               if o["name"].casefold() not in {n.casefold() for n in have.get("options") or []}]
    if not missing:
        return None
    lacking = f"column {have['name']} lacks the options " + ", ".join(o["name"] for o in missing)
    if want["type"] == "status":
        # The listing gives names only: resent, a status option would lose its group.
        raise ActionFailed(f"{lacking}; a status column's options are not changed from here: add them in its table")
    if answers.get("$rows") is None:
        raise ActionFailed(f"{lacking}; the database's row count is not known, so they were not changed: "
                           "add them in its table")
    if answers.get("$rows") != 0:
        raise ActionFailed(f"{lacking}; the database already has rows, so they were not changed: add them in its table")
    options = [{"name": n} for n in have.get("options") or []] + missing
    replaced = dict(step, skip_if_present=None, arguments={
        "database": step["arguments"]["database"], "op": "prop_update", "prop": have["name"],
        "property": {"options": options}})
    if step.get("operation_id"):
        raw = json.dumps(replaced["arguments"], sort_keys=True, ensure_ascii=False).encode("utf-8")
        # Its arguments depend on the database's live options: they are in its id.
        replaced["operation_id"] = f"{step['operation_id']}-update-{hashlib.sha256(raw).hexdigest()[:8]}"
    if "cli" in step:
        replaced["cli"] = cli_argv(replaced)
    return replaced


def column_decisions(steps: List[Dict[str, Any]], n: int, answers: Dict[str, Any],
                     present: set) -> Dict[int, Any]:
    """For each column step after step n whose column the database already has: "skip", or the
    prop_update to run in its place. Raises ActionFailed for the first that cannot be used, so a
    build that must fail fails before it writes anything to the owner's database."""
    return {id(later): existing_column(later, answers) or "skip" for later in steps[n + 1:]
            if later.get("skip_if_present") and later["skip_if_present"].strip().casefold() in present}


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
        state, recorded = ledger.claim(request["room_id"], request["request_id"], fence=fence_facts(task_text))
        if state == "done":
            return recorded
        if state == "busy":
            return completion(request["request_id"], "failed",
                              "this request is already being built")
        if state == "asked":
            return ""  # waiting on the owner's answer in the thread: nothing to say yet
        if state == "expired":
            reply = lapsed(request, ledger)
            _post_result(request, reply, call)
            return reply
    try:
        page_html, schema, name = build_inputs(request, adaptation)
        start = adaptation.get("thread_start") or f"Building {name or 'the app'}…"
        steps = with_operation_ids(request["request_id"], with_thread_start(request, plan(request, page_html, schema, name), start))
        answers = execute(steps, call)
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
    _post_result(request, reply, call)
    return reply


LAPSED = "the question in the app's thread went unanswered for 24 hours"


def lapsed(request: Dict[str, Any], ledger: "Ledger") -> str:
    """A question nobody answered in time: the request fails, recorded like any answer."""
    reply = completion(request["request_id"], "failed", LAPSED)
    ledger.record(request["room_id"], request["request_id"], reply)
    return reply


def answer_of(thread_task: str, ledger: "Ledger") -> Optional[Dict[str, Any]]:
    """The owner's answer to an open question, read off a message in that app's thread; None for
    anyone else's message, another thread, or no open question. Only the agent's own owner counts:
    the attested owner tier, the same room and the same thread root as the question."""
    if not thread_conversation():
        return None
    meta = headers(thread_task)
    owner, root, room = meta.get("user_id", ""), meta.get("thread_root", ""), meta.get("source_room_id", "")
    if meta.get("access_tier") != "owner" or not owner.startswith("@") or not _EVENT_RE.match(root) or not room:
        return None
    text = _message_text(thread_task)
    return ledger.answer(room, root, owner, text) if text else None


DEFAULT_SETTLE_S = 12  # see settle_seconds()


def _when(meta: Dict[str, str]) -> Optional[float]:
    """The task's own timestamp header, as epoch seconds; None when absent or unreadable."""
    from datetime import datetime  # noqa: PLC0415
    try:
        return datetime.fromisoformat(meta.get("timestamp", "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fence_facts(task_text: str) -> Optional[Dict[str, Any]]:
    """A DM fence's room, thread root, owner and arrival time, read whatever the switch says (the
    thread message that goes with it arrives either way); None when any is missing."""
    try:
        meta = headers(task_text)
        owner = authorize(meta)
        data = extract(task_text)
    except RequestError:
        return None
    return _facts(meta, owner, data)


def _facts(meta: Dict[str, str], owner: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    root, room, at = data.get("thread_root"), data.get("room_id"), _when(meta)
    # The root is compared with the message's validated one, so it needs no shape check of its own.
    if not (isinstance(root, str) and isinstance(room, str) and at is not None):
        return None
    message = data.get("thread_message")
    said = data.get("description")
    return {"room_id": room, "thread_root": root, "owner": owner, "at": at, "mode": data.get("mode"),
            "request_id": data.get("request_id"),
            **({"said": _said(said)} if isinstance(said, str) else {}),
            **({"thread_message": message} if isinstance(message, str) and _EVENT_RE.match(message) else {})}


def _said(text: str) -> str:
    """Text as compared for the build slot: trimmed, its whitespace collapsed."""
    return " ".join((text or "").split())


def settle_seconds() -> int:
    """How long a message in a known app thread waits for the fence that may name it
    (BUILD_THREAD_SETTLE_SECONDS, env > the manifest's config; DEFAULT_SETTLE_S when unset)."""
    try:
        from skill_config import config_value  # noqa: PLC0415  (src/ is on the path)
        value = int(config_value("BUILD_THREAD_SETTLE_SECONDS", _MANIFEST) or DEFAULT_SETTLE_S)
    except (ImportError, ValueError, TypeError):
        return DEFAULT_SETTLE_S
    return value if value > 0 else DEFAULT_SETTLE_S


def default_tasks_dir() -> Path:
    """The inbox the watcher reads: $SUTANDO_TASKS_DIR when set, else <workspace>/tasks."""
    if os.environ.get("SUTANDO_TASKS_DIR"):
        return Path(os.environ["SUTANDO_TASKS_DIR"])
    return Path(default_ledger()).parents[1] / "tasks"


def thread_task(task_text: str, ledger: "Ledger", tasks_dir: Optional[Path] = None,
                now: Optional[float] = None) -> Tuple[str, Optional[str]]:
    """What a room task in an app thread is, in this order (the gateway passes no marker, so the
    tie is exact, never by time):
    1. ("duplicate", id): a change or Try-again fence names this message (its `thread_message`);
    2. ("duplicate", id): a build fence's one slot: a message from the asking owner in that thread
       whose text is the fence's description (the client's own first message), once; closed once
       the request is answered or a question is asked;
    3. ("answer", id): the owner's answer to a request's open question (switch on);
    4. ("wait", None): switch on, a thread one of the owner's fences named within ASK_TTL_S (a
       request's own lifetime), no fence names it yet, and younger than settle_seconds(): a
       change's message arrives before its fence. Run it again after;
    5. ("none", None): an ordinary message.
    Every verdict but none needs the attested owner tier, the fence's own owner, the same room and
    the same thread root. Times are the gateway's `timestamp` headers; `now` is this host's clock."""
    return _verdict(headers(task_text), _task_line(task_text), ledger, tasks_dir, now, itself=task_text)


def _verdict(meta: Dict[str, str], line: str, ledger: "Ledger", tasks_dir: Optional[Path],
             now: Optional[float], itself: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """thread_task's checks on a message's attested headers and its task line (see thread_task)."""
    now = time.time() if now is None else now
    owner, root, room = meta.get("user_id", ""), meta.get("thread_root", ""), meta.get("source_room_id", "")
    message, at = meta.get("source_message_id", ""), _when(meta)
    if meta.get("access_tier") != "owner" or not owner.startswith("@") or not _EVENT_RE.match(root) or not room:
        return "none", None
    for path in sorted(Path(tasks_dir).glob("*.txt")) if tasks_dir and Path(tasks_dir).is_dir() else []:
        try:
            other = path.read_text(encoding="utf-8")
        except OSError:
            continue
        facts = fence_facts(other) if other != itself else None
        if facts and facts.get("room_id") == room:
            ledger.note_fence(facts)  # a fence not yet taken up is known all the same
    entries = ledger.entries(room)
    mine = [e for e in entries if (e.get("fence") or {}).get("thread_root") == root
            and (e.get("fence") or {}).get("owner") == owner]
    # 1. Named exactly.
    for e in mine:
        if message and e["fence"].get("thread_message") == message:
            return "duplicate", e.get("request_id")
    # 2. A build fence's one slot: its first message after it.
    said = _said(_words(line, strip_mention=False))
    for e in mine:
        f = e["fence"]
        # Only the client's own first message: the build's description, word for word.
        if (f.get("mode") == "build" and not f.get("thread_message") and said and f.get("said") == said
                and e.get("slot_used_by") in (None, message) and message
                and at is not None and abs(at - f.get("at", float("inf"))) <= settle_seconds()
                and (e.get("slot_used_by") == message or not e.get("asked"))):  # a reply closes nothing
            if ledger.use_slot(room, e["request_id"], message):
                return "duplicate", e.get("request_id")
    # 3. An answer.
    if thread_conversation():
        for e in entries:
            asked = e.get("asked") or {}
            if (asked.get("thread_root") == root and asked.get("owner") == owner
                    and not e.get("answer") and not e.get("reply")):
                return "answer", e.get("request_id")
    # 4. Switch on, a change's message comes before its fence: wait for it in a thread an app's
    #    fence named within ASK_TTL_S, never past the settle time from when it was first seen.
    live = [e for e in mine if now - (e["fence"].get("at") or 0) < ASK_TTL_S]
    if thread_conversation() and live and message and at is not None and now - at < settle_seconds():
        if now - ledger.first_seen(room, message, now) < settle_seconds():
            return "wait", None
    return "none", None


def intake_verdict(fields: Dict[str, Any], tier: str, ledger: "Ledger", tasks_dir: Path,
                   now: Optional[float] = None) -> Tuple[str, Optional[str]]:
    """thread_task for a room message the gateway has not queued yet (its intake gate): the same
    headers, from the gateway's task fields and the tier the bridge resolved for the sender."""
    def flat(value: Any) -> str:
        return " ".join(str(value).splitlines())
    keys = {"user_id": fields.get("user_id"), "thread_root": fields.get("thread_root"),
            "source_room_id": fields.get("source_room_id") or fields.get("channel_id"),
            "source_message_id": fields.get("source_message_id"), "timestamp": fields.get("timestamp"),
            "access_tier": tier}
    meta = {k: flat(v).strip() for k, v in keys.items() if v not in (None, "")}
    return _verdict(meta, flat(fields.get("task") or "").strip(), ledger, tasks_dir, now)


def intake_fence(fields: Dict[str, Any], tier: str, ledger: "Ledger") -> Optional[str]:
    """An owner's DM task the gateway bridge saw carrying the fence's opening line: when it is a
    whole, valid fence naming an app thread, note it (which indexes that thread, so the copy that
    follows is checked at intake) and return its request id. Anything else notes nothing."""
    meta = {k: " ".join(str(fields.get(k)).splitlines()).strip() for k in ("user_id", "channel_kind", "timestamp")
            if fields.get(k) not in (None, "")}
    meta["access_tier"] = tier
    try:
        owner = authorize(meta)
        data = extract(str(fields.get("task") or ""))
        uuid.UUID(str(data.get("request_id")))
    except (RequestError, ValueError):
        return None
    facts = _facts(meta, owner, data)
    if (not facts or data.get("mode") not in MODES or not _EVENT_RE.match(facts["thread_root"])
            or not str(facts["room_id"]).startswith("!")):
        return None
    ledger.note_fence(facts)
    return facts["request_id"]


def _task_line(task_text: str) -> str:
    return next((ln[len("task:"):].strip() for ln in task_text.splitlines() if ln.startswith("task:")), "")


def _message_text(task_text: str, strip_mention: bool = True) -> str:
    """The message's own words: the task line, less the gateway's reply context (and a mention)."""
    return _words(_task_line(task_text), strip_mention)


def _words(line: str, strip_mention: bool = True) -> str:
    line = re.sub(r"\[AG2 Space reply context.*?\[End AG2 Space reply context\]", "", line).strip()
    return (re.sub(r"^@\S+\s*", "", line) if strip_mention else line).strip()[:2000]


THREAD_FAILED = "Could not build the app. The reason is in the reply to the request."
THREAD_LAPSED = "Could not build the app: the question above went unanswered for 24 hours."


def result_line(request: Dict[str, Any], reply: str) -> Optional[Dict[str, Any]]:
    """The thread's last line for a request that failed (best effort), in fixed wording: an error's
    own text (a path, a server message) stays in the private reply. None on done: the client posts
    its own built card there."""
    body = json.loads(reply.split(f"```{FENCE}\n", 1)[1].split("\n```", 1)[0])
    if body.get("status") != "failed":
        return None
    return thread_post(request, THREAD_LAPSED if body.get("reason") == LAPSED else THREAD_FAILED)


def _post_result(request, reply, call) -> None:
    """The thread's last line, best effort from end to end: the reply is already recorded."""
    try:
        post = result_line(request, reply)
        if post is not None:
            call(dict(post, operation_id=f"cab-{request['request_id']}-result"))
    except Exception:  # noqa: BLE001 - the thread is best effort; the DM reply is recorded
        pass


# --------------------------------------------------------------------------- CLI

def _thread_command(command: str, text: str, argv: List[str], ledger: "Ledger") -> int:
    """ask / answer / thread-line. Steps print as a JSON list (empty when the switch is off or the
    request names no thread), each to run like a plan step."""
    if command == "answer":
        found = answer_of(text, ledger)
        if found is None:
            # Not the owner's answer to an open question here: the agent posts nothing.
            print("build_request: no open question this message answers", file=sys.stderr)
            return 1
        print(json.dumps(found, ensure_ascii=False))  # with "lapsed": run thread-line --result on its task_file
        return 0
    try:
        request = parse(text)
    except RequestError as error:
        print(f"build_request: {error}", file=sys.stderr)
        return 2
    if command == "ask":
        question = _flag(argv, "--question") or ""
        if not question.strip():
            raise SystemExit("build_request: ask needs --question")
        if not thread_root_of(request):
            print("build_request: this request has no app thread to ask in", file=sys.stderr)
            return 2
        # Only once the plan's membership and page checks have passed (`check` records them).
        passed = _passed_checks(_flag(argv, "--plan"), request["request_id"])
        if not {"member", "page"} <= passed:
            print("build_request: ask runs after the room membership and page checks pass "
                  "(--plan plan.json, checked with `check`)", file=sys.stderr)
            return 2
        if not ledger.ask(request["room_id"], request["request_id"], thread_root=request["thread_root"],
                          owner=request["owner"], question=question, task_text=text):
            print("[]")
            return WAITING  # already asked, or not this run's claim: never a second question
        post = thread_post(request, question, asks=True)
        print(json.dumps([dict(post, operation_id=f"cab-{request['request_id']}-ask")], ensure_ascii=False))
        return 0
    if "--result" in argv:
        entry = ledger.recorded(request["room_id"], request["request_id"]) or {}
        post = result_line(request, entry["reply"]) if entry.get("reply") else None
        op = f"cab-{request['request_id']}-result"
    else:
        post = thread_post(request, _flag(argv, "--line") or "")
        line = _flag(argv, "--line") or ""
        op = f"cab-{request['request_id']}-line-{hashlib.sha256(line.encode('utf-8')).hexdigest()[:8]}"
    print(json.dumps([dict(post, operation_id=op)] if post else [], ensure_ascii=False))
    return 0


def _plan_request_id(steps: List[Dict[str, Any]]) -> Optional[str]:
    """The request a plan is for, read off its write steps' operation ids (cab-<request id>-…)."""
    for step in steps:
        op = step.get("operation_id") or ""
        if op.startswith("cab-") and len(op) > 40:
            return op[4:40]
    return None


def _checks_path(plan_path: str) -> Path:
    return Path(plan_path + ".checks")


def _passed_checks(plan_path: Optional[str], request_id: str) -> set:
    """The checks `check` recorded as passed for this plan: "member", "page"; empty when none."""
    if not plan_path:
        return set()
    try:
        record = json.loads(_checks_path(plan_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(record.get("passed") or []) if record.get("request_id") == request_id else set()


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
    kinds = [k for k, key in (("member", "expect_member"), ("page", "expect_page")) if key in step]
    if kinds:
        rid = _plan_request_id(steps)
        done = _passed_checks(args[0], rid) | set(kinds)
        _checks_path(args[0]).write_text(json.dumps({"request_id": rid, "passed": sorted(done)}), encoding="utf-8")
    # What later steps use: a prop_add whose column is listed here is skipped, or replaced by the
    # prop_update in `columns`; a column that cannot be used fails here, before anything is written.
    columns: Dict[str, Any] = {}
    if step.get("expect_database"):
        try:
            by_step = column_decisions(steps, int(args[1]), answers, present)
        except ActionFailed as error:
            print(str(error))
            return 1
        columns = {str(i): by_step[id(later)] for i, later in enumerate(steps) if id(later) in by_step}
    shown = {k: v for k, v in answers.items() if not k.startswith("$columns") and k != "$rows"}
    listed = sorted(c["name"] for c in (answers.get("$columns") or {}).values()) or sorted(present)
    print(json.dumps({"present": listed, "answers": shown, "columns": columns}, ensure_ascii=False))
    return 0


INTAKE_LOCK_TIMEOUT_S = 1.0  # the gateway bridge gives the whole verdict 2 s, then delivers


def _intake_command(argv: List[str]) -> int:
    """intake-verdict -|FILE --ledger DIR --tasks DIR: the gateway bridge's question, as JSON
    {"fields": <the gateway's task fields>, "tier": <the tier it resolved>}; prints
    {"verdict", "request_id"}. Run out of the bridge's process: anything but exit 0 delivers."""
    try:
        folder, tasks = _flag(argv, "--ledger"), _flag(argv, "--tasks")
    except SystemExit as error:
        print(str(error), file=sys.stderr)
        return 2
    try:
        raw = sys.stdin.read() if argv[1] == "-" else Path(argv[1]).read_text(encoding="utf-8")
        data = json.loads(raw)
        fields, tier = data["fields"], data["tier"]
        if not isinstance(fields, dict) or not isinstance(tier, str):
            raise ValueError("fields must be an object and tier a string")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"build_request: bad intake question: {error}", file=sys.stderr)
        return 2
    ledger_ = Ledger(Path(folder) if folder else default_ledger(), lock_timeout=INTAKE_LOCK_TIMEOUT_S)
    if not fields.get("thread_root"):  # the owner's DM that may be a fence: note its thread
        noted = intake_fence(fields, tier, ledger_)
        print(json.dumps({"verdict": "noted" if noted else "none", "request_id": noted}))
        return 0
    try:
        verdict, request_id = intake_verdict(fields, tier, ledger_, Path(tasks) if tasks else default_tasks_dir())
    except LedgerBusy as error:
        print(f"build_request: {error}", file=sys.stderr)
        return 3
    print(json.dumps({"verdict": verdict, "request_id": request_id}))
    return 0


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("parse", "plan", "reply", "check", "chunks", "ask", "answer",
                                        "thread-line", "thread-task", "intake-verdict"):
        print("usage: build_request.py parse|plan|reply|check|chunks|ask|answer|thread-line|thread-task"
              "|intake-verdict <file> [options]", file=sys.stderr)
        return 2
    command, path = argv[0], argv[1]
    if command == "check":
        return _check(argv[1:])
    if command == "intake-verdict":
        return _intake_command(argv)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        print(f"build_request: cannot read {path}: {error}", file=sys.stderr)
        return 2
    if command == "chunks":
        for piece in chunks(text):
            print(len(piece.encode("utf-8")))
        return 0
    if command == "thread-task":
        try:
            folder, tasks = _flag(argv, "--ledger"), _flag(argv, "--tasks")
            ledger_ = Ledger(Path(folder) if folder else default_ledger())
            tasks_dir = Path(tasks) if tasks else default_tasks_dir()
        except SystemExit as error:
            print(str(error), file=sys.stderr)
            return 2
        verdict, request_id = thread_task(text, ledger_, tasks_dir)
        print(verdict if request_id is None else f"{verdict} {request_id}")
        return 0
    if command in ("ask", "answer", "thread-line"):
        try:
            folder = _flag(argv, "--ledger")
            return _thread_command(command, text, argv, Ledger(Path(folder) if folder else default_ledger()))
        except SystemExit as error:
            print(str(error), file=sys.stderr)
            return 2
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
        # Before the claim: a plan that fails holds nothing.
        start = adaptation.get("thread_start") or f"Building {name or 'the app'}…"
        steps = with_operation_ids(request["request_id"],
                                   with_thread_start(request, plan(request, page_html, schema, name), start))
    except (ActionFailed, KeyError, ValueError, TypeError, AttributeError) as error:
        facts = fence_facts(text)
        if facts:
            ledger().note_fence(facts)  # its thread message must still be known as this request's
        print(completion(request["request_id"], "failed", str(error) or type(error).__name__))
        return 1
    state, recorded = ledger().claim(request["room_id"], request["request_id"], fence=fence_facts(text))
    if state == "done":
        print(recorded)
        return ANSWERED
    if state == "asked":
        print("build_request: waiting for the owner's answer in the app's thread; ask nothing again",
              file=sys.stderr)
        return WAITING
    if state == "expired":
        print(lapsed(request, ledger()))  # then `thread-line --result` says so in the thread
        return ANSWERED
    if state == "busy":
        print("build_request: another run is building this request", file=sys.stderr)
        return 4
    if route == "cli":
        steps = [dict(step, cli=cli_argv(step)) for step in steps]
    print(json.dumps(steps, indent=2, ensure_ascii=False))
    return 0


THREAD_MARKER = "space.ag2.commons.app_thread"
THREAD_LINE_MAX = 500


def thread_root_of(request: Dict[str, Any]) -> Optional[str]:
    """The app thread this request is also spoken about in: its fence's thread_root, only with the
    skill's switch on; None means today's DM-only answer."""
    return request.get("thread_root") if thread_conversation() else None


def thread_post(request: Dict[str, Any], text: str, *, app_id: Optional[str] = None,
                asks: bool = False) -> Optional[Dict[str, Any]]:
    """One ordinary reply in the app's thread, best effort: it never fails the build. It carries the
    thread's marker (the client's own key) with the request id, the app once there is one, else the
    page, and `asks` on the one question, so the client can show the agent is waiting on the owner."""
    root = thread_root_of(request)
    if not root or not text.strip():
        return None
    marker: Dict[str, Any] = {"request_id": request["request_id"]}
    marker.update({"app": app_id} if app_id else {"page": request["page"]})
    if asks:
        marker["asks"] = True
    return {"action": "room.message.send", "room_id": request["room_id"], "best_effort": True,
            "arguments": {"body": " ".join(text.split())[:THREAD_LINE_MAX], "thread_root": root,
                          "extra_content": {THREAD_MARKER: marker}}}


def with_thread_start(request: Dict[str, Any], steps: List[Dict[str, Any]], line: str) -> List[Dict[str, Any]]:
    """The plan with the thread's start line right before its first write: the checks have passed."""
    post = thread_post(request, line)
    if post is None:
        return steps
    first = next((i for i, st in enumerate(steps) if not st.get("read")), len(steps))
    return steps[:first] + [post] + steps[first:]


def with_operation_ids(request_id: str, steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each write step with a fixed operation id from the request and the step's place, so a step
    retried after an unknown outcome sends the same id (the server dedupes it where it can)."""
    def digest(step):
        raw = json.dumps(step["arguments"], sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:8]
    # The arguments are in the id: a re-plan with another adaptation is another operation.
    return [step if step.get("read") else dict(step, operation_id=f"cab-{request_id}-{i}-{digest(step)}")
            for i, step in enumerate(steps)]


def cli_argv(step: Dict[str, Any]) -> List[str]:
    """The step as room-commons' `action` command (room_commons.py action …): the same Action on
    the deployment the CLI's lane reaches, its result printed in the shape `check` reads. `$db`
    and `$app` stay placeholders, filled from the answers `check` prints, as for an MCP host."""
    target = "account" if step.get("target_kind") == "account" else step["room_id"]
    argv = ["action", target, step["action"], "--args",
            json.dumps(step["arguments"], ensure_ascii=False, separators=(",", ":"))]
    if step.get("read"):
        return argv + ["--read"]
    return argv + (["--operation-id", step["operation_id"]] if step.get("operation_id") else [])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
