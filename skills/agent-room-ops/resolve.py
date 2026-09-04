#!/usr/bin/env python3
"""room-ops · resolve — map a friendly handle to an agent's mxid via GET /v1/agents.

The recurring failure mode for an agent that wants to @-mention a peer is not the
trigger (the broker's `is_mention` matches the peer's localpart as a whole token,
so a plain-text mxid in the body already fires) — it's *hand-crafting the mxid*
and getting it wrong or forgetting it. This module removes the hand-craft: give it
a friendly handle ("qingyun-001", a label, or a full mxid) and it returns the one
canonical mxid from the live directory, or the candidate set when ambiguous.

Pure matching (`match_agent`) is separated from the network fetch so it is unit-
testable without a gateway.
"""
from __future__ import annotations

import json
import os

from _gateway import gateway, http_request, degrade_reason, HTTPError, URLError


def _localpart(mxid: str) -> str:
    """@sutando-qingyun-001:ag2.space -> sutando-qingyun-001."""
    return (mxid or "").split(":", 1)[0].lstrip("@")


def _is_mxid(q: str) -> bool:
    return q.startswith("@") and ":" in q


def match_agent(query: str, agents: list) -> dict:
    """Resolve `query` to a single agent mxid from `agents` (list of {id, label,…}).

    Ranking, best first: exact localpart → exact label → substring localpart →
    substring label. A single winner at the best populated tier resolves; a tie at
    that tier is reported as `candidates` (ambiguous, caller disambiguates). Pure —
    no I/O — so the ranking is unit-tested directly.
    """
    q = (query or "").strip()
    if not q:
        return {"ok": False, "mxid": None, "candidates": [], "reason": "empty query"}
    # A full mxid is already resolved — trust it (still normalise via the directory
    # if present, but never fail just because the directory is stale/unreachable).
    if _is_mxid(q):
        return {"ok": True, "mxid": q, "candidates": [], "reason": "already an mxid"}

    ql = q.lstrip("@").lower()
    exact_local, exact_label, sub_local, sub_label = [], [], [], []
    for a in agents or []:
        mxid = a.get("id") or ""
        if not mxid:
            continue
        lp = _localpart(mxid).lower()
        label = (a.get("label") or "").lower()
        if lp == ql:
            exact_local.append(mxid)
        elif label and label == ql:
            exact_label.append(mxid)
        elif ql in lp:
            sub_local.append(mxid)
        elif label and ql in label:
            sub_label.append(mxid)

    for tier in (exact_local, exact_label, sub_local, sub_label):
        # De-dup while preserving order (an agent can't match two tiers, but a
        # directory could list a dup id).
        uniq = list(dict.fromkeys(tier))
        if len(uniq) == 1:
            return {"ok": True, "mxid": uniq[0], "candidates": [], "reason": None}
        if len(uniq) > 1:
            return {"ok": False, "mxid": None, "candidates": uniq,
                    "reason": f"ambiguous — {len(uniq)} agents match {query!r}"}
    return {"ok": False, "mxid": None, "candidates": [], "reason": f"no agent matches {query!r}"}


def match_member(query: str, member_mxids: list) -> dict:
    """Resolve `query` against a room's MEMBER mxids, same ranking as match_agent.

    Members carry no label, so only the localpart tiers apply. Scoping to one
    room is what makes this safe to fall back to: a handle can only resolve to
    someone already in the room being posted to.
    """
    agents = [{"id": m} for m in (member_mxids or []) if m]
    return match_agent(query, agents)


def list_agents() -> dict:
    """GET /v1/agents → {"ok", "agents": [...], "reason"}. Graceful on any failure."""
    base, headers = gateway()
    if not base:
        return {"ok": False, "agents": [], "reason": "no gateway configured"}
    try:
        _, body, _h = http_request("GET", f"{base}/v1/agents", headers)
    except HTTPError as e:
        return {"ok": False, "agents": [], "reason": degrade_reason(e.code)}
    except (URLError, TimeoutError) as e:
        return {"ok": False, "agents": [], "reason": f"network error: {e}"}
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
    except ValueError as e:
        return {"ok": False, "agents": [], "reason": f"parse error: {e}"}
    agents = parsed.get("agents") if isinstance(parsed, dict) else parsed
    return {"ok": True, "agents": agents or [], "reason": None}


def resolve_user(query: str, *, agents: list | None = None) -> dict:
    """Resolve a friendly handle to a single agent mxid.

    Returns {"ok", "mxid", "candidates", "reason"}. Pass `agents` to skip the
    network fetch (tests / batch). A full mxid short-circuits without a fetch.
    """
    if _is_mxid((query or "").strip()):
        return match_agent(query, agents or [])
    if agents is None:
        got = list_agents()
        if not got["ok"]:
            return {"ok": False, "mxid": None, "candidates": [], "reason": got["reason"]}
        agents = got["agents"]
    return match_agent(query, agents)
