#!/usr/bin/env python3
"""owner-agent-consult policy: config, the trusted-task gate, the owner-only room guard,
roster, plain asks and answers in a consult thread, the loop check and thread limits, the
pending records and claims, and matching an answer to its consult. Correlation is by Matrix
relations and this agent's own records only; no message carries anything but its text. Pure
over an injected transport; consult.py supplies the room CLI transport.

The transport is any object with:
  members(room) -> {ok, members: [{user_id, display_name, kind}], unidentified: int, reason}
  agents()      -> {ok, agents: [{id, owner, ...}], reason}   (this account's agents)
  mention(mxid, body, room, reply_to=None, thread_root=None) -> {ok, event_id, reason}
  read(room, limit) -> {ok, messages: [{sender, body, event_id, ts, in_reply_to?,
                        thread_root?}] newest first, reason}

Identity is never configured here: the self mxid comes from the caller, the owner from
the gateway's agent registry, and the roster from the consult room's membership.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

SKILL_DIR = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
_SRC = SKILL_DIR.parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

CONFIG_ENABLED = "OWNER_AGENT_CONSULT_ENABLED"
CONFIG_ROOM = "OWNER_AGENT_CONSULT_ROOM"
CONFIG_ROOM_CLI = "OWNER_AGENT_CONSULT_ROOM_CLI"
CONFIG_NUDGE_AFTER = "OWNER_AGENT_CONSULT_NUDGE_AFTER_S"
DEFAULT_NUDGE_AFTER_S = 600
NUDGE_FLOOR_S, NUDGE_CEILING_S = 60, 86400
CONFIG_EXPIRE_AFTER = "OWNER_AGENT_CONSULT_EXPIRE_AFTER_S"
DEFAULT_EXPIRE_AFTER_S = 7 * 86400
EXPIRE_FLOOR_S, EXPIRE_CEILING_S = 3600, 90 * 86400
CONFIG_MAX_DURATION = "OWNER_AGENT_CONSULT_MAX_DURATION_S"
DEFAULT_MAX_DURATION_S = 1800
MAX_DURATION_FLOOR_S, MAX_DURATION_CEILING_S = 60, 86400
CONFIG_MAX_ASKS = "OWNER_AGENT_CONSULT_MAX_ASKS"
DEFAULT_MAX_ASKS = 10
MAX_ASKS_FLOOR, MAX_ASKS_CEILING = 1, 100
READ_LIMIT = 100
MAX_TASK_BYTES = 256 * 1024
_FALSE = ("0", "false", "no", "off")
_MXID = re.compile(r"^@[^:\s]+:[^\s]+$")
_CID = re.compile(r"^[0-9a-f]{16}$")
_STATE = Path("state") / "owner-agent-consult"
_ORIGIN_KEYS = ("source", "channel_id", "source_room_id", "source_message_id", "thread_root")


def manifest_config(manifest: Optional[Path] = None) -> dict:
    try:
        data = json.loads((manifest or SKILL_DIR / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cfg = data.get("config") if isinstance(data, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def config_value(key: str, cli=None, environ=None, manifest_cfg=None) -> str:
    """CLI > env > manifest config (skills/MANIFEST.md); an empty value counts as unset."""
    if cli is not None and str(cli).strip():
        return str(cli).strip()
    env = os.environ if environ is None else environ
    if str(env.get(key) or "").strip():
        return str(env[key]).strip()
    cfg = manifest_config() if manifest_cfg is None else manifest_cfg
    return str(cfg.get(key) or "").strip()


def _seconds(raw: str, default: int, floor: int, ceiling: int) -> int:
    try:
        value = int(float(raw or default))
    except ValueError:
        value = default
    return max(floor, min(value, ceiling))


def settings(room=None, nudge_after=None, room_cli=None, environ=None, manifest_cfg=None,
             expire_after=None, max_duration=None, max_asks=None) -> dict:
    """{active, room, room_cli, nudge_after_s, expire_after_s, max_duration_s, max_asks, reason};
    inert when disabled, or when no room or no room transport is configured."""
    get = lambda k, c=None: config_value(k, c, environ, manifest_cfg)  # noqa: E731
    nudge = _seconds(get(CONFIG_NUDGE_AFTER, nudge_after), DEFAULT_NUDGE_AFTER_S, NUDGE_FLOOR_S, NUDGE_CEILING_S)
    expire = _seconds(get(CONFIG_EXPIRE_AFTER, expire_after), DEFAULT_EXPIRE_AFTER_S,
                      EXPIRE_FLOOR_S, EXPIRE_CEILING_S)
    out = {"active": False, "room": get(CONFIG_ROOM, room), "room_cli": get(CONFIG_ROOM_CLI, room_cli),
           "nudge_after_s": nudge, "expire_after_s": expire,
           "max_duration_s": _seconds(get(CONFIG_MAX_DURATION, max_duration), DEFAULT_MAX_DURATION_S,
                                      MAX_DURATION_FLOOR_S, MAX_DURATION_CEILING_S),
           "max_asks": _seconds(get(CONFIG_MAX_ASKS, max_asks), DEFAULT_MAX_ASKS,
                                MAX_ASKS_FLOOR, MAX_ASKS_CEILING),
           "reason": None}
    if get(CONFIG_ENABLED).lower() in _FALSE:
        out["reason"] = f"disabled ({CONFIG_ENABLED} is off); the skill is inert"
    elif not out["room"]:
        out["reason"] = f"no consult room configured ({CONFIG_ROOM} unset); the skill is inert"
    elif not out["room_cli"]:
        out["reason"] = f"no room transport configured ({CONFIG_ROOM_CLI} unset); the skill is inert"
    else:
        out["active"] = True
    return out


# Owner, registry and the owner-only guard
def owner_and_agents(self_mxid: str, agents: List[dict]) -> dict:
    """The owner is the registry's `owner` on this agent's own row; the owner's agents
    are the registry rows naming that same owner. No row for self -> no owner."""
    rows = [a for a in agents or [] if isinstance(a, dict)]
    me = next((a for a in rows if str(a.get("id") or "") == self_mxid), None)
    owner = str((me or {}).get("owner") or "").strip()
    if not _MXID.match(owner):
        return {"owner": "", "agents": set(),
                "reason": "the agent registry names no owner for this agent"}
    owned = {str(a.get("id")) for a in rows if str(a.get("owner") or "").strip() == owner
             and _MXID.match(str(a.get("id") or ""))}
    return {"owner": owner, "agents": owned, "reason": None}


def _count(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def guard(room: str, self_mxid: str, transport) -> dict:
    """Fail closed. Admit the room only when every joined member is accounted for and is
    this agent, the owner, or an agent the registry binds to the same owner."""
    if not self_mxid or not _MXID.match(self_mxid):
        return {"ok": False, "reason": "this agent's own mxid is unknown (pass --agent or set AGENT_MXID)"}
    reg = transport.agents()
    if not reg.get("ok"):
        return {"ok": False, "reason": f"agent registry unreadable: {reg.get('reason')}"}
    who = owner_and_agents(self_mxid, reg.get("agents") or [])
    if not who["owner"]:
        return {"ok": False, "reason": who["reason"]}
    got = transport.members(room)
    if not got.get("ok"):
        return {"ok": False, "reason": f"consult room members unreadable: {got.get('reason')}"}
    unidentified = _count(got.get("unidentified"))
    if unidentified is None:
        return {"ok": False, "reason": "the room transport did not account for every member"}
    if unidentified:
        return {"ok": False, "reason": f"consult room has {unidentified} member(s) with no identity; "
                                       "cannot prove it owner-only"}
    rows = got.get("members")
    if not isinstance(rows, list):
        return {"ok": False, "reason": "consult room member list is malformed"}
    bad = [m for m in rows if not isinstance(m, dict) or not _MXID.match(str(m.get("user_id") or ""))]
    if bad:
        return {"ok": False, "reason": f"consult room has {len(bad)} member row(s) with no valid identity; "
                                       "cannot prove it owner-only"}
    ids = {str(m["user_id"]) for m in rows}
    if self_mxid not in ids:
        return {"ok": False, "reason": "this agent is not a member of the consult room"}
    strangers = []
    for m in rows:
        uid = str(m["user_id"])
        if uid in (self_mxid, who["owner"]) or uid in who["agents"]:
            continue
        what = "agent of another owner" if m.get("kind") == "agent" else "human who is not the owner"
        strangers.append(f"{uid} ({what})")
    if strangers:
        return {"ok": False, "reason": "consult room is not owner-only: " + ", ".join(sorted(strangers))}
    return {"ok": True, "owner": who["owner"], "owner_agents": who["agents"],
            "members": rows, "reason": None}


def roster(verdict: dict, self_mxid: str) -> List[dict]:
    """The owner's other agents present in the consult room (agent kind per the registry,
    not the naming heuristic)."""
    out = []
    for m in verdict.get("members") or []:
        uid = str(m.get("user_id"))
        if uid == self_mxid or uid not in verdict.get("owner_agents", ()):
            continue
        out.append({"mxid": uid, "display_name": m.get("display_name") or ""})
    return out


# Trusted task gate
def _header_values(text: str, key: str) -> List[str]:
    prefix = f"{key}:"
    return [ln[len(prefix):].strip() for ln in text.split("\n") if ln.startswith(prefix)]


def live_task(task_id: str, workspace: Optional[Path]) -> dict:
    """{ok, text, headers} for the live, envelope-verified task named by id in this
    workspace's inbox. A caller-supplied path or text is never the authority."""
    import local_task_protocol as ltp
    from task_archive import find_task_file
    from task_envelope import attested_task_headers, verify_text

    def no(reason):
        return {"ok": False, "reason": reason}
    if workspace is None:
        return no("workspace unknown")
    if not ltp.valid_task_id(task_id or ""):
        return no(f"{task_id!r} is not a task id")
    tasks = Path(workspace) / "tasks"
    path = find_task_file(tasks, task_id)
    if (path is None or not path.name.endswith(".txt") or path.is_symlink()
            or path.resolve().parent != tasks.resolve()):
        return no(f"{task_id} is not a live task in this workspace")
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_TASK_BYTES + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return no(f"task unreadable: {e}")
    if len(raw) > MAX_TASK_BYTES or "\x00" in text:
        return no("task file is oversized or malformed")
    verdict = verify_text(text, Path(workspace)).get("verdict")
    if verdict != "verified":
        return no(f"task envelope is {verdict}; only a verified envelope establishes its origin")
    headers = attested_task_headers(text, Path(workspace)).headers
    if headers.get("id") != task_id:
        return no("task does not carry its own id")
    try:
        arrived = path.stat().st_mtime
    except OSError:
        arrived = None
    return {"ok": True, "text": text, "headers": headers, "arrived_at": arrived, "reason": None}


