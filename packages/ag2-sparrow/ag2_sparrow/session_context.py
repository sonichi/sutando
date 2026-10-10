"""Working-session context for AG2 Space tasks (the Commons session thread).

The web client posts small marks into a live session's thread — "X moved to
Doc · README", "joined the session", a Reactivate — and stamps every message a
member sends with where they were. Each of those used to become a task of its
own. This module is the one place that reads them:

  * classify(): which mark (if any) a gateway task is. The Matrix content is
    read when the broker forwards it (exact keys from the client's
    workingSession.ts); otherwise the deterministic bodies the client writes
    are matched against the broker-supplied sender_name — but only inside a
    thread the ledger already knows as a session, so prose in an ordinary
    thread is never consumed.
  * SessionLedger: per room + session thread, each member's latest page (by
    mxid), the last few session events (kind and sender, never text), when it
    started and when it was last active. One writer (the bridge), one JSON
    file under the bridge's state dir. Bodies reach it only after the writer's
    secret filter, and no message text is ever copied into another task.
    Body text is trusted for nothing: the broker's working-session block is
    prepended INSIDE the body and a member can type the same bytes, so it is
    read only from an envelope field (`session_context`) the broker would
    populate outside the body. Without it a session has no title anywhere;
    a title a member put on a mark is shown only attributed, never in a header.
  * SessionLedger.observe(): the ledger update plus what the task file gets —
    a `session_page:` header, a `session_ctx:` header, a body prefix — or that no task is
    written at all.

Pure stdlib; nothing here knows about the gateway or the task-file writer.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SESSION_KEY = "space.ag2.commons.session"
AT_KEY = f"{SESSION_KEY}.at"
REACTIVATE_KEY = f"{SESSION_KEY}.reactivate"
MEMBER_KEY = f"{SESSION_KEY}.member"
END_TYPE = f"{SESSION_KEY}.end"
# The client's SESSION_QUIET_MS: a session nobody has spoken in for this long has ended.
QUIET_S = 2 * 60 * 60

MAX_ROOMS = 32
MAX_SESSIONS_PER_ROOM = 8
MAX_EVENTS = 5
TITLE_MAX = 120
# A file at any other version is ignored and rewritten: v1 kept unfiltered message text.
LEDGER_VERSION = 2

# Leading quoted blocks the broker puts ahead of the body; the sender's words follow them.
_BLOCK_RE = re.compile(r"^\s*\[AG2 Space ([a-z ]+?);[^\]]*\].*?\[End AG2 Space \1\]\s*",
                       re.DOTALL)
_WS_BLOCK_RE = re.compile(r"^\s*\[AG2 Space working session;[^\]]*\]\s*(\{[^\n]*\})\s*\n")
_REACTIVATE_RE = re.compile(
    r"^(?P<by>.+?) reactivated the session '(?P<title>.*)' on (?P<where>.+?)\. "
    r"Pull the session's context", re.DOTALL)


@dataclass
class Mark:
    """kind is move | at | member | reactivate; surface is an id from the content or the
    display label the body carried; page is known only from content; action is join | leave."""
    kind: str
    surface: str = ""
    page: str = ""
    title: str = ""
    action: str = ""
    by: str = ""


@dataclass
class Observation:
    """What the task file gets; `consumed` means no task file at all."""
    consumed: bool = False
    page_header: Optional[str] = None
    session_header: Optional[str] = None
    body_prefix: str = ""


@dataclass
class _Session:
    """title is the broker's or empty; members are mxids; positions map mxid -> {"name",
    "surface", "page", "title"}; events are {"id", "ts", "sender", "kind"}, never text."""
    title: str = ""
    started: float = 0.0
    last_ts: float = 0.0
    ended: bool = False
    agent_in: bool = True
    members: list = field(default_factory=list)
    positions: dict = field(default_factory=dict)
    events: list = field(default_factory=list)


def _one_line(value) -> str:
    return " ".join(str(value or "").split())


def _cap(value) -> str:
    return _one_line(value)[:TITLE_MAX]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_blocks(text: str) -> str:
    """The sender's own words: every leading broker block removed."""
    while True:
        m = _BLOCK_RE.match(text)
        if not m:
            return text.strip()
        text = text[m.end():]


