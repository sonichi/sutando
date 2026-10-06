#!/usr/bin/env python3
"""navigate — point the owner's Navigator at a room with the `room.navigate` Action.

`room.navigate` posts a focus pointer (room, view, location, reason) into the DM the
agent shares with its owner, where the owner's client shows it as a row with Open.

Two entry points:
  - `to`: one navigate, as asked.
  - `mention --task-file`: the owner-mention rule. A task the gateway marked
    `owner_mentioned: true` points the Navigator at the mentioning message, at most
    once per message, never at the owner DM itself, and mentions that land within
    the coalescing window of the last navigate are folded into one trailing
    navigate to the latest of them (`flush`, run detached at the window's end).

The Action is called through the AG2 Space Actions door the way every agent reaches it:
the room-ops gateway bearer discovers the hosted MCP on its relay, mints a short-lived
delegation there, and calls `room.action.execute`. Standard library only; this module
does not import the room-commons skill.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gateway as _gw  # noqa: E402

ACTION = "room.navigate"
REASON_MAX = 280
WINDOW_CONFIG_KEY = "OWNER_MENTION_NAVIGATE_WINDOW_S"
DEFAULT_WINDOW_S = 120.0
SEEN_TTL_S = 86400.0
SEEN_MAX = 500
MCP_PROTOCOL = "2025-06-18"
USER_AGENT = "sutando-room-ops/1"
TIMEOUT_S = 30


# The Action input
def clip_reason(reason):
    if reason is None:
        return None
    text = " ".join(str(reason).split())
    if not text:
        return None
    return text if len(text) <= REASON_MAX else text[: REASON_MAX - 1].rstrip() + "…"


def build_input(room_id, view="chat", event_id=None, thread_id=None, page=None, reason=None):
    """The `room.navigate` arguments: {room_id, view, location, reason}."""
    location = {k: v for k, v in (("event_id", event_id), ("thread_id", thread_id),
                                  ("page", page)) if v}
    return {"room_id": room_id, "view": view or "chat",
            "location": location or None, "reason": clip_reason(reason)}


def operation_id_for(owner_dm, arguments):
    """Stable per target, so a retried call is the same operation server-side."""
    key = json.dumps([owner_dm, arguments["room_id"], arguments.get("location")], sort_keys=True)
    return "nav-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


# The Actions door (hosted MCP, reached from the gateway bearer)
class Refused(Exception):
    """The Action did not complete; `code` is the server's code when it gave one."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


def _post(url, token, body=None, headers=None, method="POST"):
    h = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT, **(headers or {})}
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise Refused(f"{urllib.parse.urlsplit(url).netloc} unreachable: {exc}", "UNREACHABLE")


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        return None


