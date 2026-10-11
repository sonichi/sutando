#!/usr/bin/env python3
"""owner-agent-consult policy: config, the trusted-task gate, the owner-only room guard,
roster, the consult marker and thread, the chain trace and loop check, the correlated ask
with its pending record, and matching a reply (which arrives as a new task) to that record. Pure over an injected transport, so
the rules are testable without a gateway; consult.py supplies the room CLI transport.

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

# The one definition of the consult marker prefix; ask and answer lines both start with it.
MARKER = "[owner-agent-consult:v2"
_MARKER_FAMILY = "[owner-agent-consult:"

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
    if carries_marker(text):
        return no("task is itself a consult, not an owner task; consult onward with --via-task")
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
    outcome. Found in the room by consult id: the record is completed. Otherwise it stays
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
    unresolved = []
    for st, r in loose:
        fixed = _recover_ask(r, messages, self_mxid)
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
    told, `expired` past the expiry (match then refuses), plus any task holding a root claim
    with no record (`claim_only`). Root and answer claims are pruned only once no task that
    could post from them is live; an expired consult is pruned once the owner has been told
    and its task can no longer post."""
    now = time.time() if now is None else now
    answers = Path(workspace) / _STATE / "answers"
    for f in sorted(answers.glob("*.json")) if answers.is_dir() else []:
        tasks = (_read_record(f) or {}).get("tasks") or []
        if tasks and not any(task_can_post(workspace, t) for t in tasks):
            f.unlink(missing_ok=True)
    roots = Path(workspace) / _STATE / "roots"
    claimed = {}
    for f in sorted(roots.glob("*.json")) if roots.is_dir() else []:
        task = f.name[:-len(".json")]
        if not task_can_post(workspace, task):
            f.unlink(missing_ok=True)
        else:
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


# The consult marker: ask line and answer line
def new_cid() -> str:
    return secrets.token_hex(8)


def default_limits(now: Optional[float] = None) -> dict:
    return {"since": int(time.time() if now is None else now), "max_s": DEFAULT_MAX_DURATION_S,
            "max_asks": DEFAULT_MAX_ASKS}


def ask_line(cid: str, root: Optional[str], chain: List[str], limits: Optional[dict] = None) -> str:
    lim = limits or default_limits()
    return (f"{MARKER} consult:{cid} root:{root or '-'} chain:{'>'.join(chain)} "
            f"limits:{int(lim['since'])}/{int(lim['max_s'])}/{int(lim['max_asks'])}]")


def answer_tag(cid: str) -> str:
    return f"{MARKER} answer:{cid}]"


_ASK = re.compile(rf"^{re.escape(MARKER)} consult:([0-9a-f]{{16}}) root:(\S+) chain:(\S+) "
                  r"limits:(\d{1,12})/(\d{1,6})/(\d{1,4})\]$")
_TAG = re.compile(rf"^{re.escape(MARKER)} answer:([0-9a-f]{{16}})\]$")
_ORIG_HEAD, _CHAIN_HEAD, _ASK_HEAD = "Original question:", "Chain so far:", "Question for "


def _first_line(body: str) -> tuple:
    """(first line with any leading @-mention stripped, rest of the body)."""
    lines = (body or "").strip().split("\n", 1)
    first = lines[0].strip()
    lead = first.split(" ", 1)[0]
    if _MXID.match(lead):
        first = first[len(lead):].lstrip(" —–-:")
    return first, (lines[1] if len(lines) > 1 else "")


def parse_ask(body: str) -> Optional[dict]:
    """{cid, root (None on the thread's first ask), chain, limits, original_question} or None.
    A chain lists distinct mxids, asker first; anything else is not an ask."""
    first, rest = _first_line(body)
    m = _ASK.match(first)
    if not m:
        return None
    chain = m.group(3).split(">")
    if len(chain) < 2 or len(set(chain)) != len(chain) or not all(_MXID.match(a) for a in chain):
        return None
    root = None if m.group(2) == "-" else m.group(2)
    if root is not None and not root.startswith("$"):
        return None
    orig = ""
    if rest.startswith(_ORIG_HEAD):
        orig = rest[len(_ORIG_HEAD):].split(f"\n{_CHAIN_HEAD}", 1)[0].strip()
    limits = {"since": int(m.group(4)), "max_s": int(m.group(5)), "max_asks": int(m.group(6))}
    return {"cid": m.group(1), "root": root, "chain": chain, "limits": limits, "original_question": orig}


