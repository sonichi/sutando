#!/usr/bin/env python3
"""owner-agent-consult policy: config, the owner-only room guard, roster, tier gate,
one-hop marker and the bounded wait. Pure over an injected transport, so the rules
are testable without a gateway; consult.py supplies the room-ops transport.

The transport is any object with:
  members(room) -> {ok, members: [{user_id, display_name, kind}], reason}
  agents()      -> {ok, agents: [{id, owner, ...}], reason}   (this account's agents)
  mention(mxid, body, room) -> {ok, event_id, reason}
  read(room, limit) -> {ok, messages: [{sender, body, event_id, ts}] newest first, reason}

Identity is never configured here: the self mxid comes from the caller, the owner from
the gateway's agent registry, and the roster from the consult room's membership.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

SKILL_DIR = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
_SRC = SKILL_DIR.parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# The one definition of the one-hop marker; a task or question carrying it is never consulted onward.
MARKER = "[owner-agent-consult:v1]"

CONFIG_ENABLED = "OWNER_AGENT_CONSULT_ENABLED"
CONFIG_ROOM = "OWNER_AGENT_CONSULT_ROOM"
CONFIG_MAX_WAIT = "OWNER_AGENT_CONSULT_MAX_WAIT_S"
DEFAULT_MAX_WAIT_S = 120
MAX_WAIT_CEILING_S = 600
POLL_INTERVAL_S = 5.0
READ_LIMIT = 30
_FALSE = ("0", "false", "no", "off")
_MXID = re.compile(r"^@[^:\s]+:[^\s]+$")


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


def settings(room=None, max_wait=None, environ=None, manifest_cfg=None) -> dict:
    """{active, room, max_wait_s, reason}; inactive (inert) when disabled or no room is set."""
    get = lambda k, c=None: config_value(k, c, environ, manifest_cfg)  # noqa: E731
    try:
        wait = int(float(get(CONFIG_MAX_WAIT, max_wait) or DEFAULT_MAX_WAIT_S))
    except ValueError:
        wait = DEFAULT_MAX_WAIT_S
    wait = max(1, min(wait, MAX_WAIT_CEILING_S))
    room_id = get(CONFIG_ROOM, room)
    if get(CONFIG_ENABLED).lower() in _FALSE:
        return {"active": False, "room": room_id, "max_wait_s": wait,
                "reason": f"disabled ({CONFIG_ENABLED} is off); the skill is inert"}
    if not room_id:
        return {"active": False, "room": "", "max_wait_s": wait,
                "reason": f"no consult room configured ({CONFIG_ROOM} unset); the skill is inert"}
    return {"active": True, "room": room_id, "max_wait_s": wait, "reason": None}


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


def guard(room: str, self_mxid: str, transport) -> dict:
    """Fail closed. Admit the room only when every joined member is this agent, the
    owner, or an agent the registry binds to the same owner. Anything else is named."""
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
    members = [m for m in got.get("members") or [] if isinstance(m, dict) and m.get("user_id")]
    ids = {str(m["user_id"]) for m in members}
    if self_mxid not in ids:
        return {"ok": False, "reason": "this agent is not a member of the consult room"}
    strangers = []
    for m in members:
        uid = str(m["user_id"])
        if uid in (self_mxid, who["owner"]) or uid in who["agents"]:
            continue
        what = "agent of another owner" if m.get("kind") == "agent" else "human who is not the owner"
        strangers.append(f"{uid} ({what})")
    if strangers:
        return {"ok": False, "reason": "consult room is not owner-only: " + ", ".join(sorted(strangers))}
    return {"ok": True, "owner": who["owner"], "owner_agents": who["agents"],
            "members": members, "reason": None}


def roster(verdict: dict, self_mxid: str, ents=(), quick=None) -> List[dict]:
    """The owner's other agents present in the consult room (agent kind per the registry,
    not the naming heuristic), with the domains the collaboration map records for each."""
    out = []
    for m in verdict.get("members") or []:
        uid = str(m.get("user_id"))
        if uid == self_mxid or uid not in verdict.get("owner_agents", ()):
            continue
        out.append({"mxid": uid, "display_name": m.get("display_name") or "",
                    "domains": domains_for(uid, ents, quick)})
    return out


# The collaboration-intelligence map: which agent holds which domain
def _fact_text(fact) -> str:
    if isinstance(fact, dict):
        if str(fact.get("status") or "") in ("disputed", "superseded"):
            return ""
        return str(fact.get("value") or "")
    return str(fact or "")


def domains_for(mxid: str, ents=(), quick=None) -> List[str]:
    ids = set()
    found = []
    for e in ents or ():
        if not isinstance(e, dict):
            continue
        idents = e.get("identities") or []
        if any(str(i.get("user_id") or i.get("provider_id") or "") == mxid
               for i in idents if isinstance(i, dict)):
            ids.add(str(e.get("entity_id") or ""))
            for field in ("expertise", "responsibilities", "roles"):
                found += [t for t in map(_fact_text, e.get(field) or []) if t]
    for r in ((quick or {}).get("recent_entities") or []):
        if isinstance(r, dict) and (str(r.get("entity_id") or "") in ids - {""}
                                    or str(r.get("agent_mxid") or "") == mxid):
            if r.get("one_line"):
                found.append(str(r["one_line"]))
    return list(dict.fromkeys(found))


def holds_domain(domains: List[str], domain: str) -> bool:
    d = (domain or "").strip().lower()
    return bool(d) and any(d in x.lower() for x in domains)


# Tier gate and one-hop marker
def task_gate(task_text: str, workspace: Optional[Path] = None) -> dict:
    """Owner-originated only, read through the attested header path; a missing tier
    is refused here, unlike the general dispatch default."""
    from local_task_protocol import canonical_access_tier
    from task_envelope import attested_task_headers
    headers = attested_task_headers(task_text, workspace).headers
    tier = canonical_access_tier(headers.get("access_tier"))
    if tier != "owner":
        return {"ok": False, "reason": f"task access_tier is {tier or 'absent'!r}, not owner"}
    if str(headers.get("collaborator") or "").strip().lower() == "true":
        return {"ok": False, "reason": "task is a collaborator task, not the owner's"}
    if MARKER in task_text:
        return {"ok": False, "reason": "task is itself a consult; answer it, never consult onward"}
    return {"ok": True, "reason": None}


def ask_body(question: str) -> str:
    return (f"{MARKER} {question.strip()}\n\n"
            "Answer here from what you hold. This is a one-hop consult: do not consult another agent.")


# Bounded wait
def _ts_ms(ts) -> Optional[float]:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return float(ts) if ts > 1e12 else float(ts) * 1000.0


def replies_after(messages: List[dict], agent: str, ask_event: Optional[str],
                  asked_at_ms: float) -> List[dict]:
    """Messages from `agent` newer than the ask (newest-first input). Bounded by the
    ask's event id when it is in the window, else by the ask's send time."""
    newer = []
    seen_ask = False
    for m in messages or []:
        if ask_event and m.get("event_id") == ask_event:
            seen_ask = True
            break
        newer.append(m)
    if not seen_ask:
        newer = [m for m in newer if (_ts_ms(m.get("ts")) or 0) >= asked_at_ms]
    hits = [m for m in newer if m.get("sender") == agent
            and isinstance(m.get("body"), str) and m["body"].strip()]
    return list(reversed(hits))


def wait_for_reply(transport, room: str, agent: str, ask_event: Optional[str], asked_at_ms: float,
                   max_wait_s: float, clock: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep,
                   interval: float = POLL_INTERVAL_S) -> dict:
    deadline = clock() + max_wait_s
    last_reason = None
    while True:
        got = transport.read(room, READ_LIMIT)
        if got.get("ok"):
            hits = replies_after(got.get("messages") or [], agent, ask_event, asked_at_ms)
            if hits:
                return {"answered": True, "agent": agent,
                        "reply_text": "\n\n".join(h["body"].strip() for h in hits),
                        "event_ids": [h.get("event_id") for h in hits]}
        else:
            last_reason = got.get("reason")
        left = deadline - clock()
        if left <= 0:
            break
        sleep(min(interval, left))
    reason = f"no reply from {agent} within {int(max_wait_s)}s"
    if last_reason:
        reason += f" (last read error: {last_reason})"
    return {"answered": False, "agent": agent, "reason": reason}


def consult(transport, *, room: str, self_mxid: str, agent: str, domain: str, question: str,
            task_text: str, max_wait_s: float, ents=(), quick=None,
            workspace: Optional[Path] = None, now_ms: Optional[Callable[[], float]] = None,
            clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep) -> dict:
    """Every gate before the one post; returns {answered, agent, reply_text} or
    {answered: False, reason}. Nothing is posted unless all gates pass."""
    def no(reason):
        return {"answered": False, "agent": agent, "reason": reason}
    gate = task_gate(task_text, workspace)
    if not gate["ok"]:
        return no(gate["reason"])
    if not question or not question.strip():
        return no("empty question")
    if MARKER in question:
        return no("question carries the consult marker; consults are one hop")
    verdict = guard(room, self_mxid, transport)
    if not verdict["ok"]:
        return no(verdict["reason"])
    if agent not in {r["mxid"] for r in roster(verdict, self_mxid)}:
        return no(f"{agent} is not one of the owner's other agents in the consult room")
    domains = domains_for(agent, ents, quick)
    if not holds_domain(domains, domain):
        return no(f"the collaboration map does not record {agent} as holding {domain!r}")
    asked_at = (now_ms or (lambda: time.time() * 1000.0))()
    sent = transport.mention(agent, ask_body(question), room)
    if not sent.get("ok"):
        return no(f"ask not posted: {sent.get('reason')}")
    return wait_for_reply(transport, room, agent, sent.get("event_id"), asked_at, max_wait_s,
                          clock=clock, sleep=sleep)
