#!/usr/bin/env python3
"""owner-agent-consult policy: config, the trusted-task gate, the owner-only room guard,
roster, one-hop marker, the correlated ask and the bounded wait for its final answer.
Pure over an injected transport, so the rules are testable without a gateway;
consult.py supplies the transport from the configured room CLI.

The transport is any object with:
  members(room) -> {ok, members: [{user_id, display_name, kind}], unidentified: int, reason}
  agents()      -> {ok, agents: [{id, owner, ...}], reason}   (this account's agents)
  mention(mxid, body, room) -> {ok, event_id, reason}
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

# The one definition of the one-hop marker; a task or question carrying it is never consulted onward.
MARKER = "[owner-agent-consult:v1]"
_MARKER_FAMILY = "[owner-agent-consult:"

CONFIG_ENABLED = "OWNER_AGENT_CONSULT_ENABLED"
CONFIG_ROOM = "OWNER_AGENT_CONSULT_ROOM"
CONFIG_ROOM_CLI = "OWNER_AGENT_CONSULT_ROOM_CLI"
CONFIG_MAX_WAIT = "OWNER_AGENT_CONSULT_MAX_WAIT_S"
DEFAULT_MAX_WAIT_S = 120
MAX_WAIT_CEILING_S = 600
POLL_INTERVAL_S = 5.0
READ_LIMIT = 30
MAX_TASK_BYTES = 256 * 1024
_FALSE = ("0", "false", "no", "off")
_MXID = re.compile(r"^@[^:\s]+:[^\s]+$")
_CID = re.compile(r"^[0-9a-f]{16}$")
_LEDGER = Path("state") / "owner-agent-consult" / "consumed"


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


def settings(room=None, max_wait=None, room_cli=None, environ=None, manifest_cfg=None) -> dict:
    """{active, room, room_cli, max_wait_s, reason}; inert when disabled, or when no room
    or no room transport is configured."""
    get = lambda k, c=None: config_value(k, c, environ, manifest_cfg)  # noqa: E731
    try:
        wait = int(float(get(CONFIG_MAX_WAIT, max_wait) or DEFAULT_MAX_WAIT_S))
    except ValueError:
        wait = DEFAULT_MAX_WAIT_S
    wait = max(1, min(wait, MAX_WAIT_CEILING_S))
    out = {"active": False, "room": get(CONFIG_ROOM, room), "room_cli": get(CONFIG_ROOM_CLI, room_cli),
           "max_wait_s": wait, "reason": None}
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


def trusted_task(task_id: str, workspace: Optional[Path]) -> dict:
    """The live, envelope-verified, unanswered owner task named by id in this workspace's
    inbox. A caller-supplied path or text is never the authority; anything unprovable fails closed."""
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
        return no(f"task envelope is {verdict}; only a verified envelope establishes owner origin")
    headers = attested_task_headers(text, Path(workspace)).headers
    if headers.get("id") != task_id:
        return no("task does not carry its own id")
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
    return {"ok": True, "reason": None}


def claim(task_id: str, agent: str, cid: str, workspace: Path) -> dict:
    """Single use per (task, agent): a replay of the same task cannot ask again."""
    d = Path(workspace) / _LEDGER
    name = f"{task_id}.{hashlib.sha256(agent.encode('utf-8')).hexdigest()[:16]}"
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {"ok": False, "reason": f"{task_id} already consulted {agent}; a task consults each agent once"}
    except OSError as e:
        return {"ok": False, "reason": f"consult ledger unwritable: {e}"}
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"task": task_id, "agent": agent, "cid": cid, "at": time.time()}, fh)
    return {"ok": True, "reason": None}


# Correlated ask and its final answer
def new_cid() -> str:
    return secrets.token_hex(8)


def answer_tag(cid: str) -> str:
    return f"{_MARKER_FAMILY}v1 answer:{cid}]"


def ask_body(question: str, cid: str) -> str:
    return (f"{MARKER} consult:{cid}\n{question.strip()}\n\n"
            "Answer here from what you hold, in one message whose first line is exactly\n"
            f"{answer_tag(cid)}\n"
            "Anything without that first line is read as progress, not your answer. "
            "This is a one-hop consult: do not consult another agent.")


def _ts_ms(ts) -> Optional[float]:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return float(ts) if ts > 1e12 else float(ts) * 1000.0


def replies_after(messages: List[dict], agent: str, ask_event: Optional[str],
                  asked_at_ms: float) -> List[dict]:
    """Messages from `agent` newer than the ask, oldest first (newest-first input). Bounded
    by the ask's event id when it is in the window, else by the ask's send time."""
    newer = []
    seen_ask = False
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if ask_event and m.get("event_id") == ask_event:
            seen_ask = True
            break
        newer.append(m)
    if not seen_ask:
        newer = [m for m in newer if (_ts_ms(m.get("ts")) or 0) >= asked_at_ms]
    hits = [m for m in newer if m.get("sender") == agent
            and isinstance(m.get("body"), str) and m["body"].strip()]
    return list(reversed(hits))


def final_answer(m: dict, cid: str, ask_event: Optional[str]) -> Optional[str]:
    """The answer text when `m` is this consult's final reply, else None. A reply related
    to some other event than the ask is not bound to it, whatever its text says."""
    lines = m["body"].strip().split("\n", 1)
    if lines[0].strip() != answer_tag(cid):
        return None
    for key in ("in_reply_to", "thread_root"):
        rel = m.get(key)
        if rel and ask_event and rel != ask_event:
            return None
    text = lines[1].strip() if len(lines) > 1 else ""
    return text or None


def wait_for_reply(transport, room: str, agent: str, ask_event: Optional[str], asked_at_ms: float,
                   max_wait_s: float, cid: str, clock: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep,
                   interval: float = POLL_INTERVAL_S) -> dict:
    deadline = clock() + max_wait_s
    last_reason = None
    progress = 0
    while True:
        got = transport.read(room, READ_LIMIT)
        if got.get("ok"):
            hits = replies_after(got.get("messages") or [], agent, ask_event, asked_at_ms)
            for h in hits:
                text = final_answer(h, cid, ask_event)
                if text is not None:
                    return {"answered": True, "agent": agent, "cid": cid, "reply_text": text,
                            "event_ids": [h.get("event_id")]}
            progress = max(progress, len(hits))
        else:
            last_reason = got.get("reason")
        left = deadline - clock()
        if left <= 0:
            break
        sleep(min(interval, left))
    reason = f"no final answer from {agent} within {int(max_wait_s)}s"
    if progress:
        reason += f" ({progress} progress message(s) seen)"
    if last_reason:
        reason += f" (last read error: {last_reason})"
    return {"answered": False, "agent": agent, "cid": cid, "reason": reason}


def consult(transport, *, room: str, self_mxid: str, agent: str, question: str,
            task_id: str, max_wait_s: float, workspace: Optional[Path],
            cid: Optional[str] = None, now_ms: Optional[Callable[[], float]] = None,
            clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep) -> dict:
    """Every gate before the one post; returns {answered, agent, reply_text} or
    {answered: False, reason}. Nothing is posted unless all gates pass."""
    def no(reason):
        return {"answered": False, "agent": agent, "reason": reason}
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
    held = claim(task_id, agent, cid, workspace)
    if not held["ok"]:
        return no(held["reason"])
    asked_at = (now_ms or (lambda: time.time() * 1000.0))()
    sent = transport.mention(agent, ask_body(question, cid), room)
    if not sent.get("ok"):
        return no(f"ask not posted: {sent.get('reason')}")
    return wait_for_reply(transport, room, agent, sent.get("event_id"), asked_at, max_wait_s,
                          cid, clock=clock, sleep=sleep)