class Door:
    """One MCP session on the hosted Actions service, opened from the relay bearer."""

    def __init__(self, relay_base, bearer, post=_post):
        self._post, self._next, self._session = post, 0, None
        status, _, raw = post(relay_base.rstrip("/") + "/v1/mcp/discovery", bearer, method="GET")
        found = _json(raw) if status == 200 else None
        if not isinstance(found, dict) or not str(found.get("mcp_url", "")).startswith("https://"):
            raise Refused(f"MCP discovery refused ({status})", "NO_MCP")
        relay_host = (urllib.parse.urlsplit(relay_base).hostname or "").lower()
        mint_url = str(found.get("mint_url") or "")
        if (urllib.parse.urlsplit(mint_url).hostname or "").lower() != relay_host:
            raise Refused("the mint URL is not on the relay's host; the bearer is not sent there",
                          "MINT_HOST")
        status, _, raw = post(mint_url, bearer, {})
        access = (_json(raw) or {}).get("access_token") if 200 <= status < 300 else None
        if not isinstance(access, str) or not access:
            raise Refused(f"minting an MCP delegation was refused ({status})", "MINT")
        self._url, self._token = found["mcp_url"], access
        self._rpc("initialize", {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "sutando-room-ops", "version": "1"}})
        self._rpc("notifications/initialized", None, notify=True)

    def _rpc(self, method, params, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._next += 1
            msg["id"] = self._next
        headers = {"Accept": "application/json, text/event-stream"}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        status, hdrs, raw = self._post(self._url, self._token, msg, headers)
        self._session = hdrs.get("mcp-session-id") or self._session
        if notify:
            return None
        if status != 200:
            raise Refused(f"MCP {method} refused ({status})", "MCP_HTTP")
        body = None
        if hdrs.get("content-type", "").startswith("text/event-stream"):
            for line in raw.decode("utf-8", "replace").splitlines():
                got = _json(line[5:].strip().encode("utf-8")) if line.startswith("data:") else None
                if isinstance(got, dict) and got.get("id") == msg["id"]:
                    body = got
        else:
            body = _json(raw)
        if not isinstance(body, dict) or isinstance(body.get("error"), dict):
            raise Refused(f"MCP {method} failed", "MCP_RPC")
        return body.get("result")

    def call(self, tool, arguments):
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if not isinstance(result, dict):
            raise Refused(f"{tool} answered without a result", "MCP_RPC")
        if result.get("isError"):
            text = next((b.get("text", "") for b in result.get("content") or []
                         if isinstance(b, dict) and b.get("type") == "text"), "")
            env = _json(str(text).encode("utf-8")) or {"message": text}
            raise Refused(str(env.get("message") or "refused"), str(env.get("code") or ""))
        out = result.get("structuredContent")
        if not isinstance(out, dict):
            raise Refused(f"{tool} answered without structured content", "MCP_RPC")
        return out

    def execute(self, room_id, action, arguments, operation_id):
        d = self.call("room.actions.describe", {"room_id": room_id, "action": action})
        got = self.call("room.action.execute", {
            "room_id": room_id, "action": action, "arguments": dict(arguments),
            "expected_action_revision": d.get("action_revision"),
            "expected_catalog_version": d.get("catalog_version"),
            "operation_id": operation_id})
        if got.get("status") != "completed" or not isinstance(got.get("result"), dict):
            raise Refused(f"{action} did not complete (status {got.get('status')!r}); operation "
                          f"{operation_id} is not re-run", "NOT_COMPLETED")
        return got["result"]


def open_door():
    base, headers = _gw.gateway()
    bearer = (headers.get("Authorization") or "").partition(" ")[2]
    if not base or not bearer:
        raise Refused("no gateway configured", "NO_GATEWAY")
    return Door(base, bearer)


def navigate(owner_dm, arguments, door_factory=open_door):
    """Execute room.navigate in the owner DM; {"ok", "event_id"|"reason", "code"}."""
    try:
        out = door_factory().execute(owner_dm, ACTION, arguments,
                                     operation_id_for(owner_dm, arguments))
    except Refused as exc:
        return {"ok": False, "reason": str(exc), "code": exc.code}
    return {"ok": True, "event_id": out.get("event_id")}


# Workspace state: the owner DM, the per-mention ledger
def _workspace():
    if not _gw._core_src_on_path():
        raise RuntimeError("core src/ not found; cannot resolve the workspace")
    from workspace_default import resolve_workspace
    return Path(resolve_workspace())


def _instance_suffix():
    inst = os.environ.get("GATEWAY_INSTANCE") or ""
    return f".{inst}" if inst else ""


class Rooms:
    """What the owner-DM resolution reads, through the room-ops gateway."""

    def agents(self):
        base, headers = _gw.gateway()
        _status, res = _gw.http_json("GET", f"{base}/v1/agents", headers)
        return res.get("agents") or [] if isinstance(res, dict) else []

    def joined(self):
        import rooms as _rooms
        return [r for r in _rooms.joined_rooms().get("rooms") or [] if isinstance(r, str)]

    def members(self, room_id):
        import members as _members
        res = _members.room_members(room_id)
        return {m["user_id"] for m in res["members"]} if res.get("ok") else None

    def last_message_by(self, room_id, sender):
        import read as _read
        res = _read.read_room(room_id, limit=OWNER_DM_HISTORY)
        stamps = [float(m.get("ts") or 0) for m in res.get("messages") or []
                  if m.get("sender") == sender]
        return max(stamps) if stamps else None


OWNER_DM_TTL_S = 86400.0
OWNER_DM_HISTORY = 50
OWNER_DM_SCAN_WORKERS = 8


def _routing(workspace, gateway_base):
    path = Path(workspace) / "state" / f"owner-routing{_instance_suffix()}.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ok = isinstance(d, dict) and str(d.get("gateway") or "") == (gateway_base or "")
    return d if ok else {}


def _dm_cache(workspace):
    return Path(workspace) / "state" / f"owner-mention-dm{_instance_suffix()}.json"


def _read_dm_cache(workspace):
    try:
        c = json.loads(_dm_cache(workspace).read_text(encoding="utf-8"))
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_dm_cache(workspace, data):
    cache = _dm_cache(workspace)
    tmp = cache.with_name(f".{cache.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, cache)


def is_dm_refusal(res):
    """The Action refused the room it was sent in, not the target ("...only into the DM...")."""
    return not res.get("ok") and " DM " in f" {res.get('reason') or ''} "


def forget_owner_dm(workspace, refused_room=""):
    """Drop the cached DM; a room the Action refused as the owner DM is never picked again."""
    c = _read_dm_cache(workspace)
    refused = [r for r in c.get("refused") or [] if r != refused_room][-49:]
    _write_dm_cache(workspace, {"refused": refused + ([refused_room] if refused_room else [])})


def pick_owner_dm(candidates, last_owner_ts):
    """The candidate with the most recent owner message; with none, the lowest room id."""
    spoken = [(ts, r) for r in candidates if (ts := last_owner_ts(r)) is not None]
    if spoken:
        return max(spoken)[1], "most recent owner message"
    return (min(candidates), "no owner message in reach; lowest room id") if candidates else ("", "")


def owner_dm_room(workspace, gateway_base, rooms=None, now=None):
    """The DM whose members are exactly {this agent, its owner}: the only room `room.navigate`
    accepts. The gateway's owner_dm_room reading is not used as-is; it may hold others."""
    now = time.time() if now is None else now
    routing = _routing(workspace, gateway_base)
    agent = os.environ.get("AGENT_MXID") or str(routing.get("identity") or "")
    if not agent:
        return ""
    c = _read_dm_cache(workspace)
    try:
        if (c.get("agent"), c.get("gateway")) == (agent, gateway_base) \
                and now - float(c.get("at") or 0) < OWNER_DM_TTL_S and c.get("room"):
            return c["room"]
    except (ValueError, TypeError):
        pass
    refused = set(c.get("refused") or [])
    rooms = rooms or Rooms()
    row = next((r for r in rooms.agents() if isinstance(r, dict) and r.get("id") == agent), {})
    owner = str(row.get("owner") or "")
    if not owner.startswith("@"):
        return ""
    want = {agent, owner}
    from concurrent.futures import ThreadPoolExecutor
    joined = rooms.joined()
    with ThreadPoolExecutor(OWNER_DM_SCAN_WORKERS) as pool:
        got = list(pool.map(rooms.members, joined))
    candidates = sorted(r for r, m in zip(joined, got) if m == want and r not in refused)
    room, why = pick_owner_dm(candidates, lambda r: rooms.last_message_by(r, owner))
    if room:
        _log(workspace, f"owner DM for {agent}: {room} ({why}; {len(candidates)} candidate(s): "
                        f"{', '.join(candidates)})")
        _write_dm_cache(workspace, {"agent": agent, "gateway": gateway_base, "room": room,
                                    "at": now, "candidates": candidates,
                                    "refused": sorted(refused)})
    return room


def window_seconds(explicit=None):
    """CLI > env > this skill's manifest config > the built-in default."""
    for raw in (explicit, os.environ.get(WINDOW_CONFIG_KEY)):
        if raw not in (None, ""):
            return float(raw)
    try:
        cfg = json.loads((Path(__file__).parent / "manifest.json").read_text())["config"]
        return float(cfg[WINDOW_CONFIG_KEY])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_WINDOW_S


class Ledger:
    """`state/owner-mention-navigate*.json` under an exclusive lock for the whole step."""

    def __init__(self, workspace):
        state = Path(workspace) / "state"
        state.mkdir(parents=True, exist_ok=True)
        self.path = state / f"owner-mention-navigate{_instance_suffix()}.json"
        self._lock_path = self.path.with_name("." + self.path.name + ".lock")

    def __enter__(self):
        self._fh = open(self._lock_path, "a")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.data = data if isinstance(data, dict) else {}
        self.data.setdefault("seen", {})
        return self

    def save(self):
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


# The owner-mention decision (pure)
@dataclass
class Mention:
    room_id: str
    event_id: str
    thread_id: str = ""
    sender: str = ""
    room_name: str = ""

    def as_dict(self):
        return dict(self.__dict__)


def mention_from_task(text, workspace=None):
    """The mention a task attests, or None when it is not an owner-mention task.

    The flag comes from the strict parse; the fields written below `task:` only from
    the attested one, so a body line can neither claim a mention nor aim it."""
    if not _gw._core_src_on_path():
        return None
    from policy.egress.result import is_owner_mention_task
    from task_envelope import attested_task_headers
    if not is_owner_mention_task(text):
        return None
    h = attested_task_headers(text, workspace).headers
    room = (h.get("source_room_id") or h.get("channel_id") or "").strip()
    event = (h.get("source_message_id") or "").strip()
    thread = (h.get("thread_root") or "").strip()
    return Mention(room_id=room, event_id=event, thread_id="" if thread == event else thread,
                   sender=(h.get("sender_name") or h.get("user_id") or "").strip(),
                   room_name=(h.get("room_name") or "").strip())


def reason_for(m, extra=0):
    who = m.sender or "Someone"
    where = m.room_name or m.room_id
    more = f" (+{extra} more mention{'s' if extra != 1 else ''} just before)" if extra else ""
    return clip_reason(f"{who} mentioned you in {where}{more}")


@dataclass
class Decision:
    kind: str  # "navigate" | "hold" | "skip"
    why: str = ""
    flush_at: float = 0.0


def decide(m, data, now, owner_dm, window):
    """What to do with one mention, given the ledger. Mutates nothing."""
    if m is None:
        return Decision("skip", "not an owner-mention task")
    if not owner_dm:
        return Decision("skip", "no owner DM reading for this gateway")
    if not m.room_id or not m.event_id:
        return Decision("skip", "the task attests no room or message id")
    if m.room_id == owner_dm:
        return Decision("skip", "the mention is in the owner DM itself")
    if m.event_id in data.get("seen", {}):
        return Decision("skip", "already navigated for this message")
    last = float(data.get("last_nav_at") or 0.0)
    if now - last < window:
        return Decision("hold", "inside the coalescing window", flush_at=last + window)
    return Decision("navigate")


def _remember(data, event_id, now):
    seen = {k: v for k, v in data.get("seen", {}).items() if now - float(v) < SEEN_TTL_S}
    seen[event_id] = now
    if len(seen) > SEEN_MAX:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-SEEN_MAX:])
    data["seen"] = seen