def trusted_task(task_id: str, workspace: Optional[Path]) -> dict:
    """The live, verified, unanswered owner task named by id; anything unprovable fails closed."""
    import local_task_protocol as ltp
    got = live_task(task_id, workspace)
    if not got["ok"]:
        return got
    text, headers = got["text"], got["headers"]

    def no(reason):
        return {"ok": False, "reason": reason}
    tier = ltp.canonical_access_tier(headers.get("access_tier"))
    tiers = {ltp.canonical_access_tier(v) for v in _header_values(text, "access_tier")}
    if tier != "owner" or tiers != {"owner"}:
        return no(f"task access_tier is {tier or 'absent'!r}, not owner")
    if any(v.lower() == "true" for v in _header_values(text, "collaborator")):
        return no("task is a collaborator task, not the owner's")
    if ltp.find_result(Path(workspace) / "results", task_id) is not None:
        return no("task already has a result; a replayed task cannot consult")
    origin = {k: headers[k] for k in _ORIGIN_KEYS if headers.get(k)}
    return {"ok": True, "origin": origin, "sender": headers.get("user_id") or "", "reason": None}


# Pending consult records: <workspace>/state/owner-agent-consult/{pending,answered}/<cid>.json
def _record_path(workspace: Path, state: str, cid: str) -> Path:
    return Path(workspace) / _STATE / state / f"{cid}.json"