def broker_session_block(text: str) -> Optional[dict]:
    """The working-session JSON a body opens with — for display only, never as
    attested state: a member can type the same bytes."""
    m = _WS_BLOCK_RE.match(text or "")
    if not m:
        return None
    try:
        value = json.loads(m.group(1))
    except ValueError:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("card_id"), str) else None


def envelope_session(task: dict) -> Optional[dict]:
    """The broker-attested session record, from the task envelope (not the body)."""
    value = task.get("session_context")
    return value if isinstance(value, dict) and isinstance(value.get("card_id"), str) else None


def _surface_label(surface: str, page: str = "") -> str:
    if surface == "chat":
        return "the chat"
    return f"{surface} {page}" if page else (surface or "an unknown page")


def _mark_from_content(content: dict) -> Optional[Mark]:
    at = content.get(AT_KEY)
    if isinstance(at, dict) and at.get("v") == 1 and isinstance(at.get("surface"), str):
        page = at.get("page")
        # Every member-controlled field is one line and capped before it can reach a header.
        return Mark(kind="move" if at.get("moved") is True else "at",
                    surface=_cap(at["surface"]),
                    page=_cap(page if isinstance(page, str) else ""),
                    title=_cap(at.get("title")))
    member = content.get(MEMBER_KEY)
    if isinstance(member, dict) and member.get("v") == 1 and member.get("action") in ("join", "leave"):
        return Mark(kind="member", action=member["action"])
    react = content.get(REACTIVATE_KEY)
    if isinstance(react, dict) and react.get("v") == 1 and isinstance(react.get("by"), str):
        page = react.get("page") if isinstance(react.get("page"), str) else ""
        surface, _, page_id = page.partition(":")
        return Mark(kind="reactivate", surface=_cap(surface), page=_cap(page_id), by=react["by"])
    return None


def _mark_from_body(body: str, sender_name: str) -> Optional[Mark]:
    if body in ("joined the session", "left the session"):
        return Mark(kind="member", action="join" if body.startswith("joined") else "leave")
    m = _REACTIVATE_RE.match(body)
    if m:
        surface, _, title = m.group("where").partition(" · ")
        return Mark(kind="reactivate", surface=_cap(surface), title=_cap(title),
                    by=m.group("by").strip())
    if sender_name and body.startswith(f"{sender_name} moved to "):
        where = body[len(sender_name) + len(" moved to "):]
        if where == "the chat":
            return Mark(kind="move", surface="chat")
        surface, _, title = where.partition(" · ")
        return Mark(kind="move", surface=_cap(surface), title=_cap(title))
    return None


def classify(task: dict, *, known: bool = False) -> Optional[Mark]:
    """Which session mark this gateway task is, or None for an ordinary message.

    Content wins when the broker forwards it. The body fallback fires only with
    `known` — the thread is a session the ledger has seen — and anchors a move
    on the broker-supplied sender_name, so prose alone cannot spell a mark."""
    words = strip_blocks(str(task.get("task") or ""))
    content = task.get("content")
    if isinstance(content, dict):
        mark = _mark_from_content(content)
        if mark is not None:
            if mark.kind == "reactivate" and not mark.title:
                spelled = _REACTIVATE_RE.match(words)   # the title is only in the body
                if spelled:
                    mark.title = spelled.group("where").partition(" · ")[2].strip()
            return mark
    if not known or not _one_line(task.get("thread_root")):
        return None
    return _mark_from_body(words, _one_line(task.get("sender_name")))