def ask_body(question: str, cid: str, root: Optional[str], chain: List[str], original: str,
             limits: Optional[dict] = None) -> str:
    asker, target = chain[-2], chain[-1]
    return (f"{ask_line(cid, root, chain, limits)}\n"
            f"{_ORIG_HEAD} {original.strip()}\n"
            f"{_CHAIN_HEAD} {' > '.join(chain)}\n"
            f"{_ASK_HEAD}{target}: {question.strip()}\n\n"
            f"Read this thread first. Answer from what you hold with `consult.py answer`, which posts\n"
            f"ONE message in this thread @-mentioning {asker}, first line exactly\n"
            f"{answer_tag(cid)}\n"
            "If you consult another agent first, do it with `consult.py ask --via-task`, in this thread; "
            "never ask an agent already in this consult's chain. When `ask` reports the consult limit "
            "reached, answer with what you have.")


def _ts_seconds(ts) -> Optional[float]:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return float(ts) / 1000.0 if ts > 1e11 else float(ts)


def effective_limits(root_msg: dict, marker: Optional[dict], max_duration_s: int, max_asks: int) -> Optional[dict]:
    """The limits this agent enforces on a thread: the window starts at the root event's
    server timestamp (never the marker's `since`), and each limit is the smaller of the
    thread's marker and this agent's own config, so a root can narrow them, never widen them.
    None when the root event carries no usable timestamp."""
    since = _ts_seconds((root_msg or {}).get("ts"))
    if since is None:
        return None
    marker = marker or {}
    return {"since": since,
            "max_s": min(int(marker.get("max_s", max_duration_s)), int(max_duration_s)),
            "max_asks": min(int(marker.get("max_asks", max_asks)), int(max_asks))}


def limit_reached(limits: dict, asks_so_far: int, now: float) -> Optional[str]:
    """Why the thread takes no new ask, or None, for limits from effective_limits (or a
    thread's first ask, before it is posted); out-of-range values are clamped."""
    max_s = max(MAX_DURATION_FLOOR_S, min(int(limits["max_s"]), MAX_DURATION_CEILING_S))
    max_asks = max(MAX_ASKS_FLOOR, min(int(limits["max_asks"]), MAX_ASKS_CEILING))
    if now >= limits["since"] + max_s:
        return f"consult limit reached: the thread's {max_s}s window has passed"
    if asks_so_far >= max_asks:
        return f"consult limit reached: the thread already has {asks_so_far} of {max_asks} asks"
    return None


# The consult thread: what every agent reads before it asks or answers
def thread_view(transport, room: str, root: str) -> dict:
    """{ok, root_msg, msgs (oldest first), server_now} for the consult thread rooted at `root`,
    or a refusal when the root is not visible: an unseen thread cannot be checked. `server_now`
    is the newest server timestamp among the thread's own events, so an event elsewhere in the
    room cannot move the window floor."""
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
    "Time-window invariant" for what it does and does not guarantee): the largest of the
    thread's newest server timestamp, the anchor's server timestamp plus local time elapsed
    since it, and the local clock. `anchor` is (server ts, local time) of one moment, or
    (None, None). None (refuse) when no server timestamp is known, or when the local clock is
    more than CLOCK_TOLERANCE_S behind the thread's newest server timestamp."""
    if server_now_s is None or local_now < server_now_s - CLOCK_TOLERANCE_S:
        return None
    terms = [server_now_s, local_now]
    a_server, a_local = anchor if anchor else (None, None)
    if a_server is not None and a_local is not None:
        terms.append(a_server + max(0.0, local_now - a_local))
    return max(terms)