def _write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_record(path: Path) -> Optional[dict]:
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def records(workspace: Path, state: str) -> List[dict]:
    d = Path(workspace) / _STATE / state
    out = [_read_record(p) for p in sorted(d.glob("*.json"))] if d.is_dir() else []
    return [r for r in out if r and _CID.match(str(r.get("cid") or ""))]


def _room_messages(transport, room: str) -> List[dict]:
    """The latest room read. It is never proof that something is absent: the gateway cannot
    say a read covered the whole history."""
    page = transport.read(room, READ_LIMIT)
    return [m for m in page.get("messages") or [] if isinstance(m, dict)] if page.get("ok") else []


def _sent_before_publication(sent: dict) -> bool:
    """True only for a refusal the transport reports as made before anything was sent. A
    timeout, an output that is not JSON, an HTTP error or a missing event id is ambiguous."""
    return not sent.get("ok") and sent.get("posted") is False


def reconcile(transport, room: str, self_mxid: str, workspace: Path, *, task_id: Optional[str] = None,
              via_task: Optional[str] = None, agent: Optional[str] = None,
              expire_after_s: float = DEFAULT_EXPIRE_AFTER_S, now: Optional[float] = None) -> Optional[str]:
    """Settle every earlier ask from this owner task (or from this ask task to this agent) whose
    post was not confirmed: a failed write, a crash after the post, or an ambiguous transport
    outcome. Found in the room (this agent's own message, same thread, same text): the record
    is completed. Otherwise it stays
    unconfirmed, whatever the read shows and however old it is, and this returns a refusal:
    nothing new is posted from this task while it may already be in the room."""
    now = time.time() if now is None else now
    loose = [(st, r) for st in ("pending", "answered") for r in records(workspace, st)
             if not (r.get("ask_event") and r.get("root"))
             and ((task_id and r.get("task_id") == task_id)
                  or (via_task and r.get("via_task") == via_task and r.get("agent") == agent))]
    if not loose:
        return None
    messages = _room_messages(transport, room)
    used = {r.get("ask_event") for st in ("pending", "answered") for r in records(workspace, st)} - {None}
    unresolved = []
    for st, r in loose:
        fixed = _recover_ask(r, messages, self_mxid, used)
        if fixed.get("ask_event"):
            try:
                _write_record(_record_path(workspace, st, r["cid"]), fixed)
                continue
            except OSError:
                unresolved.append(r["cid"])
                continue
        unresolved.append(r["cid"])
    if unresolved:
        return (f"an earlier ask is unconfirmed ({', '.join(unresolved)}): it may already be in the room, "
                "so nothing new is posted from this task; `pending` lists it, and it clears only when "
                "found. Tell the owner it is unconfirmed; the owner can ask again with a new message")
    return None


def _root_claim(workspace: Path, task_id: str) -> Path:
    return Path(workspace) / _STATE / "roots" / f"{task_id}.json"


def claim_root(workspace: Path, task_id: str, cid: str) -> bool:
    """The one durable claim that this task's thread root is `cid`'s ask; False when taken."""
    return _claim_file(_root_claim(workspace, task_id), {"cid": cid, "task_id": task_id, "asked_at": time.time()})


def prior_ask(workspace: Path, key: str, value: str) -> Optional[dict]:
    """The first earlier posted ask whose `key` (task_id, or via_task) is `value`."""
    same = [r for state in ("pending", "answered") for r in records(workspace, state)
            if r.get(key) == value and r.get("ask_event") and r.get("root")]
    return min(same, key=lambda r: r.get("asked_at") or 0) if same else None


FUTURE_TOLERANCE_S = 300


def _age(rec: dict, now: float) -> float:
    """Seconds since the ask; a record stamped well in the future reads as infinitely old,
    so it expires instead of living forever."""
    age = now - float(rec.get("asked_at") or now)
    return float("inf") if age < -FUTURE_TOLERANCE_S else max(0.0, age)


def pending(workspace: Path, nudge_after_s: float, now: Optional[float] = None,
            expire_after_s: float = DEFAULT_EXPIRE_AFTER_S) -> List[dict]:
    """Unanswered consults, oldest first; `nudge_due` when overdue and the owner was not yet
    told, `expired` past the expiry (match then refuses), plus any live task holding a root
    claim with no record (`claim_only`). Root and answer claims are never pruned; an expired
    consult's record is pruned once the owner has been told and its task can no longer post."""
    now = time.time() if now is None else now
    roots = Path(workspace) / _STATE / "roots"
    claimed = {}  # root and answer claims are never removed: a removed claim can be raced
    for f in sorted(roots.glob("*.json")) if roots.is_dir() else []:
        task = f.name[:-len(".json")]
        if task_can_post(workspace, task):
            claimed[task] = (_read_record(f) or {}).get("cid")
    for state in ("answered", "nudged"):
        for r in records(workspace, state):
            orphan = state == "nudged" and not _record_path(workspace, "pending", r["cid"]).exists()
            if (state == "answered" or orphan) and _age(r, now) > expire_after_s:
                _record_path(workspace, state, r["cid"]).unlink(missing_ok=True)
    out = []
    listed_tasks = set()
    for r in sorted(records(workspace, "pending"), key=lambda r: r.get("asked_at") or 0):
        if _record_path(workspace, "answered", r["cid"]).exists():
            _record_path(workspace, "pending", r["cid"]).unlink(missing_ok=True)
            continue
        age = _age(r, now)
        overdue = age >= nudge_after_s
        nudged = _record_path(workspace, "nudged", r["cid"]).exists()
        owner_task = r.get("task_id") or r.get("via_task")
        if nudged and age > expire_after_s and not task_can_post(workspace, owner_task):
            _record_path(workspace, "pending", r["cid"]).unlink(missing_ok=True)
            _record_path(workspace, "nudged", r["cid"]).unlink(missing_ok=True)
            continue
        listed_tasks.add(r.get("task_id"))
        out.append({**r, "age_s": int(age) if age != float("inf") else None, "overdue": overdue, "nudged": nudged,
                    "unconfirmed": not r.get("ask_event"),
                    "nudge_due": overdue and not nudged, "expired": age > expire_after_s})
    for task, cid in sorted(claimed.items()):
        if task not in listed_tasks and not prior_ask(workspace, "task_id", task):
            out.append({"task_id": task, "cid": cid, "claim_only": True, "unconfirmed": True,
                        "reason": "a root claim with no posted ask: this task cannot consult again; "
                                  "the owner can ask again with a new message"})
    return out


