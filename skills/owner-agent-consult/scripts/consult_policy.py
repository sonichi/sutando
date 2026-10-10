#!/usr/bin/env python3
"""owner-agent-consult policy: config, the trusted-task gate, the owner-only room guard,
roster, one-hop marker, the correlated ask with its pending record, and matching a reply
(which arrives as a new task) back to that record. Pure over an injected transport, so
the rules are testable without a gateway; consult.py supplies the room CLI transport.

The transport is any object with:
  members(room) -> {ok, members: [{user_id, display_name, kind}], unidentified: int, reason}
  agents()      -> {ok, agents: [{id, owner, ...}], reason}   (this account's agents)
  mention(mxid, body, room, reply_to=None) -> {ok, event_id, reason}
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

# The one definition of the one-hop marker; a task or question carrying it is never consulted onward.
MARKER = "[owner-agent-consult:v1]"
_MARKER_FAMILY = "[owner-agent-consult:"

CONFIG_ENABLED = "OWNER_AGENT_CONSULT_ENABLED"
CONFIG_ROOM = "OWNER_AGENT_CONSULT_ROOM"
CONFIG_ROOM_CLI = "OWNER_AGENT_CONSULT_ROOM_CLI"
CONFIG_NUDGE_AFTER = "OWNER_AGENT_CONSULT_NUDGE_AFTER_S"
DEFAULT_NUDGE_AFTER_S = 600
NUDGE_FLOOR_S, NUDGE_CEILING_S = 60, 86400
READ_LIMIT = 30
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


def settings(room=None, nudge_after=None, room_cli=None, environ=None, manifest_cfg=None) -> dict:
    """{active, room, room_cli, nudge_after_s, reason}; inert when disabled, or when no room
    or no room transport is configured."""
    get = lambda k, c=None: config_value(k, c, environ, manifest_cfg)  # noqa: E731
    try:
        nudge = int(float(get(CONFIG_NUDGE_AFTER, nudge_after) or DEFAULT_NUDGE_AFTER_S))
    except ValueError:
        nudge = DEFAULT_NUDGE_AFTER_S
    nudge = max(NUDGE_FLOOR_S, min(nudge, NUDGE_CEILING_S))
    out = {"active": False, "room": get(CONFIG_ROOM, room), "room_cli": get(CONFIG_ROOM_CLI, room_cli),
           "nudge_after_s": nudge, "reason": None}
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


# Trusted task gate and one-hop marker
def carries_marker(text: str) -> bool:
    return _MARKER_FAMILY in (text or "")


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
    return {"ok": True, "text": text, "headers": headers, "reason": None}


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
    if carries_marker(text):
        return no("task is itself a consult; answer it, never consult onward")
    if ltp.find_result(Path(workspace) / "results", task_id) is not None:
        return no("task already has a result; a replayed task cannot consult")
    origin = {k: headers[k] for k in _ORIGIN_KEYS if headers.get(k)}
    return {"ok": True, "origin": origin, "reason": None}


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


def prior_ask(workspace: Path, task_id: str, agent: str) -> Optional[dict]:
    """The first earlier ask from this task to this agent, pending or answered."""
    same = [r for state in ("pending", "answered") for r in records(workspace, state)
            if r.get("task_id") == task_id and r.get("agent") == agent and r.get("ask_event")]
    return min(same, key=lambda r: r.get("asked_at") or 0) if same else None


def pending(workspace: Path, nudge_after_s: float, now: Optional[float] = None) -> List[dict]:
    """Unanswered consults, oldest first; `nudge_due` when overdue and the owner was not yet told."""
    now = time.time() if now is None else now
    out = []
    for r in sorted(records(workspace, "pending"), key=lambda r: r.get("asked_at") or 0):
        age = max(0.0, now - float(r.get("asked_at") or now))
        overdue = age >= nudge_after_s
        out.append({**r, "age_s": int(age), "overdue": overdue,
                    "nudge_due": overdue and not r.get("nudged_at")})
    return out


def mark_nudged(workspace: Path, cid: str, now: Optional[float] = None) -> dict:
    if not _CID.match(cid or ""):
        return {"ok": False, "reason": "malformed correlation id"}
    path = _record_path(workspace, "pending", cid)
    rec = _read_record(path)
    if rec is None:
        return {"ok": False, "reason": f"no pending consult {cid}"}
    rec["nudged_at"] = time.time() if now is None else now
    _write_record(path, rec)
    return {"ok": True, "cid": cid, "reason": None}


# Correlated ask and its final answer
def new_cid() -> str:
    return secrets.token_hex(8)


def answer_tag(cid: str) -> str:
    return f"{_MARKER_FAMILY}v1 answer:{cid}]"


_TAG = re.compile(r"^\[owner-agent-consult:v1 answer:([0-9a-f]{16})\]$")


def ask_body(question: str, cid: str, self_mxid: str) -> str:
    return (f"{MARKER} consult:{cid}\n{question.strip()}\n\n"
            f"Answer from what you hold in ONE message that replies to this one and @-mentions "
            f"{self_mxid}, with this as its first line (after the mention):\n"
            f"{answer_tag(cid)}\n"
            "Anything without that line is read as progress, not your answer. "
            "This is a one-hop consult: do not consult another agent.")


def consult(transport, *, room: str, self_mxid: str, agent: str, question: str,
            task_id: str, workspace: Optional[Path], cid: Optional[str] = None,
            now: Optional[Callable[[], float]] = None) -> dict:
    """Every gate, then the one post and its pending record; returns at once with
    {asked, agent, cid, ask_event, follow_up} or {asked: False, reason}. The reply arrives
    later as a new task and is matched with match_reply. Nothing is posted unless all gates pass."""
    def no(reason):
        return {"asked": False, "agent": agent, "reason": reason}
    clock = now or time.time
    cid = cid or new_cid()
    if not _CID.match(cid):
        return no("malformed correlation id")
    gate = trusted_task(task_id, workspace)
    if not gate["ok"]:
        return no(gate["reason"])
    if not question or not question.strip():
        return no("empty question")
    if carries_marker(question):
        return no("question carries the consult marker; consults are one hop")
    verdict = guard(room, self_mxid, transport)
    if not verdict["ok"]:
        return no(verdict["reason"])
    if agent not in {r["mxid"] for r in roster(verdict, self_mxid)}:
        return no(f"{agent} is not one of the owner's other agents in the consult room")
    prior = prior_ask(workspace, task_id, agent)
    thread_event = (prior.get("thread_event") or prior.get("ask_event")) if prior else None
    path = _record_path(workspace, "pending", cid)
    if path.exists() or _record_path(workspace, "answered", cid).exists():
        return no("correlation id already used")
    record = {"cid": cid, "task_id": task_id, "agent": agent, "room": room, "ask_event": None,
              "thread_event": thread_event, "asked_at": clock(), "origin": gate["origin"],
              "question": question.strip()[:500]}
    try:
        _write_record(path, record)
    except OSError as e:
        return no(f"pending record unwritable: {e}")
    sent = transport.mention(agent, ask_body(question, cid, self_mxid), room, reply_to=thread_event)
    event = sent.get("event_id") if sent.get("ok") else None
    if not event:
        path.unlink(missing_ok=True)
        return no(f"ask not posted: {sent.get('reason') or 'no event id'}")
    record["ask_event"] = event
    _write_record(path, record)
    return {"asked": True, "agent": agent, "cid": cid, "ask_event": event,
            "follow_up": bool(prior), "reason": None}


def _first_line_tag(body: str, self_mxid: str) -> tuple:
    """(cid, rest) when the body's first line, after an optional leading mention of
    this agent, is an answer tag; else (None, None)."""
    lines = body.strip().split("\n", 1)
    first = lines[0].strip()
    if self_mxid and first.startswith(self_mxid):
        first = first[len(self_mxid):].lstrip(" \u2014\u2013-:")
    m = _TAG.match(first)
    if not m:
        return None, None
    return m.group(1), (lines[1].strip() if len(lines) > 1 else "")


def match_reply(transport, *, room: str, self_mxid: str, task_id: str,
                workspace: Optional[Path]) -> dict:
    """Bind a reply task to its pending consult: the reply must be a verified live task
    from the consult room, whose room event comes from the asked agent, carries that
    consult's answer line, and replies to (or threads under) that consult's ask.
    Returns {matched, task_id (the original), origin, reply_text, ...} or {matched: False, reason}."""
    def no(reason, **extra):
        return {"matched": False, "reason": reason, **extra}
    got = live_task(task_id, workspace)
    if not got["ok"]:
        return no(got["reason"])
    h = got["headers"]
    if room not in (h.get("channel_id"), h.get("source_room_id")):
        return no("task did not come from the consult room")
    event = h.get("source_message_id")
    if not event:
        return no("task names no source message")
    page = transport.read(room, READ_LIMIT)
    if not page.get("ok"):
        return no(f"consult room unreadable: {page.get('reason')}")
    msg = next((m for m in page.get("messages") or [] if isinstance(m, dict)
                and m.get("event_id") == event), None)
    if msg is None or not isinstance(msg.get("body"), str):
        return no(f"reply event not in the last {READ_LIMIT} messages of the consult room")
    cid, text = _first_line_tag(msg["body"], self_mxid)
    if cid is None:
        return no("not a final answer (no answer line); treat it as progress", progress=True)
    rec = _read_record(_record_path(workspace, "pending", cid))
    if rec is None:
        state = "already answered" if _record_path(workspace, "answered", cid).exists() else "unknown"
        return no(f"consult {cid} is {state}")
    if msg.get("sender") != rec.get("agent"):
        return no(f"answer is from {msg.get('sender')}, not {rec.get('agent')}")
    asks = {rec.get("ask_event"), rec.get("thread_event")} - {None}
    if not rec.get("ask_event"):
        return no("the ask was never confirmed posted")
    for key in ("in_reply_to", "thread_root"):
        rel = msg.get(key)
        if rel and rel not in asks:
            return no(f"answer {key} is {rel}, not this consult's ask")
    if not text:
        return no("answer line carries no answer")
    src = _record_path(workspace, "pending", cid)
    dst = _record_path(workspace, "answered", cid)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)  # the claim: a second match of the same answer finds nothing pending
    except FileNotFoundError:
        return no(f"consult {cid} is already answered")
    except OSError as e:
        return no(f"consult {cid} could not be closed: {e}")
    rec.update(answered_at=time.time(), answer_event=event, reply_task=task_id)
    try:
        _write_record(dst, rec)
    except OSError:
        pass
    origin = rec.get("origin") or {}
    return {"matched": True, "task_id": rec["task_id"], "agent": rec["agent"], "cid": cid,
            "reply_text": text, "event_id": event, "origin": origin, "lead": reply_lead(origin),
            "question": rec.get("question") or "", "reason": None}


def reply_lead(origin: dict) -> str:
    """Leading marker lines that send a proactive answer to the original task's conversation,
    inside its thread when it had one."""
    room = origin.get("channel_id") or origin.get("source_room_id")
    lead = f"[channel: {room}]\n" if room else ""
    if origin.get("thread_root"):
        lead += f"[thread: {origin['thread_root']}]\n"
    return lead