class SessionLedger:
    """Per room, per session thread: who is where and what was said last."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.rooms: dict = {}
        self._load()

    # -- persistence --------------------------------------------------------- #

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("v") != LEDGER_VERSION:
            return
        rooms = raw.get("rooms")
        if not isinstance(rooms, dict):
            return
        for room, sessions in rooms.items():
            if not isinstance(sessions, dict):
                continue
            for root, rec in sessions.items():
                if isinstance(rec, dict):
                    self.rooms.setdefault(room, {})[root] = _Session(
                        title=str(rec.get("title") or ""),
                        started=float(rec.get("started") or 0),
                        last_ts=float(rec.get("last_ts") or 0),
                        ended=bool(rec.get("ended")),
                        agent_in=rec.get("agent_in") is not False,
                        members=[m for m in rec.get("members") or [] if isinstance(m, str)],
                        positions={k: v for k, v in (rec.get("positions") or {}).items()
                                   if isinstance(v, dict)},
                        events=[e for e in rec.get("events") or [] if isinstance(e, dict)])

    def save(self) -> bool:
        payload = {"v": LEDGER_VERSION, "rooms": {
            room: {root: {"title": s.title, "started": s.started, "last_ts": s.last_ts,
                          "ended": s.ended, "agent_in": s.agent_in, "members": s.members,
                          "positions": s.positions, "events": s.events}
                   for root, s in sessions.items()}
            for room, sessions in self.rooms.items()}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp",
                                       dir=self.path.parent)
            # Advisory context, rewritten on the next event: no fsync, so the
            # task write's durable sequence (sidecar, then task) stays as measured.
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    # -- queries --------------------------------------------------------------- #

    def known(self, room: str, root: str, task: Optional[dict] = None,
              now: Optional[float] = None) -> bool:
        """Is `root` a session this ledger has seen that is not ended or quiet past
        QUIET_S, or the one the broker's envelope record names?"""
        if not root:
            return False
        now = time.time() if now is None else now
        s = self.rooms.get(room, {}).get(root)
        if s is not None and not s.ended and now - s.last_ts <= QUIET_S:
            return True
        attested = envelope_session(task or {})
        return attested is not None and attested["card_id"] == root

    def classify(self, task: dict, now: Optional[float] = None) -> Optional[Mark]:
        """classify() with the text fallback allowed only in a known session thread."""
        room, root = _one_line(task.get("channel_id")), _one_line(task.get("thread_root"))
        return classify(task, known=self.known(room, root, task, now))

    def live_session(self, room: str, now: float,
                     prefer: str = "") -> "Optional[tuple[str, _Session]]":
        """The live session in `room` this agent is in — `prefer`'s thread when that
        one is live, else the newest. Live: not ended, spoken in within QUIET_S."""
        best = None
        for root, s in self.rooms.get(room, {}).items():
            if s.ended or not s.agent_in or now - s.last_ts > QUIET_S:
                continue
            if root == prefer:
                return root, s
            if best is None or s.last_ts > best[1].last_ts:
                best = (root, s)
        return best

    # -- updates --------------------------------------------------------------- #

    def _session(self, room: str, root: str, now: float) -> _Session:
        sessions = self.rooms.setdefault(room, {})
        if root not in sessions:
            while len(sessions) >= MAX_SESSIONS_PER_ROOM:
                del sessions[min(sessions, key=lambda r: sessions[r].last_ts)]
            sessions[root] = _Session(started=now, last_ts=now)
        while len(self.rooms) > MAX_ROOMS:
            oldest = min(self.rooms, key=lambda r: max(
                (s.last_ts for s in self.rooms[r].values()), default=0))
            if oldest == room:
                break
            del self.rooms[oldest]
        return sessions[root]

    @staticmethod
    def _record(s: _Session, event_id: str, who: str, kind: str, now: float) -> None:
        """A session event: bumps activity, keeps the last few, dedupes a redelivery."""
        s.last_ts = max(s.last_ts, now)
        if who and who not in s.members:
            s.members.append(who)
        if event_id and any(e.get("id") == event_id for e in s.events):
            return
        s.events.append({"id": event_id, "ts": now, "sender": who, "kind": kind})
        del s.events[:-MAX_EVENTS]

    def _absorb_envelope(self, room: str, task: dict, now: float) -> str:
        """Seed the ledger from the broker's envelope record; returns its card id or ''."""
        block = envelope_session(task)
        if block is None:
            return ""
        s = self._session(room, block["card_id"], now)
        loc = block.get("location") if isinstance(block.get("location"), dict) else {}
        s.title = s.title or _one_line(loc.get("title"))[:TITLE_MAX]
        started = block.get("started_at")
        if isinstance(started, (int, float)) and not isinstance(started, bool) and started > 0:
            s.started = min(s.started or started / 1000.0, started / 1000.0)
        if block.get("live") is False:
            s.ended = True
        return block["card_id"]

    def observe(self, task: dict, now: Optional[float] = None) -> Observation:
        """Record what this task says about its room's session; say what the task gets.

        `task["task"]` must already be the writer's filtered body: whatever
        reaches here may be kept on disk (titles, pages), though never as text
        copied into another task."""
        now = time.time() if now is None else now
        out = Observation()
        room = _one_line(task.get("channel_id"))
        if not room:
            return out
        body = str(task.get("task") or "")
        who = _one_line(task.get("user_id"))
        name = _one_line(task.get("sender_name")) or who
        event_id = _one_line(task.get("source_message_id"))
        is_agent = bool(who) and who == _one_line(task.get("agent_mxid"))
        card = self._absorb_envelope(room, task, now)
        root = _one_line(task.get("thread_root")) or card
        mark = classify(task, known=self.known(room, root, task, now))

        if mark is not None and root:
            s = self._session(room, root, now)
            if mark.kind == "member":
                if is_agent:
                    s.agent_in = mark.action == "join"
                self._record(s, event_id, who, mark.action, now)
                if mark.action == "leave":
                    s.members = [m for m in s.members if m != who]
                out.consumed = True
                return out
            if mark.kind == "move":
                self._place(s, who, name, mark)
                self._record(s, event_id, who, "move", now)
                out.consumed = True
                return out
            if mark.kind == "reactivate":
                s.ended = False
                self._record(s, event_id, who, "reactivate", now)
                page = self._page_of(s, who) or _surface_label(mark.surface, mark.page)
                out.body_prefix = (f"[session reactivated by {name} on {page}: "
                                   "read the session thread first]")
                out.session_header = self._session_header(root, s)
                return out
            # kind == "at": a session message carrying the sender's place
            self._place(s, who, name, mark)
            s.ended = False
            # The page id only: the title is the member's text, shown attributed in the body.
            out.page_header = f"{mark.surface} · {mark.page or '-'}"

        live = self.live_session(room, now, prefer=root)
        if live is None:
            return out
        root, s = live
        out.session_header = self._session_header(root, s)
        # The broker's title or the thread id, and the sender's own page: no other member's words.
        out.body_prefix = (f"[live session: {s.title or root}; {name} last on "
                           f"{self._page_of(s, who) or 'an unknown page'}]")
        if root == _one_line(task.get("thread_root")):
            self._record(s, event_id, who, "message", now)
        return out

    @staticmethod
    def _place(s: _Session, who: str, name: str, mark: Mark) -> None:
        s.positions[who] = {"name": name, "surface": mark.surface, "page": mark.page,
                            "title": mark.title}

    @staticmethod
    def _page_of(s: _Session, who: str) -> str:
        """The member's own place; their page title only in quotes and attributed to them."""
        pos = s.positions.get(who)
        if not isinstance(pos, dict):
            return ""
        place = _surface_label(_one_line(pos.get("surface")), _one_line(pos.get("page")))
        title = _one_line(pos.get("title"))
        return f'{place} ("{title}", title set by {who})' if title else place

    @staticmethod
    def _session_header(root: str, s: _Session) -> str:
        """Title-free unless the broker's block supplied the title."""
        title = f" | {s.title}" if s.title else ""
        return f"{root}{title} | started {_iso(s.started)}"