def mark_nudged(workspace: Path, cid: str, now: Optional[float] = None) -> dict:
    """Record that the owner was told, in its own file: the pending record is never rewritten
    here, so a nudge cannot bring back a consult a match has closed."""
    if not _CID.match(cid or ""):
        return {"ok": False, "reason": "malformed correlation id"}
    rec = _read_record(_record_path(workspace, "pending", cid))
    if rec is None:
        return {"ok": False, "reason": f"no pending consult {cid}"}
    _write_record(_record_path(workspace, "nudged", cid),
                  {"cid": cid, "asked_at": rec.get("asked_at"), "nudged_at": time.time() if now is None else now})
    return {"ok": True, "cid": cid, "reason": None}


# Asks and answers: plain messages, correlated only by Matrix relations and this agent's records
def new_cid() -> str:
    return secrets.token_hex(8)


def _ts_seconds(ts) -> Optional[float]:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return float(ts) / 1000.0 if ts > 1e11 else float(ts)


def effective_limits(root_msg: dict, own: Optional[dict], max_duration_s: int, max_asks: int) -> Optional[dict]:
    """This agent's limits on a thread: the window starts at the root event's server timestamp,
    and each limit is the smaller of the one recorded with this agent's first ask in the thread
    and this agent's own config. None when the root event carries no usable timestamp."""
    since = _ts_seconds((root_msg or {}).get("ts"))
    if since is None:
        return None
    own = own or {}
    return {"since": since,
            "max_s": min(int(own.get("max_s", max_duration_s)), int(max_duration_s)),
            "max_asks": min(int(own.get("max_asks", max_asks)), int(max_asks))}


def limit_reached(limits: dict, asks_so_far: int, now: float) -> Optional[str]:
    """Why the thread takes no new ask from this agent, or None; out-of-range values are clamped."""
    max_s = max(MAX_DURATION_FLOOR_S, min(int(limits["max_s"]), MAX_DURATION_CEILING_S))
    max_asks = max(MAX_ASKS_FLOOR, min(int(limits["max_asks"]), MAX_ASKS_CEILING))
    if now >= limits["since"] + max_s:
        return f"consult limit reached: the thread's {max_s}s window has passed"
    if asks_so_far >= max_asks:
        return f"consult limit reached: this agent has made {asks_so_far} of {max_asks} asks in the thread"
    return None


def thread_view(transport, room: str, root: str) -> dict:
    """{ok, root_msg, msgs (oldest first), all (newest first), server_now} for the consult thread
    rooted at `root`, or a refusal when the root is not in the read: an unseen thread cannot be
    checked. `server_now` is the newest server timestamp among the thread's own events."""
    page = transport.read(room, READ_LIMIT)
    if not page.get("ok"):
        return {"ok": False, "reason": f"consult room unreadable: {page.get('reason')}"}
    msgs = [m for m in page.get("messages") or [] if isinstance(m, dict)]
    root_msg = next((m for m in msgs if m.get("event_id") == root), None)
    if root_msg is None:
        return {"ok": False, "reason": f"consult thread root {root} is not in the last {READ_LIMIT} "
                                       "messages of the consult room; cannot see the whole thread"}
    inside = [m for m in msgs if m.get("event_id") == root or m.get("thread_root") == root]
    return {"ok": True, "root_msg": root_msg, "msgs": list(reversed(inside)), "all": msgs,
            "server_now": server_now(inside)}


CLOCK_TOLERANCE_S = 120


def measured_now(server_now_s: Optional[float], anchor: tuple, local_now: float) -> Optional[float]:
    """An estimate of the server's current time for the thread window (see SKILL.md
    "Time-window invariant"): the largest of the thread's newest server timestamp, the anchor's
    server timestamp plus local time elapsed since it, and the local clock. None (refuse) when
    no server timestamp is known, or the local clock is more than CLOCK_TOLERANCE_S behind it."""
    if server_now_s is None or local_now < server_now_s - CLOCK_TOLERANCE_S:
        return None
    terms = [server_now_s, local_now]
    a_server, a_local = anchor if anchor else (None, None)
    if a_server is not None and a_local is not None:
        terms.append(a_server + max(0.0, local_now - a_local))
    return max(terms)


def server_now(messages: List[dict]) -> Optional[float]:
    """The newest server timestamp among `messages`, in seconds; None when no event has one."""
    stamps = [t for t in (_ts_seconds(m.get("ts")) for m in messages) if t is not None]
    return max(stamps) if stamps else None


def own_asks(workspace: Path, root: Optional[str]) -> List[dict]:
    """This agent's posted asks in the thread rooted at `root`, oldest first (its own records)."""
    if not root:
        return []
    mine = [r for st in ("pending", "answered") for r in records(workspace, st)
            if r.get("root") == root and r.get("ask_event")]
    return sorted(mine, key=lambda r: r.get("asked_at") or 0)


def loop_check(thread_msgs: List[dict], mine: List[dict], target: str) -> Optional[str]:
    """Refusal when `target` has already posted in this consult thread and this agent never
    asked it there: it is already part of the consult (it asked, or was asked by someone
    else). A follow-up to an agent this agent asked is allowed. Structural: senders only."""
    posted = {m.get("sender") for m in thread_msgs}
    if target in posted and target not in {r.get("agent") for r in mine}:
        return (f"{target} has already taken part in this consult thread; answer from what the "
                "thread has instead of asking it")
    return None