def _arguments(m, extra=0):
    return build_input(m.room_id, "chat", event_id=m.event_id, thread_id=m.thread_id or None,
                       reason=reason_for(m, extra))


def _fallback_line(m, why):
    return (f"(Your Navigator was not moved to {m.room_name or m.room_id}: {why})")


def on_mention(m, workspace, now=None, owner_dm=None, window=None,
               door_factory=open_door, spawn_flush=None):
    """Apply the owner-mention rule to one mention; a JSON-able outcome."""
    now = time.time() if now is None else now
    window = window_seconds() if window is None else window
    if m is None:
        return {"ok": True, "navigated": False, "skipped": "not an owner-mention task"}
    if owner_dm is None:
        owner_dm = owner_dm_room(workspace, _gw.gateway()[0])
    with Ledger(workspace) as led:
        d = decide(m, led.data, now, owner_dm, window)
        if d.kind == "skip":
            return {"ok": True, "navigated": False, "skipped": d.why}
        _remember(led.data, m.event_id, now)
        if d.kind == "hold":
            pending = led.data.get("pending")
            live = isinstance(pending, dict) and float(pending.get("flush_at") or 0) >= now
            folded = int(pending.get("folded", 0)) + 1 if isinstance(pending, dict) else 0
            led.data["pending"] = {**m.as_dict(), "folded": folded, "flush_at": d.flush_at}
            led.save()
            if not live:  # no trailing flush is waiting for this window
                (spawn_flush or _spawn_flush)(d.flush_at)
            return {"ok": True, "navigated": False, "held": True, "flush_at": d.flush_at}
        led.data["last_nav_at"] = now
        led.data["pending"] = None
        led.save()
    res = navigate(owner_dm, _arguments(m), door_factory)
    if not res["ok"]:
        _log(workspace, f"navigate refused for {m.event_id}: {res['reason']}")
        if is_dm_refusal(res):
            forget_owner_dm(workspace, owner_dm)
        res["dm_line"] = _fallback_line(m, res["reason"])
    return {**res, "navigated": res["ok"]}


