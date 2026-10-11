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


def prior_ask(workspace: Path, key: str, value: str) -> Optional[dict]:
    """The first earlier posted ask whose `key` (task_id, or via_task) is `value`."""
    same = [r for state in ("pending", "answered") for r in records(workspace, state)
            if r.get(key) == value and r.get("ask_event") and r.get("root")]
    return min(same, key=lambda r: r.get("asked_at") or 0) if same else None


def _age(rec: dict, now: float) -> float:
    return max(0.0, now - float(rec.get("asked_at") or now))


def pending(workspace: Path, nudge_after_s: float, now: Optional[float] = None,
            expire_after_s: float = DEFAULT_EXPIRE_AFTER_S) -> List[dict]:
    """Unanswered consults, oldest first; `nudge_due` when overdue and the owner was not yet
    told, `expired` past the expiry (match then refuses). Prunes answered and nudge records
    older than the expiry."""
    now = time.time() if now is None else now
    for state in ("answered", "nudged"):
        for r in records(workspace, state):
            if _age(r, now) > expire_after_s:
                _record_path(workspace, state, r["cid"]).unlink(missing_ok=True)
    out = []
    for r in sorted(records(workspace, "pending"), key=lambda r: r.get("asked_at") or 0):
        age = _age(r, now)
        overdue = age >= nudge_after_s
        nudged = _record_path(workspace, "nudged", r["cid"]).exists()
        out.append({**r, "age_s": int(age), "overdue": overdue, "nudged": nudged,
                    "nudge_due": overdue and not nudged, "expired": age > expire_after_s})
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
            f"limits:{lim['since']}/{lim['max_s']}/{lim['max_asks']}]")


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


def limit_reached(limits: dict, asks_so_far: int, now: float) -> Optional[str]:
    """Why the thread takes no new ask, or None. Limits come from the thread's first ask, so
    every agent in the chain enforces the same ones; out-of-range values are clamped."""
    max_s = max(MAX_DURATION_FLOOR_S, min(int(limits["max_s"]), MAX_DURATION_CEILING_S))
    max_asks = max(MAX_ASKS_FLOOR, min(int(limits["max_asks"]), MAX_ASKS_CEILING))
    if now >= limits["since"] + max_s:
        return f"consult limit reached: the thread's {max_s}s window has passed"
    if asks_so_far >= max_asks:
        return f"consult limit reached: the thread already has {asks_so_far} of {max_asks} asks"
    return None


# The consult thread: what every agent reads before it asks or answers
def thread_view(transport, room: str, root: str) -> dict:
    """{ok, root_msg, msgs (oldest first)} for the consult thread rooted at `root`,
    or a refusal when the root is not visible: an unseen thread cannot be checked."""
    page = transport.read(room, READ_LIMIT)
    if not page.get("ok"):
        return {"ok": False, "reason": f"consult room unreadable: {page.get('reason')}"}
    msgs = [m for m in page.get("messages") or [] if isinstance(m, dict)]
    root_msg = next((m for m in msgs if m.get("event_id") == root), None)
    if root_msg is None:
        return {"ok": False, "reason": f"consult thread root {root} is not in the last {READ_LIMIT} "
                                       "messages of the consult room; cannot see the whole thread"}
    inside = [m for m in msgs if m.get("event_id") == root or m.get("thread_root") == root]
    return {"ok": True, "root_msg": root_msg, "msgs": list(reversed(inside)), "all": msgs}


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


def incoming_ask(transport, room: str, self_mxid: str, task_id: str,
                 workspace: Optional[Path], owner_agents) -> dict:
    """The consult ask a live verified task delivered to this agent, traced through the
    thread to a root ask, which only the verified-owner-task gate posts. Refuses anything
    whose chain the thread does not show link by link."""
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
    return {"ok": True, "cid": ask["cid"], "root": root, "chain": ask["chain"], "asker": sender,
            "ask_event": event, "original_question": ask["original_question"] or first["original_question"],
            "asks": asks, "limits": first["limits"], "reason": None}