def incoming_ask(transport, room: str, self_mxid: str, task_id: str, workspace: Optional[Path],
                 owner_agents, max_duration_s: int = DEFAULT_MAX_DURATION_S) -> dict:
    """The consult ask a live verified task delivered to this agent: the gateway delivered it
    because the message mentions this agent. The message must be from another of the owner's
    agents, in the consult room, in (or rooting) a thread whose root is an owner's agent's
    message, and posted within this agent's window for that thread."""
    def no(reason):
        return {"ok": False, "reason": reason}
    import local_task_protocol as ltp
    got = live_task(task_id, workspace)
    if not got["ok"]:
        return no(got["reason"])
    h = got["headers"]
    if room not in (h.get("channel_id"), h.get("source_room_id")):
        return no("task did not come from the consult room")
    if ltp.find_result(Path(workspace) / "results", task_id) is not None:
        return no("the ask's task already has a result; a closed ask cannot be answered or consulted from")
    event = h.get("source_message_id")
    page = transport.read(room, READ_LIMIT)
    if not page.get("ok"):
        return no(f"consult room unreadable: {page.get('reason')}")
    msg = next((m for m in page.get("messages") or [] if isinstance(m, dict)
                and event and m.get("event_id") == event), None)
    if msg is None:
        return no(f"ask event not in the last {READ_LIMIT} messages of the consult room")
    sender = msg.get("sender")
    if sender == self_mxid or sender not in owner_agents:
        return no("the ask is not from another of the owner's agents")
    if not _mentions_first(msg.get("body") or "", self_mxid):
        return no("the message is not addressed to this agent")
    root = msg.get("thread_root") or event
    view = thread_view(transport, room, root)
    if not view["ok"]:
        return no(view["reason"])
    if view["root_msg"].get("sender") not in owner_agents:
        return no("the consult thread's root is not from one of the owner's agents")
    window = effective_limits(view["root_msg"], None, max_duration_s, MAX_ASKS_CEILING)
    posted = _ts_seconds(msg.get("ts"))
    if window is None or posted is None:
        return no("no server timestamp on the thread root or the ask")
    late = limit_reached(window, 0, posted)
    if late:
        return no(f"this ask was posted outside the thread's window ({late.replace('consult limit reached: ', '')}); "
                  "it is not answered or consulted from")
    return {"ok": True, "root": root, "asker": sender, "ask_event": event, "root_msg": view["root_msg"],
            "msgs": view["msgs"], "server_now": view["server_now"],
            "anchor": (posted, got["arrived_at"]), "reason": None}


def consult(transport, *, room: str, self_mxid: str, agent: str, question: str,
            workspace: Optional[Path], task_id: Optional[str] = None, via_task: Optional[str] = None,
            cid: Optional[str] = None, now: Optional[Callable[[], float]] = None,
            max_duration_s: int = DEFAULT_MAX_DURATION_S, max_asks: int = DEFAULT_MAX_ASKS,
            expire_after_s: float = DEFAULT_EXPIRE_AFTER_S) -> dict:
    """Every gate, then the one post (a plain message mentioning `agent`, in the consult thread)
    and its pending record; returns at once with {asked, agent, cid, ask_event, root, limits,
    follow_up} or {asked: False, reason}. Nothing is posted unless all gates pass."""
    def no(reason, **extra):
        return {"asked": False, "agent": agent, "reason": reason, **extra}
    clock = now or time.time
    cid = cid or new_cid()
    if not _CID.match(cid):
        return no("malformed correlation id")
    if bool(task_id) == bool(via_task):
        return no("name exactly one of an owner task (--task-id) or a consult ask (--via-task)")
    origin, via, gate = {}, None, None
    if task_id:
        gate = trusted_task(task_id, workspace)
        if not gate["ok"]:
            return no(gate["reason"])
        origin = gate["origin"]
    if not question or not question.strip():
        return no("empty question")
    verdict = guard(room, self_mxid, transport)
    if not verdict["ok"]:
        return no(verdict["reason"])
    if agent not in {r["mxid"] for r in roster(verdict, self_mxid)}:
        return no(f"{agent} is not one of the owner's other agents in the consult room")
    if task_id and room in (origin.get("channel_id"), origin.get("source_room_id")) \
            and gate["sender"] != verdict["owner"]:
        return no("an owner task from the consult room must be the owner's own message")
    owner_agents = verdict["owner_agents"] | {self_mxid}
    own = {"max_s": int(max_duration_s), "max_asks": int(max_asks)}
    if task_id:
        refusal = reconcile(transport, room, self_mxid, workspace, task_id=task_id)
        if refusal:
            return no(refusal)
        prior = prior_ask(workspace, "task_id", task_id)
        root = prior["root"] if prior else None
        msgs, root_msg, now_s = [], None, clock()  # a first ask opens its own window
        if root:
            view = thread_view(transport, room, root)
            if not view["ok"]:
                return no(view["reason"])
            msgs, root_msg = view["msgs"], view["root_msg"]
            now_s = measured_now(view["server_now"], (_ts_seconds(root_msg.get("ts")), prior.get("asked_at")),
                                 clock())
    else:
        inc = incoming_ask(transport, room, self_mxid, via_task, workspace, owner_agents, max_duration_s)
        if not inc["ok"]:
            return no(inc["reason"])
        refusal = reconcile(transport, room, self_mxid, workspace, via_task=via_task, agent=agent)
        if refusal:
            return no(refusal)
        root, msgs, root_msg = inc["root"], inc["msgs"], inc["root_msg"]
        now_s = measured_now(inc["server_now"], inc["anchor"], clock())
        via = {"task_id": via_task, "asker": inc["asker"], "ask_event": inc["ask_event"]}
    mine = own_asks(workspace, root)
    refusal = loop_check(msgs, mine, agent)
    if refusal:
        return no(refusal)
    if root:
        first_limits = (mine[0].get("limits") if mine else None) or own
        limits = effective_limits(root_msg, first_limits, max_duration_s, max_asks)
        if limits is None:
            return no("the consult thread's root event has no server timestamp; cannot apply the thread limits")
        if now_s is None:
            return no("cannot measure the thread window on server time: this agent's clock is more than "
                      f"{CLOCK_TOLERANCE_S}s behind the consult thread's newest server timestamp")
        refusal = limit_reached(limits, len(mine), now_s)
        if refusal:
            return no(refusal, limit_reached=True)
    if _record_path(workspace, "pending", cid).exists() or _record_path(workspace, "answered", cid).exists():
        return no("correlation id already used")
    follow_up = agent in {r.get("agent") for r in mine}
    path = _record_path(workspace, "pending", cid)
    new_root = bool(task_id) and not root
    if new_root and not claim_root(workspace, task_id, cid):
        return no("another ask from this task holds its thread root and has no confirmed post; nothing "
                  "new is posted from this task. The owner can ask again with a new message")

    def release():
        if new_root:
            _root_claim(workspace, task_id).unlink(missing_ok=True)
    body = question.strip()
    record = {"cid": cid, "task_id": task_id, "via_task": via_task, "via": via, "agent": agent,
              "room": room, "root": root, "ask_event": None, "asked_at": clock(), "origin": origin,
              "body": body, "limits": own, "boundary": _boundary(transport, room)}
    try:
        _write_record(path, record)
    except OSError as e:
        release()
        return no(f"pending record unwritable: {e}")
    sent = transport.mention(agent, body, room, reply_to=via["ask_event"] if via else None, thread_root=root)
    if _sent_before_publication(sent):
        path.unlink(missing_ok=True)
        release()
        return no(f"ask not posted: {sent.get('reason') or 'refused'}")
    event = sent.get("event_id") if sent.get("ok") else None
    if not event:
        return {"asked": True, "unconfirmed": True, "agent": agent, "cid": cid, "ask_event": None,
                "root": root, "limits": own, "follow_up": follow_up,
                "warning": f"the post may have landed ({sent.get('reason') or 'no event id'}); the record "
                           "and any root claim are kept, and `match` or the next ask reconciles it from "
                           "the room",
                "reason": None}
    record.update(ask_event=event, root=root or event)
    warning = None
    try:
        if not _record_path(workspace, "answered", cid).exists():
            _write_record(path, record)
    except OSError as e:
        warning = f"ask posted but its event id was not recorded ({e}); match recovers it from the room"
    return {"asked": True, "agent": agent, "cid": cid, "ask_event": event, "root": record["root"],
            "limits": own, "follow_up": follow_up, "warning": warning, "reason": None}