def server_now(messages: List[dict]) -> Optional[float]:
    """The newest server timestamp among `messages`, in seconds: a lower bound on the server's
    current time that no agent's own clock can move. None when no event carries one."""
    stamps = [t for t in (_ts_seconds(m.get("ts")) for m in messages) if t is not None]
    return max(stamps) if stamps else None


def asks_in(msgs: List[dict], owner_agents) -> List[dict]:
    """Every ask in the thread posted by its own chain's asker, an agent of the owner."""
    out = []
    for m in msgs:
        a = parse_ask(m.get("body") if isinstance(m.get("body"), str) else "")
        if a and m.get("sender") in owner_agents and m.get("sender") == a["chain"][-2]:
            out.append({**a, "event_id": m.get("event_id"), "sender": m.get("sender")})
    return out


def loop_check(asks: List[dict], self_mxid: str, target: str) -> Optional[str]:
    """Refusal when `target` is already in this consult's chain and this would not be a
    follow-up on an existing self->target link. Structural: read from the thread's markers."""
    seen = {a for ask in asks for a in ask["chain"]}
    links = {(ask["chain"][-2], ask["chain"][-1]) for ask in asks}
    if target in seen and (self_mxid, target) not in links:
        return (f"{target} is already in this consult's chain; answer from what the thread has "
                "instead of asking it")
    return None


def _admission(root_msg: dict, marker: dict, msg: dict, asks: List[dict], event: str,
               max_duration_s: int, max_asks: int) -> Optional[str]:
    """Why this ask was not admitted by the thread's limits when it was posted, or None: its own
    server timestamp must fall inside the root's window and its place among the thread's asks,
    in timeline order, within the ask limit. An ask admitted in time can still be answered
    after the deadline, so a chain can unwind."""
    limits = effective_limits(root_msg, marker, max_duration_s, max_asks)
    posted = _ts_seconds(msg.get("ts"))
    if limits is None or posted is None:
        return "no server timestamp on the root or the ask"
    order = [a["event_id"] for a in asks]
    if event not in order:
        return "the ask is not in its thread"
    reason = limit_reached(limits, order.index(event), posted)
    return reason.replace("consult limit reached: ", "") if reason else None


def incoming_ask(transport, room: str, self_mxid: str, task_id: str,
                 workspace: Optional[Path], owner_agents,
                 max_duration_s: int = DEFAULT_MAX_DURATION_S, max_asks: int = DEFAULT_MAX_ASKS) -> dict:
    """The consult ask a live verified task delivered to this agent, traced through the
    thread to a root ask posted by the chain's first agent (one of the owner's agents; whether
    its owner-task gate ran cannot be checked from here). Refuses anything whose chain the
    thread does not show link by link."""
    def no(reason):
        return {"ok": False, "reason": reason}
    got = live_task(task_id, workspace)
    if not got["ok"]:
        return no(got["reason"])
    import local_task_protocol as ltp
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
    if msg is None or not isinstance(msg.get("body"), str):
        return no(f"ask event not in the last {READ_LIMIT} messages of the consult room")
    ask = parse_ask(msg["body"])
    if ask is None:
        return no("task is not a consult ask")
    sender = msg.get("sender")
    if ask["chain"][-1] != self_mxid or ask["chain"][-2] != sender or sender not in owner_agents:
        return no("ask is not addressed to this agent by one of the owner's agents")
    root = ask["root"] or event
    if ask["root"] is None and (len(ask["chain"]) != 2 or msg.get("thread_root")):
        return no("a thread's first ask must be a direct ask from the owner's agent")
    if ask["root"] is not None and msg.get("thread_root") != root:
        return no("ask is not posted in its consult thread")
    view = thread_view(transport, room, root)
    if not view["ok"]:
        return no(view["reason"])
    asks = asks_in(view["msgs"], owner_agents)
    first = next((a for a in asks if a["event_id"] == root and a["root"] is None), None)
    if first is None or first["chain"][0] != ask["chain"][0]:
        return no("the thread root is not a consult ask by this chain's first agent")
    chains = {tuple(a["chain"]) for a in asks}
    for k in range(2, len(ask["chain"])):
        if tuple(ask["chain"][:k]) not in chains:
            return no(f"the thread shows no ask for {' > '.join(ask['chain'][:k])}; "
                      "the chain does not trace to the root ask")
    admitted = _admission(view["root_msg"], first["limits"], msg, asks, event, max_duration_s, max_asks)
    if admitted:
        return no(f"this ask was posted outside the thread's limits ({admitted}); "
                  "it is not answered or consulted from")
    return {"ok": True, "cid": ask["cid"], "root": root, "chain": ask["chain"], "asker": sender,
            "ask_event": event, "original_question": ask["original_question"] or first["original_question"],
            "asks": asks, "limits": first["limits"], "root_msg": view["root_msg"],
            "server_now": view["server_now"], "anchor": (_ts_seconds(msg.get("ts")), got["arrived_at"]),
            "reason": None}