def flush(workspace, now=None, owner_dm=None, window=None, door_factory=open_door,
          dm_writer=None):
    """The trailing navigate: the latest held mention, once the window has passed."""
    now = time.time() if now is None else now
    window = window_seconds() if window is None else window
    if owner_dm is None:
        owner_dm = owner_dm_room(workspace, _gw.gateway()[0])
    with Ledger(workspace) as led:
        pending = led.data.get("pending")
        if not isinstance(pending, dict):
            return {"ok": True, "navigated": False, "skipped": "nothing held"}
        due = float(led.data.get("last_nav_at") or 0.0) + window
        if now < due:
            return {"ok": True, "navigated": False, "skipped": "window still open", "due": due}
        led.data["pending"] = None
        led.data["last_nav_at"] = now
        led.save()
    folded = int(pending.get("folded", 0))
    m = Mention(**{k: pending.get(k, "") for k in Mention.__dataclass_fields__})
    if not owner_dm:
        return {"ok": True, "navigated": False, "skipped": "no owner DM reading for this gateway"}
    res = navigate(owner_dm, _arguments(m, folded), door_factory)
    if not res["ok"]:
        _log(workspace, f"trailing navigate refused for {m.event_id}: {res['reason']}")
        if is_dm_refusal(res):
            forget_owner_dm(workspace, owner_dm)
        (dm_writer or _write_dm_line)(workspace, _fallback_line(m, res["reason"]))
    return {**res, "navigated": res["ok"]}