_MENTION_SEPARATORS = (" \u2014 ", ": ", " ")


def _strip_mention(body: str, mxid: str) -> str:
    """The body without a leading mention of `mxid` and its one separator (the room CLI writes
    `<mxid> — <text>`); the text itself is left exactly as written."""
    text = (body or "").strip()
    if mxid and text.startswith(mxid):
        rest = text[len(mxid):]
        for sep in _MENTION_SEPARATORS:
            if rest.startswith(sep):
                return rest[len(sep):]
        return rest.strip()
    return text


def _mentions_first(body: str, mxid: str) -> bool:
    return bool(mxid) and (body or "").strip().startswith(mxid)


def _boundary(transport, room: str) -> Optional[dict]:
    """The room as this agent saw it just before posting: the newest server timestamp and the
    events carrying it. A post can only be newer. None when the room could not be read."""
    page = transport.read(room, READ_LIMIT)
    if not page.get("ok"):
        return None
    msgs = [m for m in page.get("messages") or [] if isinstance(m, dict)]
    newest = server_now(msgs) or 0.0
    return {"ts": newest, "ids": [m.get("event_id") for m in msgs if _ts_seconds(m.get("ts")) == newest]}


def _recover_ask(rec: dict, messages: List[dict], self_mxid: str, used=frozenset()) -> dict:
    """A record whose post was not recorded gets its ask event (and root) back only when exactly
    one message can be it: this agent's own, not bound to another record, posted after the room
    boundary recorded before the send, in the expected thread (or a new root), with the exact
    text it posted. No boundary, no match, or more than one: it stays unconfirmed."""
    if rec.get("ask_event"):
        return rec
    bound = rec.get("boundary")
    if not isinstance(bound, dict):
        return rec
    hits = [m for m in messages
            if m.get("sender") == self_mxid and m.get("event_id") not in used
            and m.get("event_id") not in (bound.get("ids") or [])
            and (_ts_seconds(m.get("ts")) or 0.0) >= float(bound.get("ts") or 0.0)
            and (m.get("thread_root") or None) == (rec.get("root") or None)
            and _strip_mention(m.get("body") or "", rec.get("agent") or "") == rec.get("body")]
    if len(hits) != 1:
        return rec
    event = hits[0].get("event_id")
    return {**rec, "ask_event": event, "root": rec.get("root") or event}