# Ask: from a verified owner task, or onward from a consult ask this agent received
def consult(transport, *, room: str, self_mxid: str, agent: str, question: str,
            workspace: Optional[Path], task_id: Optional[str] = None, via_task: Optional[str] = None,
            cid: Optional[str] = None, now: Optional[Callable[[], float]] = None,
            max_duration_s: int = DEFAULT_MAX_DURATION_S, max_asks: int = DEFAULT_MAX_ASKS,
            expire_after_s: float = DEFAULT_EXPIRE_AFTER_S) -> dict:
    """Every gate, then the one post in the consult thread and its pending record; returns at
    once with {asked, agent, cid, ask_event, root, chain, limits, follow_up} or {asked: False,
    reason}. The limits given here apply only to a thread's first ask; later asks use the
    thread's. Nothing is posted unless all gates pass."""
    def no(reason, **extra):
        return {"asked": False, "agent": agent, "reason": reason, **extra}
    clock = now or time.time
    cid = cid or new_cid()
    if not _CID.match(cid):
        return no("malformed correlation id")
    if bool(task_id) == bool(via_task):
        return no("name exactly one of an owner task (--task-id) or a consult ask (--via-task)")
    origin, via = {}, None
    if task_id:
        gate = trusted_task(task_id, workspace)
        if not gate["ok"]:
            return no(gate["reason"])
        origin = gate["origin"]
    if not question or not question.strip():
        return no("empty question")
    if carries_marker(question):
        return no("question carries the consult marker")
    verdict = guard(room, self_mxid, transport)
    if not verdict["ok"]:
        return no(verdict["reason"])
    if agent not in {r["mxid"] for r in roster(verdict, self_mxid)}:
        return no(f"{agent} is not one of the owner's other agents in the consult room")
    if task_id and room in (origin.get("channel_id"), origin.get("source_room_id")) \
            and gate["sender"] != verdict["owner"]:
        return no("an owner task from the consult room must be the owner's own message")
    if task_id:
        refusal = reconcile(transport, room, self_mxid, workspace, task_id=task_id,
                            expire_after_s=expire_after_s)
        if refusal:
            return no(refusal)
        prior = prior_ask(workspace, "task_id", task_id)
        root = prior["root"] if prior else None
        chain = [self_mxid, agent]
        original = (prior or {}).get("original_question") or question.strip()
        limits = {"since": int(clock()), "max_s": int(max_duration_s), "max_asks": int(max_asks)}
        asks, now_s = [], clock()  # a first ask opens its own window; nothing to measure yet
        if root:
            view = thread_view(transport, room, root)
            if not view["ok"]:
                return no(view["reason"])
            first_own = prior or {}
            now_s = measured_now(view["server_now"],
                                 (_ts_seconds(view["root_msg"].get("ts")), first_own.get("asked_at")), clock())
            asks = asks_in(view["msgs"], verdict["owner_agents"] | {self_mxid})
            marker = (parse_ask(view["root_msg"].get("body") or "") or {}).get("limits")
            limits = effective_limits(view["root_msg"], marker, max_duration_s, max_asks)
    else:
        inc = incoming_ask(transport, room, self_mxid, via_task, workspace,
                           verdict["owner_agents"] | {self_mxid}, max_duration_s, max_asks)
        if not inc["ok"]:
            return no(inc["reason"])
        refusal = reconcile(transport, room, self_mxid, workspace, via_task=via_task, agent=agent,
                            expire_after_s=expire_after_s)
        if refusal:
            return no(refusal)
        root, asks, original = inc["root"], inc["asks"], inc["original_question"]
        limits = effective_limits(inc["root_msg"], inc["limits"], max_duration_s, max_asks)
        now_s = measured_now(inc["server_now"], inc["anchor"], clock())
        chain = inc["chain"] + [agent]
        via = {"task_id": via_task, "cid": inc["cid"], "asker": inc["asker"],
               "ask_event": inc["ask_event"]}
    refusal = loop_check(asks, self_mxid, agent)
    if refusal:
        return no(refusal)
    if limits is None:
        return no("the consult thread's root event has no server timestamp; cannot apply the thread limits")
    if now_s is None:
        return no("cannot measure the thread window on server time: this agent's clock is more than "
                  f"{CLOCK_TOLERANCE_S}s behind the consult thread's newest server timestamp")
    refusal = limit_reached(limits, len(asks), now_s)
    if refusal:
        return no(refusal, limit_reached=True)
    if _record_path(workspace, "pending", cid).exists() or _record_path(workspace, "answered", cid).exists():
        return no("correlation id already used")
    follow_up = any(a["chain"][-2:] == [self_mxid, agent] for a in asks)
    path = _record_path(workspace, "pending", cid)
    new_root = bool(task_id) and not root
    if new_root and not claim_root(workspace, task_id, cid):
        return no("another ask from this task holds its thread root and has no confirmed post; nothing "
                  "new is posted from this task. The owner can ask again with a new message")

    def release():
        if new_root:
            _root_claim(workspace, task_id).unlink(missing_ok=True)
    record = {"cid": cid, "task_id": task_id, "via_task": via_task, "via": via, "agent": agent,
              "room": room, "root": root, "chain": chain, "ask_event": None, "asked_at": clock(),
              "origin": origin, "question": question.strip()[:500], "original_question": original[:500],
              "limits": limits}
    try:
        _write_record(path, record)
    except OSError as e:
        release()
        return no(f"pending record unwritable: {e}")
    reply_to = via["ask_event"] if via else None
    sent = transport.mention(agent, ask_body(question, cid, root, chain, original, limits), room,
                             reply_to=reply_to, thread_root=root)
    if _sent_before_publication(sent):
        path.unlink(missing_ok=True)
        release()
        return no(f"ask not posted: {sent.get('reason') or 'refused'}")
    event = sent.get("event_id") if sent.get("ok") else None
    if not event:
        return {"asked": True, "unconfirmed": True, "agent": agent, "cid": cid, "ask_event": None,
                "root": root, "chain": chain, "limits": limits, "follow_up": follow_up,
                "warning": f"the post may have landed ({sent.get('reason') or 'no event id'}); the record "
                           "and any root claim are kept, and `match` or the next ask reconciles it from "
                           "the room by consult id",
                "reason": None}
    record.update(ask_event=event, root=root or event)
    warning = None
    try:
        if not _record_path(workspace, "answered", cid).exists():
            _write_record(path, record)
    except OSError as e:
        warning = f"ask posted but its event id was not recorded ({e}); match recovers it from the room"
    return {"asked": True, "agent": agent, "cid": cid, "ask_event": event, "root": record["root"],
            "chain": chain, "limits": limits, "follow_up": follow_up, "warning": warning, "reason": None}


