#!/usr/bin/env python3
"""owner-agent-consult policy: config, the trusted-task gate, the owner-only room guard,
roster, one plain ask per owner task, the answer to an ask, binding a reply to its consult,
and the pending records and claims. Correlation is by Matrix relations and this agent's own
records only; no message carries anything but its text. Pure over an injected transport;
consult.py supplies the room CLI transport.

The transport is any object with:
  members(room) -> {ok, members: [{user_id, display_name, kind}], unidentified: int, reason}
  agents()      -> {ok, agents: [{id, owner, ...}], reason}   (this account's agents)
  mention(mxid, body, room, reply_to=None, thread_root=None) -> {ok, event_id, posted, reason}
  read(room, limit) -> {ok, messages: [{sender, body, event_id, ts, in_reply_to?,
                        thread_root?}] newest first, reason}

Identity is never configured here: the self mxid comes from the caller, the owner from
the gateway's agent registry, and the roster from the consult room's membership.
"""
from __future__ import annotations

import hashlib
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
             expire_after=None, max_duration=None) -> dict:
    """{active, room, room_cli, nudge_after_s, expire_after_s, max_duration_s, reason};
    inert when disabled, or when no room or no room transport is configured."""
    get = lambda k, c=None: config_value(k, c, environ, manifest_cfg)  # noqa: E731
    out = {"active": False, "room": get(CONFIG_ROOM, room), "room_cli": get(CONFIG_ROOM_CLI, room_cli),
           "nudge_after_s": _seconds(get(CONFIG_NUDGE_AFTER, nudge_after), DEFAULT_NUDGE_AFTER_S,
                                     NUDGE_FLOOR_S, NUDGE_CEILING_S),
           "expire_after_s": _seconds(get(CONFIG_EXPIRE_AFTER, expire_after), DEFAULT_EXPIRE_AFTER_S,
                                      EXPIRE_FLOOR_S, EXPIRE_CEILING_S),
           "max_duration_s": _seconds(get(CONFIG_MAX_DURATION, max_duration), DEFAULT_MAX_DURATION_S,
                                      MAX_DURATION_FLOOR_S, MAX_DURATION_CEILING_S),
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
    if ltp.find_result(Path(workspace) / "results", task_id) is not None:
        return no("task already has a result; a replayed task cannot consult")
    origin = {k: headers[k] for k in _ORIGIN_KEYS if headers.get(k)}
    return {"ok": True, "origin": origin, "sender": headers.get("user_id") or "", "reason": None}



# State under <workspace>/state/owner-agent-consult/: records {pending,answered,nudged}/<cid>.json,
# and once-only claims roots/<task>, threads/<root>, answers/<ask>, bound/<event>, never deleted.
def _record_path(workspace: Path, state: str, cid: str) -> Path:
    return Path(workspace) / _STATE / state / f"{cid}.json"


def _key_path(workspace: Path, kind: str, key: str) -> Path:
    return Path(workspace) / _STATE / kind / f"{hashlib.sha256(str(key).encode('utf-8')).hexdigest()[:16]}.json"


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


def _claim_file(path: Path, data: dict) -> bool:
    """Create `path` holding `data`, whole or not at all; False when it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)
    return True


def records(workspace: Path, state: str) -> List[dict]:
    d = Path(workspace) / _STATE / state
    out = [_read_record(p) for p in sorted(d.glob("*.json"))] if d.is_dir() else []
    return [r for r in out if r and _CID.match(str(r.get("cid") or ""))]


def _root_claim(workspace: Path, task_id: str) -> Path:
    return Path(workspace) / _STATE / "roots" / f"{task_id}.json"


def task_can_post(workspace: Path, task_id: Optional[str]) -> bool:
    """True while `task_id` is live in this inbox with no result."""
    import local_task_protocol as ltp
    from task_archive import find_task_file
    if not task_id or not ltp.valid_task_id(task_id):
        return False
    if find_task_file(Path(workspace) / "tasks", task_id) is None:
        return False
    return ltp.find_result(Path(workspace) / "results", task_id) is None


FUTURE_TOLERANCE_S = 300


def _age(rec: dict, now: float) -> float:
    """Seconds since the ask; a record stamped well in the future reads as infinitely old,
    so it expires instead of living forever."""
    age = now - float(rec.get("asked_at") or now)
    return float("inf") if age < -FUTURE_TOLERANCE_S else max(0.0, age)


def pending(workspace: Path, nudge_after_s: float, now: Optional[float] = None,
            expire_after_s: float = DEFAULT_EXPIRE_AFTER_S) -> List[dict]:
    """Unanswered consults, oldest first; `nudge_due` when overdue and the owner was not yet
    told, `unconfirmed` when the ask's post was never confirmed, `expired` past the expiry, plus
    closed consults whose relay is not acknowledged (`closed_unrelayed`) and any live task
    holding a root claim with no record (`claim_only`). Read-only: nothing is ever deleted here,
    so no race with a concurrent match can orphan a record; a cid closed meanwhile is listed once, as closed. An expired, nudged consult whose
    task can no longer post is hidden from the listing, unless a message is bound as its answer."""
    now = time.time() if now is None else now
    open_recs = [r for r in records(workspace, "pending")
                 if not _record_path(workspace, "answered", r["cid"]).exists()]
    held = _answer_bindings(workspace, {r["cid"] for r in open_recs})
    out, listed = [], set()
    for r in sorted(open_recs, key=lambda r: r.get("asked_at") or 0):
        age = _age(r, now)
        nudged = _record_path(workspace, "nudged", r["cid"]).exists()
        if nudged and age > expire_after_s and r["cid"] not in held \
                and not task_can_post(workspace, r.get("task_id")):
            continue
        listed.add(r.get("task_id"))
        overdue = age >= nudge_after_s
        out.append({**r, "age_s": int(age) if age != float("inf") else None, "overdue": overdue,
                    "nudged": nudged, "unconfirmed": not r.get("ask_event"),
                    "nudge_due": overdue and not nudged, "expired": age > expire_after_s})
    closed = {r["cid"]: r for r in records(workspace, "answered")}
    for r in out:
        if r["cid"] not in closed:
            rec = _read_record(_record_path(workspace, "answered", r["cid"]))
            if rec:
                closed[r["cid"]] = rec
    out = [r for r in out if r["cid"] not in closed]
    for r in closed.values():
        if not r.get("relayed"):
            out.append({"cid": r["cid"], "task_id": r.get("task_id"), "agent": r.get("agent"),
                        "origin": r.get("origin"), "answer_event": r.get("answer_event"),
                        "closed_unrelayed": True,
                        "reason": "closed, but its relay is not acknowledged: tell the owner that the answer "
                                  "may not have been delivered, then run `relayed <cid>`"})
    roots = Path(workspace) / _STATE / "roots"
    for f in sorted(roots.glob("*.json")) if roots.is_dir() else []:
        task = f.name[:-len(".json")]
        if task not in listed and task_can_post(workspace, task) \
                and not any(r.get("task_id") == task for r in records(workspace, "answered")):
            out.append({"task_id": task, "cid": (_read_record(f) or {}).get("cid"), "claim_only": True,
                        "unconfirmed": True,
                        "reason": "a root claim with no ask record: this task cannot ask again; "
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


# Messages: plain text; correlation only by Matrix relations and this agent's own records
def new_cid() -> str:
    return secrets.token_hex(8)


def _ts_seconds(ts) -> Optional[float]:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return float(ts) / 1000.0 if ts > 1e11 else float(ts)


_MENTION_SEPARATORS = (" — ", ": ", "\n", " ")


def leading_mention(body: str, mxid: str) -> Optional[str]:
    """The text after a leading mention of exactly `mxid` and one separator, or None when the
    body does not start with that mxid as a whole token (a longer mxid that merely starts with it is another agent)."""
    text = (body or "").strip()
    if not mxid or not text.startswith(mxid):
        return None
    rest = text[len(mxid):]
    if not rest:
        return ""
    sep = next((s for s in _MENTION_SEPARATORS if rest.startswith(s)), None)
    return rest[len(sep):] if sep else None


def _room_page(transport, room: str):
    page = transport.read(room, READ_LIMIT)
    if not page.get("ok"):
        return None, f"consult room unreadable: {page.get('reason')}"
    return [m for m in page.get("messages") or [] if isinstance(m, dict)], None


def _sent_before_publication(sent: dict) -> bool:
    """True only for a refusal the transport reports as made before anything was sent. A
    timeout, an output that is not JSON, an HTTP error or a missing event id is ambiguous."""
    return not sent.get("ok") and sent.get("posted") is False


def consult(transport, *, room: str, self_mxid: str, agent: str, question: str,
            workspace: Optional[Path], task_id: Optional[str], cid: Optional[str] = None,
            now: Optional[Callable[[], float]] = None,
            max_duration_s: int = DEFAULT_MAX_DURATION_S) -> dict:
    """Every gate, then the one ask of this owner task: a plain top-level message mentioning
    `agent`, which roots the consult thread. Returns at once with {asked, agent, cid, ask_event,
    root} or {asked: False, reason}. An unclear post outcome stays unconfirmed: it is never
    looked for in the room or posted again."""
    def no(reason):
        return {"asked": False, "agent": agent, "reason": reason}
    clock = now or time.time
    cid = cid or new_cid()
    if not _CID.match(cid):
        return no("malformed correlation id")
    gate = trusted_task(task_id, workspace)
    if not gate["ok"]:
        return no(gate["reason"])
    origin = gate["origin"]
    if origin.get("source") != "ag2space":
        return no(f"relaying back to {origin.get('source') or 'unknown'} conversations is not supported; "
                  "answer without consulting")
    if not question or not question.strip():
        return no("empty question")
    verdict = guard(room, self_mxid, transport)
    if not verdict["ok"]:
        return no(verdict["reason"])
    if agent not in {r["mxid"] for r in roster(verdict, self_mxid)}:
        return no(f"{agent} is not one of the owner's other agents in the consult room")
    if room in (origin.get("channel_id"), origin.get("source_room_id")) and gate["sender"] != verdict["owner"]:
        return no("an owner task from the consult room must be the owner's own message")
    if _record_path(workspace, "pending", cid).exists() or _record_path(workspace, "answered", cid).exists():
        return no("correlation id already used")
    if not _claim_file(_root_claim(workspace, task_id), {"cid": cid, "task_id": task_id, "asked_at": clock()}):
        return no("this owner task has already asked (one ask per task); the owner can ask again "
                  "with a new message")

    def release():
        _root_claim(workspace, task_id).unlink(missing_ok=True)
    body = question.strip()
    path = _record_path(workspace, "pending", cid)
    record = {"cid": cid, "task_id": task_id, "agent": agent, "room": room, "root": None, "ask_event": None,
              "asked_at": clock(), "origin": origin, "body": body,
              "max_s": max(MAX_DURATION_FLOOR_S, min(int(max_duration_s), MAX_DURATION_CEILING_S))}
    try:
        _write_record(path, record)
    except OSError as e:
        release()
        return no(f"pending record unwritable: {e}")
    sent = transport.mention(agent, body, room)
    if _sent_before_publication(sent):
        path.unlink(missing_ok=True)
        release()
        return no(f"ask not posted: {sent.get('reason') or 'refused'}")
    unconfirmed = {"asked": True, "unconfirmed": True, "agent": agent, "cid": cid, "ask_event": None,
                   "root": None, "reason": None}
    event = sent.get("event_id") if sent.get("ok") else None
    if not event:
        return {**unconfirmed, "warning": f"could not confirm the ask was posted ({sent.get('reason') or 'no event id'}); "
                                          "it is not looked for or retried. Tell the owner; the owner can ask "
                                          "again with a new message"}
    if not _claim_file(_key_path(workspace, "threads", event), {"cid": cid, "ask_event": event}):
        return {**unconfirmed, "warning": f"the ask's event {event} already roots another consult; this one stays "
                                          "unconfirmed. Tell the owner; the owner can ask again with a new message"}
    record.update(ask_event=event, root=event)
    try:
        _write_record(path, record)
    except OSError as e:
        return {**unconfirmed, "warning": f"ask posted but not recorded ({e}); it stays unconfirmed and its answer "
                                          "will not be matched. Tell the owner; the owner can ask again"}
    return {"asked": True, "agent": agent, "cid": cid, "ask_event": event, "root": event, "reason": None}


def incoming_ask(transport, room: str, self_mxid: str, task_id: str, workspace: Optional[Path],
                 owner_agents) -> dict:
    """The consult ask a live verified task delivered to this agent: a thread root in the
    consult room, from another of the owner's agents, whose plain body starts with exactly this
    agent's mxid."""
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
        return no("the ask's task already has a result; a closed ask is not answered again")
    event = h.get("source_message_id")
    messages, err = _room_page(transport, room)
    if err:
        return no(err)
    msg = next((m for m in messages if event and m.get("event_id") == event), None)
    if msg is None:
        return no(f"ask event not in the last {READ_LIMIT} messages of the consult room")
    sender = msg.get("sender")
    if sender == self_mxid or sender not in owner_agents:
        return no("the ask is not from another of the owner's agents")
    if leading_mention(msg.get("body") or "", self_mxid) is None:
        return no("the message is not addressed to this agent (its body must start with this agent's exact mxid)")
    if msg.get("thread_root") not in (None, event) or msg.get("in_reply_to"):
        return no("the message is inside a thread or replies to another, not a consult ask "
                  "(an ask roots its own thread and replies to nothing)")
    return {"ok": True, "asker": sender, "ask_event": event, "reason": None}


def answer(transport, *, room: str, self_mxid: str, text: str, workspace: Optional[Path],
           task_id: Optional[str]) -> dict:
    """Post this agent's answer to the consult ask a live task delivered, once: a plain message
    in the ask's thread, replying to the ask and mentioning the asker. An unclear post outcome
    stays unconfirmed and is never posted again or looked for in the room."""
    def no(reason, **extra):
        return {"answered": False, "reason": reason, **extra}
    if not text or not text.strip():
        return no("empty answer")
    verdict = guard(room, self_mxid, transport)
    if not verdict["ok"]:
        return no(verdict["reason"])
    inc = incoming_ask(transport, room, self_mxid, task_id, workspace, verdict["owner_agents"])
    if not inc["ok"]:
        return no(inc["reason"])
    base = {"answered": True, "to": inc["asker"], "ask_event": inc["ask_event"], "root": inc["ask_event"],
            "reason": None}
    claim = _key_path(workspace, "answers", inc["ask_event"])
    held = _read_record(claim)
    if held is not None:
        if not held.get("event_id"):
            return no("an earlier answer to this ask is unconfirmed; it is not posted again", unconfirmed=True)
        return {**base, "event_id": held["event_id"], "already_posted": True}
    if not _claim_file(claim, {"ask_event": inc["ask_event"], "to": inc["asker"], "at": time.time(),
                               "event_id": None, "task_id": task_id}):
        return no("another answer to this ask is being posted")
    sent = transport.mention(inc["asker"], text.strip(), room, reply_to=inc["ask_event"],
                             thread_root=inc["ask_event"])
    if _sent_before_publication(sent):
        claim.unlink(missing_ok=True)
        return no(f"answer not posted: {sent.get('reason')}")
    event = sent.get("event_id") if sent.get("ok") else None
    if not event:
        return {**base, "event_id": None, "unconfirmed": True,
                "warning": f"could not confirm the answer was posted ({sent.get('reason') or 'no event id'}); "
                           "it is not posted again"}
    try:
        _write_record(claim, {**(_read_record(claim) or {}), "event_id": event})
    except OSError:
        pass
    return {**base, "event_id": event}


def _consult_of(workspace: Path, msg: dict) -> tuple:
    """(record, None) for the one consult a message can belong to: the consult whose thread it
    is in, or whose ask it replies to. (None, reason) when neither, or when they disagree."""
    found = {}
    for rel in ("thread_root", "in_reply_to"):
        ev = msg.get(rel)
        claim = _read_record(_key_path(workspace, "threads", ev)) if ev else None
        if claim and claim.get("ask_event") == ev:
            found[rel] = str(claim.get("cid") or "")
    if len(set(found.values())) > 1:
        return None, "the message's thread and the ask it replies to are different consults"
    if not found:
        return None, "not in the thread of, or a reply to, a consult of this agent"
    cid = next(iter(found.values()))
    rec = _read_record(_record_path(workspace, "pending", cid)) or _read_record(_record_path(workspace, "answered", cid))
    if rec is None or not rec.get("ask_event"):
        return None, f"consult {cid} has no confirmed ask record"
    return rec, None


def _in_consult(workspace: Path, rec: dict, m: dict) -> bool:
    """The asked agent's message that belongs to this consult and no other: one whose thread
    and reply disagree belongs to neither, so it never blocks the order of either."""
    if m.get("sender") != rec.get("agent") or m.get("event_id") == rec.get("ask_event"):
        return False
    owner, _ = _consult_of(workspace, m)
    return bool(owner) and owner.get("cid") == rec.get("cid")


def _window_refusal(rec: dict, msg: dict, messages: List[dict], max_duration_s: int) -> Optional[str]:
    """Why `msg` falls outside the consult's answer window, measured on server timestamps."""
    ask = next((m for m in messages if m.get("event_id") == rec["ask_event"]), None)
    ask_ts, msg_ts = _ts_seconds((ask or {}).get("ts")), _ts_seconds(msg.get("ts"))
    if ask_ts is None or msg_ts is None:
        return ("the ask or the message has no server timestamp in the last "
                f"{READ_LIMIT} messages; cannot apply the answer window")
    window = min(int(rec.get("max_s") or max_duration_s), int(max_duration_s))
    if msg_ts - ask_ts < 0:
        return f"stamped {int(ask_ts - msg_ts)}s before the ask; outside the answer window"
    if msg_ts - ask_ts > window:
        return f"posted {int(msg_ts - ask_ts)}s after the ask, outside the {window}s answer window"
    return None


def _reply_text(msg: dict, self_mxid: str) -> str:
    """The message text without a leading mention of this agent, with result markers neutralized."""
    from result_markers import neutralize_markers
    text = leading_mention(msg.get("body") or "", self_mxid)
    return neutralize_markers((msg.get("body") or "").strip() if text is None else text)


def _payload(rec: dict, event: str, text: str) -> dict:
    origin = rec.get("origin") or {}
    return {"matched": True, "agent": rec["agent"], "cid": rec["cid"], "reply_text": text, "event_id": event,
            "root": rec["ask_event"], "question": rec.get("body") or "", "task_id": rec["task_id"],
            "origin": origin, "lead": reply_lead(origin), "reason": None}


def match_reply(transport, *, room: str, self_mxid: str, workspace: Optional[Path],
                task_id: Optional[str] = None, event_id: Optional[str] = None, final: bool = False,
                not_final: bool = False, max_duration_s: int = DEFAULT_MAX_DURATION_S,
                expire_after_s: float = DEFAULT_EXPIRE_AFTER_S, now: Optional[float] = None) -> dict:
    """Bind a message to the one consult it can answer: the asked agent's message in that
    consult's thread (or replying to its ask), inside the answer window. Named by the task it
    arrived as (`task_id`) or by its event (`event_id`). With neither flag it only looks
    (`candidate: true`). `final` binds it as the answer and closes the consult; `not_final`
    binds it as progress. A message is bound once (`bound/<event>`, never deleted); the
    binding holds what closing needs, so a failed close finishes without reading the room.
    A consult closes once and its answer is returned once; nothing is ever replayed."""
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
    import local_task_protocol as ltp
    if task_id and ltp.find_result(Path(workspace) / "results", task_id) is not None:
        return no("the reply task already has a result; a handled task binds and closes nothing "
                  "(use --event-id)")
    bind = _key_path(workspace, "bound", event)
    bound = _read_record(bind)
    if bound is not None:
        return _judged(workspace, bound, event, task_id, final, not_final, no)
    messages, err = _room_page(transport, room)
    if err:
        return no(err)
    msg = next((m for m in messages if m.get("event_id") == event), None)
    if msg is None:
        return no(f"message not in the last {READ_LIMIT} messages of the consult room")
    rec, why = _consult_of(workspace, msg)
    if rec is None:
        return no(why)
    cid = rec["cid"]
    if rec.get("room") != room:
        return no(f"consult {cid} was asked in another room")
    if _record_path(workspace, "answered", cid).exists():
        return _already_answered(workspace, cid, event, task_id, no)
    if not _in_consult(workspace, rec, msg):
        return no(f"not from {rec['agent']}, the agent consult {cid} asked")
    late = _window_refusal(rec, msg, messages, max_duration_s)
    if late:
        return no(late)
    if _age(rec, time.time() if now is None else now) > expire_after_s:
        return no(f"consult {cid} expired; tell the owner it was not answered in time")
    later = False
    for m in messages:  # newest first: anything after `msg` here is older than it
        if m.get("event_id") == event:
            later = True
        elif later and _in_consult(workspace, rec, m) and not _key_path(workspace, "bound", m.get("event_id")).exists() \
                and not _window_refusal(rec, m, messages, max_duration_s):  # one that cannot bind holds nothing up
            return no(f"an earlier message from {rec['agent']} in this consult, {m.get('event_id')}, is not "
                      "judged yet; judge it first", earlier=m.get("event_id"))
    text = _reply_text(msg, self_mxid)
    if not final and not not_final:
        return {"matched": False, "candidate": True, "agent": rec["agent"], "cid": cid, "reply_text": text,
                "event_id": event, "reason": "read it, then run again with --final (the answer) "
                                              "or --not-final (progress)"}
    data = {"event": event, "cid": cid, "disposition": "answer" if final else "progress", "at": time.time(),
            "reply_task": task_id}
    if final:
        data.update(payload=_payload(rec, event, text), record=rec)
    try:
        fresh = _claim_file(bind, data)
    except OSError as e:
        return no(f"message could not be bound: {e}")
    if not fresh:
        return _judged(workspace, _read_record(bind) or {}, event, task_id, final, not_final, no)
    if not_final:
        return no("recorded as not final; the asked agent's next message in the thread is the candidate",
                  progress=True, cid=cid)
    return _close(workspace, data, event, task_id, no)


def _judged(workspace: Path, bound: dict, event: str, task_id, final: bool, not_final: bool, no) -> dict:
    """A message already bound: its recorded judgement wins; an answer binding finishes
    closing its own consult from what the binding holds, without the room or the expiry."""
    cid = bound.get("cid")
    if bound.get("disposition") == "progress":
        if final:
            return no("this message was bound as not final; --final conflicts with that judgement",
                      conflict=True, cid=cid)
        return no("this message was already judged not final", progress=True, cid=cid)
    if not isinstance(bound.get("payload"), dict) or not isinstance(bound.get("record"), dict):
        return no(f"the binding of {event} is malformed; nothing is closed")
    if not_final:
        return no("this message was bound as the answer; --not-final conflicts with that judgement "
                  "(run --final to finish closing)", conflict=True, cid=cid)
    if _record_path(workspace, "answered", str(cid)).exists():
        return _already_answered(workspace, str(cid), event, task_id, no)
    if not final:
        return no("this message was bound as the answer; run --final to finish closing", candidate=True,
                  bound="answer", cid=cid, reply_text=bound["payload"].get("reply_text"), event_id=event)
    return _close(workspace, bound, event, task_id, no)


def _close(workspace: Path, binding: dict, event: str, task_id, no) -> dict:
    """Close the binding's consult once: link answered/<cid>, recording which route closed it.
    The answer is returned to this caller only; a crash before its relay is reported, not retried."""
    cid = str(binding["cid"])
    dst = _record_path(workspace, "answered", cid)
    claim = {**binding["record"], "answered_at": time.time(), "answer_event": event,
             "reply_task": task_id, "payload": binding["payload"]}
    try:
        if not _claim_file(dst, claim):
            return _already_answered(workspace, cid, event, task_id, no)
    except OSError as e:
        return no(f"consult {cid} could not be closed: {e}")
    _record_path(workspace, "pending", cid).unlink(missing_ok=True)
    _record_path(workspace, "nudged", cid).unlink(missing_ok=True)
    return binding["payload"]


def _already_answered(workspace: Path, cid: str, event: str, task_id: Optional[str], no) -> dict:
    """Refuse a second delivery. The route that closed the consult (the same message, through the
    same reply task or the same --event-id), while `relayed <cid>` is not recorded, is told so:
    the owner must hear the answer may be lost."""
    rec = _read_record(_record_path(workspace, "answered", cid)) or {}
    if rec.get("answer_event") == event and rec.get("reply_task") == task_id and not rec.get("relayed"):
        return no(f"consult {cid} was closed by this route and its relay is not acknowledged; the answer "
                  "is never delivered twice. If you did not relay it, tell the owner that it may not have "
                  "been delivered; then run `relayed <cid>`",
                  answered_by=event, closed_unrelayed=True, cid=cid)
    return no(f"consult {cid} is already answered; its answer is never delivered twice",
              answered_by=rec.get("answer_event"))


def _answer_bindings(workspace: Path, cids) -> dict:
    """{cid: binding} for messages bound as these consults' answers; one pass over bound/."""
    d, out = Path(workspace) / _STATE / "bound", {}
    for p in sorted(d.glob("*.json")) if cids and d.is_dir() else []:
        b = _read_record(p)
        if b and b.get("cid") in cids and b.get("disposition") == "answer":
            out.setdefault(b["cid"], b)
    return out


def mark_relayed(workspace: Path, cid: str) -> dict:
    """Acknowledge a closed consult's relay (delivered, or reported to the owner as possibly not).
    The only acknowledgement: a reply task's result is not one."""
    path = _record_path(workspace, "answered", cid) if _CID.match(cid or "") else None
    rec = _read_record(path) if path else None
    if rec is None:
        return {"ok": False, "reason": f"no answered consult {cid}"}
    _write_record(path, {**rec, "relayed": True})
    return {"ok": True, "cid": cid, "reason": None}


def waiting_answers(transport, room: str, workspace: Path, max_duration_s: int = DEFAULT_MAX_DURATION_S,
                    self_mxid: str = "") -> dict:
    """{cid: {event_id, text[, refused | bound]}}: for each confirmed pending consult, the asked
    agent's oldest message in it not yet judged, when no task brought it here. `refused` carries
    the reason `match` will give when it falls outside the answer window; `bound: "answer"` marks a
    message already bound as the answer whose close did not land. One room read."""
    messages, err = _room_page(transport, room)
    out = {}
    open_recs = [r for r in records(workspace, "pending") if r.get("ask_event") and r.get("room") == room
                 and not _record_path(workspace, "answered", r["cid"]).exists()]
    held_by = _answer_bindings(workspace, {r["cid"] for r in open_recs})  # from files, not the room
    for r in open_recs:
        held = held_by.get(r["cid"])
        if held:
            out[r["cid"]] = {"event_id": held.get("event"), "text": (held.get("payload") or {}).get("reply_text"),
                             "bound": "answer"}
            continue
        if err:
            continue
        cands = [m for m in reversed(messages) if _in_consult(workspace, r, m)
                 and not _key_path(workspace, "bound", m.get("event_id")).exists()]
        if cands:
            first = next((m for m in cands if not _window_refusal(r, m, messages, max_duration_s)), cands[0])
            row = {"event_id": first.get("event_id"), "text": _reply_text(first, self_mxid)}
            late = _window_refusal(r, first, messages, max_duration_s)
            out[r["cid"]] = {**row, "refused": late} if late else row
    return {c: v for c, v in out.items() if not _record_path(workspace, "answered", c).exists()}


def reply_lead(origin: dict) -> str:
    """Leading marker lines that send a proactive answer to the original task's conversation,
    inside its thread when it had one."""
    room = origin.get("channel_id") or origin.get("source_room_id")
    lead = f"[channel: {room}]\n" if room else ""
    if origin.get("thread_root"):
        lead += f"[thread: {origin['thread_root']}]\n"
    return lead