def answer(transport, *, room: str, self_mxid: str, text: str, workspace: Optional[Path],
           task_id: Optional[str] = None, up: Optional[str] = None,
           max_duration_s: int = DEFAULT_MAX_DURATION_S, max_asks: int = DEFAULT_MAX_ASKS,
           expire_after_s: float = DEFAULT_EXPIRE_AFTER_S) -> dict:
    """Post this agent's answer to a consult ask, once: a plain message in the thread, replying
    to the ask and mentioning the asker. The ask is the one a live task delivered (`task_id`), or,
    to pass an answer up, the ask an onward consult of this agent's came from (`up`, its id).
    Any agent may instead just reply in the thread; this is the at-most-once path."""
    def no(reason):
        return {"answered": False, "reason": reason}
    if bool(task_id) == bool(up):
        return no("name exactly one of the ask's task (--task-id) or an onward consult (--up)")
    if not text or not text.strip():
        return no("empty answer")
    verdict = guard(room, self_mxid, transport)
    if not verdict["ok"]:
        return no(verdict["reason"])
    if up:
        rec = next((r for st in ("answered", "pending") if _CID.match(up)
                    for r in [_read_record(_record_path(workspace, st, up))] if r), None)
        via = (rec or {}).get("via")
        if not via or not rec.get("root"):
            return no(f"{up} is not an onward consult of this agent")
        inc = {"asker": via["asker"], "ask_event": via["ask_event"], "root": rec["root"]}
        posters = [rec.get("reply_task"), via.get("task_id")]
    else:
        inc = incoming_ask(transport, room, self_mxid, task_id, workspace,
                           verdict["owner_agents"] | {self_mxid}, max_duration_s)
        if not inc["ok"]:
            return no(inc["reason"])
        posters = [task_id]
    reached = None
    if inc.get("root_msg"):
        lim = effective_limits(inc["root_msg"], None, max_duration_s, max_asks)
        now_s = measured_now(inc["server_now"], inc["anchor"], time.time()) if lim else None
        reached = limit_reached(lim, 0, now_s) if lim and now_s is not None else None
    base = {"answered": True, "to": inc["asker"], "ask_event": inc["ask_event"], "root": inc["root"],
            "limit_reached": reached, "reason": None}
    claim_path = _answer_claim(workspace, inc["ask_event"])
    held = _read_record(claim_path)
    if held is not None:
        if not held.get("event_id"):
            found = _own_answer(_room_messages(transport, room), self_mxid, inc["ask_event"])
            if not found:
                return no("an earlier answer to this ask is unconfirmed: it may already be in the room, "
                          "so it is not posted again")
            held["event_id"] = found
            _write_record(claim_path, held)
        _note_tasks(claim_path, held, posters)
        return {**base, "event_id": held["event_id"], "replay": True}
    if not _claim_file(claim_path, {"ask_event": inc["ask_event"], "to": inc["asker"], "at": time.time(),
                                    "event_id": None, "tasks": [t for t in posters if t]}):
        return no("another answer to this ask is being posted")
    sent = transport.mention(inc["asker"], text.strip(), room, reply_to=inc["ask_event"], thread_root=inc["root"])
    if _sent_before_publication(sent):
        claim_path.unlink(missing_ok=True)
        return no(f"answer not posted: {sent.get('reason')}")
    event = sent.get("event_id") if sent.get("ok") else None
    if not event:
        return {**base, "event_id": None, "unconfirmed": True,
                "warning": f"the answer may have landed ({sent.get('reason') or 'no event id'}); a retry "
                           "reconciles it from the room instead of posting again"}
    try:
        _write_record(claim_path, {**_read_record(claim_path), "event_id": event})
    except (OSError, TypeError):
        pass
    return {**base, "event_id": event}


def _note_tasks(path: Path, claim: dict, tasks: List[Optional[str]]) -> None:
    """Record tasks seen posting or replaying this answer (the reply task and the original ask task).
    Informational only: the claim is never deleted, and this list authorizes nothing."""
    new = [t for t in tasks if t and t not in claim.get("tasks", [])]
    if new:
        try:
            _write_record(path, {**claim, "tasks": [*claim.get("tasks", []), *new]})
        except OSError:
            pass


def task_can_post(workspace: Path, task_id: Optional[str]) -> bool:
    """True while `task_id` is live in this inbox with no result."""
    import local_task_protocol as ltp
    from task_archive import find_task_file
    if not task_id or not ltp.valid_task_id(task_id):
        return False
    if find_task_file(Path(workspace) / "tasks", task_id) is None:
        return False
    return ltp.find_result(Path(workspace) / "results", task_id) is None


def _answer_claim(workspace: Path, ask_event: str) -> Path:
    import hashlib
    key = hashlib.sha256(str(ask_event).encode("utf-8")).hexdigest()[:16]
    return Path(workspace) / _STATE / "answers" / f"{key}.json"


def _claim_file(path: Path, data: dict) -> bool:
    """Create `path` holding `data`, whole or not at all; False when it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)
    return True


def _own_answer(messages: List[dict], self_mxid: str, ask_event: str) -> Optional[str]:
    """The event id of this agent's own reply to `ask_event` in the room read, if any."""
    for m in messages:
        if m.get("sender") == self_mxid and m.get("in_reply_to") == ask_event:
            return m.get("event_id")
    return None


def _bind_path(workspace: Path, event: str) -> Path:
    import hashlib
    key = hashlib.sha256(str(event).encode("utf-8")).hexdigest()[:16]
    return Path(workspace) / _STATE / "bound" / f"{key}.json"


def _candidates(rec: dict, messages: List[dict], workspace: Optional[Path] = None) -> List[dict]:
    """Messages from the asked agent in the consult's thread (or citing its ask) after its ask,
    oldest first, minus events already bound (as an answer or as not final). `messages` is a
    read, newest first."""
    ask = rec.get("ask_event")
    out = []
    for m in messages:
        if m.get("event_id") == ask:
            break
        in_consult = m.get("thread_root") == rec.get("root") or m.get("in_reply_to") == ask
        if (m.get("sender") == rec.get("agent") and in_consult
                and not (workspace and _bind_path(workspace, m.get("event_id")).exists())):
            out.append(m)
    return list(reversed(out))