def answer(transport, *, room: str, self_mxid: str, text: str, workspace: Optional[Path],
           task_id: Optional[str] = None, up: Optional[str] = None,
           max_duration_s: int = DEFAULT_MAX_DURATION_S, max_asks: int = DEFAULT_MAX_ASKS,
           expire_after_s: float = DEFAULT_EXPIRE_AFTER_S) -> dict:
    """Post this agent's answer to a consult ask, in its thread, replying to the ask,
    @-mentioning the asker, first line its answer line. The ask is the one a live task
    delivered (`task_id`), or, to pass an answer up the chain, the ask an onward consult of
    this agent's came from (`up`, that consult's id)."""
    def no(reason):
        return {"answered": False, "reason": reason}
    if bool(task_id) == bool(up):
        return no("name exactly one of the ask's task (--task-id) or an onward consult (--up)")
    if not text or not text.strip():
        return no("empty answer")
    if carries_marker(text):
        return no("answer text carries the consult marker")
    verdict = guard(room, self_mxid, transport)
    if not verdict["ok"]:
        return no(verdict["reason"])
    if up:
        rec = next((r for st in ("answered", "pending") if _CID.match(up)
                    for r in [_read_record(_record_path(workspace, st, up))] if r), None)
        via = (rec or {}).get("via")
        if not via or not rec.get("root"):
            return no(f"{up} is not an onward consult of this agent")
        inc = {"cid": via["cid"], "asker": via["asker"], "ask_event": via["ask_event"], "root": rec["root"]}
        poster = rec.get("reply_task")
    else:
        inc = incoming_ask(transport, room, self_mxid, task_id, workspace,
                           verdict["owner_agents"] | {self_mxid}, max_duration_s, max_asks)
        if not inc["ok"]:
            return no(inc["reason"])
        poster = task_id
    lim = effective_limits(inc["root_msg"], inc["limits"], max_duration_s, max_asks) if inc.get("root_msg") else None
    now_s = measured_now(inc["server_now"], inc["anchor"], time.time()) if lim else None
    reached = limit_reached(lim, len(inc["asks"]), now_s) if lim and now_s is not None else None
    base = {"answered": True, "to": inc["asker"], "cid": inc["cid"], "root": inc["root"],
            "limit_reached": reached, "reason": None}
    claim_path = _answer_claim(workspace, inc["cid"])
    held = _read_record(claim_path)
    if held is not None:
        if not held.get("event_id"):
            found = _own_answer(_room_messages(transport, room), self_mxid, inc["cid"])
            if not found:
                return no("an earlier answer to this ask is unconfirmed: it may already be in the room, "
                          "so it is not posted again")
            held["event_id"] = found
            _write_record(claim_path, held)
        _note_task(claim_path, held, poster)
        return {**base, "event_id": held["event_id"], "replay": True}
    if not _claim_file(claim_path, {"cid": inc["cid"], "to": inc["asker"], "at": time.time(), "event_id": None,
                                    "tasks": [t for t in [poster] if t]}):
        return no("another answer to this ask is being posted")
    sent = transport.mention(inc["asker"], f"{answer_tag(inc['cid'])}\n{text.strip()}", room,
                             reply_to=inc["ask_event"], thread_root=inc["root"])
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