def _spawn_flush(at):
    room_ops = os.path.join(os.path.dirname(os.path.abspath(__file__)), "room_ops.py")
    subprocess.Popen([sys.executable, room_ops, "navigate", "flush", "--not-before", str(at)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def _log(workspace, line):
    try:
        logs = Path(workspace) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(logs / "owner-mention-navigate.log", "a", encoding="utf-8") as f:
            f.write(f"{stamp} {line}\n")
    except OSError:
        pass
    print(line, file=sys.stderr)


def _write_dm_line(workspace, line):
    """An owner-DM proactive line through the ag2space proactive leg, published atomically."""
    if not _gw._core_src_on_path():
        return
    from proactive_routing import proactive_filename
    results = Path(workspace) / "results"
    results.mkdir(parents=True, exist_ok=True)
    name = proactive_filename(int(time.time() * 1000), "ag2space")
    tmp = results / f".{name}.{os.getpid()}"
    tmp.write_text(line + "\n", encoding="utf-8")
    os.replace(tmp, results / name)


# CLI (also reached as `room_ops.py navigate ...`)
def add_arguments(p):
    sub = p.add_subparsers(dest="nav_cmd", required=True)
    t = sub.add_parser("to", help="point the owner's Navigator at a room")
    t.add_argument("room_id")
    t.add_argument("--view", default="chat")
    t.add_argument("--event", dest="event_id", default=None)
    t.add_argument("--thread", dest="thread_id", default=None)
    t.add_argument("--page", default=None)
    t.add_argument("--reason", default=None)
    t.add_argument("--owner-dm", dest="owner_dm", default=None)
    m = sub.add_parser("mention", help="apply the owner-mention rule to one task file")
    m.add_argument("--task-file", required=True)
    m.add_argument("--window", default=None)
    f = sub.add_parser("flush", help="run the trailing navigate for held mentions")
    f.add_argument("--not-before", dest="not_before", type=float, default=0.0)
    f.add_argument("--window", default=None)


def run(a):
    ws = _workspace()
    if a.nav_cmd == "to":
        owner_dm = a.owner_dm or owner_dm_room(ws, _gw.gateway()[0])
        if not owner_dm:
            return {"ok": False, "reason": "no owner DM reading for this gateway; pass --owner-dm"}
        args = build_input(a.room_id, a.view, a.event_id, a.thread_id, a.page, a.reason)
        return navigate(owner_dm, args)
    if a.nav_cmd == "mention":
        try:
            text = Path(a.task_file).read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "reason": f"cannot read --task-file: {exc}"}
        return on_mention(mention_from_task(text, ws), ws, window=window_seconds(a.window))
    delay = a.not_before - time.time()
    if delay > 0:
        time.sleep(delay)
    return flush(ws, window=window_seconds(a.window))