# Ask: from a verified owner task, or onward from a consult ask this agent received
def consult(transport, *, room: str, self_mxid: str, agent: str, question: str,
            workspace: Optional[Path], task_id: Optional[str] = None, via_task: Optional[str] = None,
            cid: Optional[str] = None, now: Optional[Callable[[], float]] = None,
            max_duration_s: int = DEFAULT_MAX_DURATION_S, max_asks: int = DEFAULT_MAX_ASKS) -> dict:
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
        prior = prior_ask(workspace, "task_id", task_id)
        root = prior["root"] if prior else None
        chain = [self_mxid, agent]
        original = (prior or {}).get("original_question") or question.strip()
        limits = (prior or {}).get("limits") or {"since": int(clock()), "max_s": int(max_duration_s),
                                                 "max_asks": int(max_asks)}
        asks = []
        if root:
            view = thread_view(transport, room, root)
            if not view["ok"]:
                return no(view["reason"])
            asks = asks_in(view["msgs"], verdict["owner_agents"] | {self_mxid})
    else:
        inc = incoming_ask(transport, room, self_mxid, via_task, workspace,
                           verdict["owner_agents"] | {self_mxid})
        if not inc["ok"]:
            return no(inc["reason"])
        root, asks, original, limits = inc["root"], inc["asks"], inc["original_question"], inc["limits"]
        chain = inc["chain"] + [agent]
        via = {"task_id": via_task, "cid": inc["cid"], "asker": inc["asker"],
               "ask_event": inc["ask_event"]}
    refusal = loop_check(asks, self_mxid, agent)
    if refusal:
        return no(refusal)
    refusal = limit_reached(limits, len(asks), clock())
    if refusal:
        return no(refusal, limit_reached=True)
    if _record_path(workspace, "pending", cid).exists() or _record_path(workspace, "answered", cid).exists():
        return no("correlation id already used")
    follow_up = any(a["chain"][-2:] == [self_mxid, agent] for a in asks)
    path = _record_path(workspace, "pending", cid)
    record = {"cid": cid, "task_id": task_id, "via_task": via_task, "via": via, "agent": agent,
              "room": room, "root": root, "chain": chain, "ask_event": None, "asked_at": clock(),
              "origin": origin, "question": question.strip()[:500], "original_question": original[:500],
              "limits": limits}
    try:
        _write_record(path, record)
    except OSError as e:
        return no(f"pending record unwritable: {e}")
    reply_to = via["ask_event"] if via else None
    sent = transport.mention(agent, ask_body(question, cid, root, chain, original, limits), room,
                             reply_to=reply_to, thread_root=root)
    event = sent.get("event_id") if sent.get("ok") else None
    if not event:
        path.unlink(missing_ok=True)
        return no(f"ask not posted: {sent.get('reason') or 'no event id'}")
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
           task_id: Optional[str] = None, up: Optional[str] = None) -> dict:
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
    else:
        inc = incoming_ask(transport, room, self_mxid, task_id, workspace,
                           verdict["owner_agents"] | {self_mxid})
        if not inc["ok"]:
            return no(inc["reason"])
    sent = transport.mention(inc["asker"], f"{answer_tag(inc['cid'])}\n{text.strip()}", room,
                             reply_to=inc["ask_event"], thread_root=inc["root"])
    if not sent.get("ok"):
        return no(f"answer not posted: {sent.get('reason')}")
    reached = limit_reached(inc["limits"], len(inc["asks"]), time.time()) if inc.get("limits") else None
    return {"answered": True, "to": inc["asker"], "cid": inc["cid"], "root": inc["root"],
            "event_id": sent.get("event_id"), "limit_reached": reached, "reason": None}


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
    rec = _read_record(_record_path(workspace, "pending", cid))
    if rec is None or _record_path(workspace, "answered", cid).exists():
        state = "already answered" if _record_path(workspace, "answered", cid).exists() else "unknown"
        return no(f"consult {cid} is {state}")
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
    src = _record_path(workspace, "pending", cid)
    dst = _record_path(workspace, "answered", cid)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(src, dst)  # the claim: link fails once answered/<cid> exists, whatever pending holds
    except (FileNotFoundError, FileExistsError):
        return no(f"consult {cid} is already answered")
    except OSError as e:
        return no(f"consult {cid} could not be closed: {e}")
    src.unlink(missing_ok=True)
    _record_path(workspace, "nudged", cid).unlink(missing_ok=True)
    rec.update(answered_at=time.time(), answer_event=event, reply_task=task_id)
    try:
        _write_record(dst, rec)
    except OSError:
        pass
    out = {"matched": True, "agent": rec["agent"], "cid": cid, "reply_text": text, "event_id": event,
           "root": rec["root"], "chain": rec.get("chain") or [], "question": rec.get("question") or "",
           "reason": None}
    if rec.get("via"):
        return {**out, "task_id": None, "answer_up": {**rec["via"], "root": rec["root"], "up": cid}}
    origin = rec.get("origin") or {}
    return {**out, "task_id": rec["task_id"], "origin": origin, "lead": reply_lead(origin)}


def reply_lead(origin: dict) -> str:
    """Leading marker lines that send a proactive answer to the original task's conversation,
    inside its thread when it had one."""
    room = origin.get("channel_id") or origin.get("source_room_id")
    lead = f"[channel: {room}]\n" if room else ""
    if origin.get("thread_root"):
        lead += f"[thread: {origin['thread_root']}]\n"
    return lead