def _note_task(path: Path, claim: dict, task: Optional[str]) -> None:
    """Add a task that can post this answer to its claim, so the claim outlives every such task."""
    if task and task not in claim.get("tasks", []):
        try:
            _write_record(path, {**claim, "tasks": [*claim.get("tasks", []), task]})
        except OSError:
            pass


def task_can_post(workspace: Path, task_id: Optional[str]) -> bool:
    """True while `task_id` is live in this inbox with no result: something could still post
    from it, so no claim it holds may be released."""
    import local_task_protocol as ltp
    from task_archive import find_task_file
    if not task_id or not ltp.valid_task_id(task_id):
        return False
    if find_task_file(Path(workspace) / "tasks", task_id) is None:
        return False
    return ltp.find_result(Path(workspace) / "results", task_id) is None


def _answer_claim(workspace: Path, cid: str) -> Path:
    return Path(workspace) / _STATE / "answers" / f"{cid}.json"


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


def _own_answer(messages: List[dict], self_mxid: str, cid: str) -> Optional[str]:
    """The event id of this agent's own answer to consult `cid` in the room read, if any."""
    for m in messages:
        if m.get("sender") == self_mxid and _first_line(m.get("body") or "")[0] == answer_tag(cid):
            return m.get("event_id")
    return None