def match_reply(transport, *, room: str, self_mxid: str, workspace: Optional[Path],
                task_id: Optional[str] = None, event_id: Optional[str] = None, final: bool = False,
                not_final: bool = False, expire_after_s: float = DEFAULT_EXPIRE_AFTER_S,
                now: Optional[float] = None) -> dict:
    """Find the pending consult a message answers: a message from the asked agent, in that
    consult's thread (or citing its ask), after its ask. The message is named by the task it
    arrived as (`task_id`) or by its event (`event_id`). Two phases: with neither flag it only
    returns the candidate (`candidate: true`) and changes nothing; the agent reads it, then
    runs again with `final` (bind it and close the consult; returns where the answer goes) or
    `not_final` (bind it as progress; the agent's next message becomes the candidate). A
    message binds to at most one consult, once: the binding is a claim keyed by its event."""
    def no(reason, **extra):
        return {"matched": False, "reason": reason, **extra}
    if bool(task_id) == bool(event_id):
        return no("name exactly one of the message's task (--task-id) or its event (--event-id)")
    if final and not_final:
        return no("name at most one of --final and --not-final")
    event = event_id
    if task_id:
        got = live_task(task_id, workspace)
        if not got["ok"]:
            return no(got["reason"])
        h = got["headers"]
        if room not in (h.get("channel_id"), h.get("source_room_id")):
            return no("task did not come from the consult room")
        event = h.get("source_message_id")
        if not event:
            return no("task names no source message")
    bind_path = _bind_path(workspace, event)
    bound = _read_record(bind_path)
    if bound is not None and bound.get("disposition") == "progress":
        return no("this message was already judged not final", progress=True, cid=bound.get("cid"))
    if bound is not None and _record_path(workspace, "answered", str(bound.get("cid"))).exists():
        return _replay(workspace, bound["cid"], task_id, event, no)
    page = transport.read(room, READ_LIMIT)
    if not page.get("ok"):
        return no(f"consult room unreadable: {page.get('reason')}")
    messages = [m for m in page.get("messages") or [] if isinstance(m, dict)]
    msg = next((m for m in messages if m.get("event_id") == event), None)
    if msg is None:
        return no(f"message not in the last {READ_LIMIT} messages of the consult room")
    if bound is not None:  # bound as this consult's answer before a crash: finish closing it
        rec = _read_record(_record_path(workspace, "pending", str(bound.get("cid"))))
        if rec is None:
            return no(f"consult {bound.get('cid')} is already answered")
    else:
        used = {r.get("ask_event") for st in ("pending", "answered") for r in records(workspace, st)} - {None}
        open_recs = []
        for r in records(workspace, "pending"):
            r = _recover_ask(r, messages, self_mxid, used)
            if r.get("ask_event") and r.get("room") == room and r.get("agent") == msg.get("sender"):
                open_recs.append(r)
        owners = [r for r in open_recs
                  if any(c.get("event_id") == event for c in _candidates(r, messages, workspace))]
        if not owners:
            return no("not an answer to a pending consult of this agent (wrong sender, another thread, "
                      "or before the ask)")
        cited = [r for r in owners if r.get("ask_event") == msg.get("in_reply_to")]
        rec = (cited or sorted(owners, key=lambda r: r.get("asked_at") or 0))[0]
    cid = rec["cid"]
    if _age(rec, time.time() if now is None else now) > expire_after_s:
        return no(f"consult {cid} expired; tell the owner it was not answered in time")
    text = _strip_mention(msg.get("body") or "", self_mxid)
    if not final and not not_final:
        return {"matched": False, "candidate": True, "agent": rec["agent"], "cid": cid, "reply_text": text,
                "event_id": event, "reason": "read it, then run again with --final (the answer) "
                                              "or --not-final (progress, or an onward ask)"}
    if bound is None:
        try:
            fresh = _claim_file(bind_path, {"event": event, "cid": cid,
                                            "disposition": "answer" if final else "progress", "at": time.time()})
        except OSError as e:
            return no(f"message could not be bound: {e}")
        if not fresh:
            return no("this message was bound by another match just now; run it again")
    if not_final:
        return no("recorded as not final; the asked agent's next message in the thread is the candidate",
                  progress=True, cid=cid)
    out = {"matched": True, "agent": rec["agent"], "cid": cid, "reply_text": text, "event_id": event,
           "root": rec["root"], "question": rec.get("body") or "", "reason": None}
    if rec.get("via"):
        out = {**out, "task_id": None, "answer_up": {**rec["via"], "root": rec["root"], "up": cid}}
    else:
        origin = rec.get("origin") or {}
        out = {**out, "task_id": rec["task_id"], "origin": origin, "lead": reply_lead(origin)}
    src = _record_path(workspace, "pending", cid)
    dst = _record_path(workspace, "answered", cid)
    claim = {**rec, "answered_at": time.time(), "answer_event": event, "reply_task": task_id, "payload": out}
    tmp = dst.with_name(f".{dst.name}.{secrets.token_hex(4)}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(claim, indent=2, sort_keys=True), encoding="utf-8")
        os.link(tmp, dst)  # the claim carries the payload: once linked, a retry can replay it
    except FileExistsError:
        tmp.unlink(missing_ok=True)
        return _replay(workspace, cid, task_id, event, no)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return no(f"consult {cid} could not be closed: {e}")
    tmp.unlink(missing_ok=True)
    src.unlink(missing_ok=True)
    _record_path(workspace, "nudged", cid).unlink(missing_ok=True)
    return out


def _replay(workspace: Path, cid: str, task_id: Optional[str], event: str, no) -> dict:
    """A closed consult's stored payload, again, until it is relayed: for the reply task that
    closed it while that task has no result, or for the same event until `relayed` is recorded."""
    import local_task_protocol as ltp
    rec = _read_record(_record_path(workspace, "answered", cid)) or {}
    if not isinstance(rec.get("payload"), dict) or rec.get("answer_event") != event:
        return no(f"consult {cid} is already answered")
    if task_id and rec.get("reply_task") == task_id \
            and ltp.find_result(Path(workspace) / "results", task_id) is None:
        return {**rec["payload"], "replay": True}
    if not task_id and not rec.get("reply_task") and not rec.get("relayed"):
        return {**rec["payload"], "replay": True}
    return no(f"consult {cid} is already answered")


def mark_relayed(workspace: Path, cid: str) -> dict:
    """Record that a consult matched by event (not by a task) has been relayed: no more replays."""
    path = _record_path(workspace, "answered", cid) if _CID.match(cid or "") else None
    rec = _read_record(path) if path else None
    if rec is None:
        return {"ok": False, "reason": f"no answered consult {cid}"}
    _write_record(path, {**rec, "relayed": True})
    return {"ok": True, "cid": cid, "reason": None}


def waiting_answers(transport, room: str, workspace: Path) -> dict:
    """{cid: {event_id, text}} for pending consults whose asked agent has posted in the thread
    after the ask: answers that did not arrive as a task. One room read."""
    page = transport.read(room, READ_LIMIT)
    messages = [m for m in page.get("messages") or [] if isinstance(m, dict)] if page.get("ok") else []
    out = {}
    for r in records(workspace, "pending"):
        if r.get("ask_event") and r.get("room") == room:
            cands = _candidates(r, messages, workspace)
            if cands:
                out[r["cid"]] = {"event_id": cands[0].get("event_id"), "text": cands[0].get("body") or ""}
    return out


def reply_lead(origin: dict) -> str:
    """Leading marker lines that send a proactive answer to the original task's conversation,
    inside its thread when it had one."""
    room = origin.get("channel_id") or origin.get("source_room_id")
    lead = f"[channel: {room}]\n" if room else ""
    if origin.get("thread_root"):
        lead += f"[thread: {origin['thread_root']}]\n"
    return lead