def _recover_ask(rec: dict, messages: List[dict], self_mxid: str) -> dict:
    """A record whose post was not recorded (a failed write, or a crash after the post) gets
    its ask event and root back from this agent's own ask in the room, found by consult id."""
    if rec.get("ask_event"):
        return rec
    for m in messages:
        a = parse_ask(m.get("body") if isinstance(m.get("body"), str) else "")
        if a and a["cid"] == rec["cid"] and m.get("sender") == self_mxid and a["chain"][-1] == rec.get("agent"):
            return {**rec, "ask_event": m.get("event_id"), "root": a["root"] or m.get("event_id")}
    return rec


def match_reply(transport, *, room: str, self_mxid: str, task_id: str,
                workspace: Optional[Path], expire_after_s: float = DEFAULT_EXPIRE_AFTER_S,
                now: Optional[float] = None) -> dict:
    """Bind a reply task to its pending consult: the reply must be a verified live task from
    the consult room, whose room event comes from the asked agent, sits in the consult's
    thread, carries that consult's answer line, and replies to that consult's ask (or the
    root) when it cites anything. Returns where the answer goes next: the owner (`lead`) or
    up the chain (`answer_up`)."""
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
    messages = [m for m in page.get("messages") or [] if isinstance(m, dict)]
    msg = next((m for m in messages if m.get("event_id") == event), None)
    if msg is None or not isinstance(msg.get("body"), str):
        return no(f"reply event not in the last {READ_LIMIT} messages of the consult room")
    first, rest = _first_line(msg["body"])
    m = _TAG.match(first)
    if not m:
        if parse_ask(msg["body"]):
            return no("this is a consult ask to you, not an answer; answer it or consult onward")
        return no("not a final answer (no answer line); treat it as progress", progress=True)
    cid, text = m.group(1), rest.strip()
    if _record_path(workspace, "answered", cid).exists():
        return _replay(workspace, cid, task_id, no)
    rec = _read_record(_record_path(workspace, "pending", cid))
    if rec is None:
        return no(f"consult {cid} is unknown")
    if rec.get("room") != room:
        return no(f"consult {cid} was asked in {rec.get('room')}, not {room}")
    if _age(rec, time.time() if now is None else now) > expire_after_s:
        return no(f"consult {cid} expired; tell the owner it was not answered in time")
    if msg.get("sender") != rec.get("agent"):
        return no(f"answer is from {msg.get('sender')}, not {rec.get('agent')}")
    rec = _recover_ask(rec, messages, self_mxid)
    if not rec.get("ask_event"):
        return no("the ask was never confirmed posted")
    if msg.get("thread_root") != rec.get("root"):
        return no("answer is not posted in this consult's thread")
    rel = msg.get("in_reply_to")
    if rel and rel not in (rec["ask_event"], rec["root"]):
        return no(f"answer in_reply_to is {rel}, not this consult's ask")
    if not text:
        return no("answer line carries no answer")
    out = {"matched": True, "agent": rec["agent"], "cid": cid, "reply_text": text, "event_id": event,
           "root": rec["root"], "chain": rec.get("chain") or [], "question": rec.get("question") or "",
           "reason": None}
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
        return _replay(workspace, cid, task_id, no)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return no(f"consult {cid} could not be closed: {e}")
    tmp.unlink(missing_ok=True)
    src.unlink(missing_ok=True)
    _record_path(workspace, "nudged", cid).unlink(missing_ok=True)
    return out


def _replay(workspace: Path, cid: str, task_id: str, no) -> dict:
    """A closed consult's stored payload, again, for the reply task that closed it while that
    task has no result yet (its result file is the acknowledgement); anything else is closed."""
    import local_task_protocol as ltp
    rec = _read_record(_record_path(workspace, "answered", cid)) or {}
    if (rec.get("reply_task") == task_id and isinstance(rec.get("payload"), dict)
            and ltp.find_result(Path(workspace) / "results", task_id) is None):
        return {**rec["payload"], "replay": True}
    return no(f"consult {cid} is already answered")


def reply_lead(origin: dict) -> str:
    """Leading marker lines that send a proactive answer to the original task's conversation,
    inside its thread when it had one."""
    room = origin.get("channel_id") or origin.get("source_room_id")
    lead = f"[channel: {room}]\n" if room else ""
    if origin.get("thread_root"):
        lead += f"[thread: {origin['thread_root']}]\n"
    return lead
