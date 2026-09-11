#!/usr/bin/env python3
"""room-ops · resolve — map a friendly handle to one mxid: directory, broker, room.

The recurring failure mode for an agent that wants to @-mention a peer is not
the trigger — the broker routes on the mxid: it stamps `op:message` `mentions`
into `m.mentions`, and a member mxid written in the plain body is matched as a
whole token, auto-mentioned and rendered as a pill for humans — it is
*producing the mxid*. Hand-crafting it and getting it wrong is one half; the
other is naming the peer the way the room shows it ("Bassil's Sutando") and
having no tool that turns that into `@bassil-bassil-s-sutando.agent:ag2.space`.
Verified in code 2026-09-10: the directory (`/v1/agents`) lists only this
account's own agents, the old matcher compared raw lowercase strings, and the
room fallback threw the display names away — so a peer named by its display
name resolved nowhere, callers fell back to `say` (which pings nobody), and the
peer, which ignores agent-authored messages that do not mention it, never saw
the hand-off.

Three sources, one answer shape `{ok, mxid, candidates, reason}`:

  - `match_agent` / `match_member` — pure, tiered matching over
    `{id, label?, display_name?}` entries, both sides run through
    `normalize_handle` so a possessive, a curly apostrophe, spaces/underscores
    and case never decide a miss. Tiers, best first: exact localpart → exact
    label/display name → token-prefix ("bassil-sutando" ~ "bassil-s-sutando")
    → substring of the localpart → substring of the name. One winner at the
    best populated tier resolves; a tie is reported as `candidates` — the
    caller disambiguates rather than mentioning the wrong one. `prefer_agents`
    breaks a tie only within a tier and only when exactly one candidate is
    agent-kind, so an exact human name still beats an agent's looser match.
  - `resolve_in_room` — the broker's own `op: resolve_user`, scoped to a room
    (exact mxid / localpart / display name, then substring; it normalises
    nothing, hence `slug_handle` for a second try with the platform's
    localpart spelling of a name). It says "nobody" with an HTTP 404 that
    carries a JSON error (measured live 2026-09-11), which `op_unsupported`
    tells apart from a gateway that has no such op.
  - `resolve_user` — the `/v1/agents` directory, the unchanged entry point.

Pure matching and response parsing are separated from the network so both are
unit-tested without a gateway.
"""
from __future__ import annotations

import json
import re
import unicodedata

from _gateway import (gateway, http_request, http_json, degrade_reason, degrade_reason_parsed,
                      HTTPError, URLError)

# Apostrophe look-alikes folded to U+0027 — the twin of `normalizeMentionText` in the
# web client (cinny src/app/components/editor/autocomplete/mentionCandidates.ts).
_APOSTROPHES = str.maketrans({
    "\u2019": "'", "\u2018": "'", "\u201b": "'",  # right, left, reversed single quotes
    "\u02bc": "'", "\u2032": "'",                 # modifier-letter apostrophe, prime
    "`": "'", "\u00b4": "'",
})
# A possessive "'s" ending a token ("bassil's sutando" names Bassil's agent), anchored
# to the token end so an "s" inside a word stays ("o'neil").
_POSSESSIVE_RE = re.compile(r"'s(?=$|[\s_\-])")
_SEPARATORS_RE = re.compile(r"[\s_\-]+")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MXID_RE = re.compile(r"@[^\s,:]+:[^\s,]+")


def _localpart(mxid: str) -> str:
    """@sutando-qingyun-001:ag2.space -> sutando-qingyun-001."""
    return (mxid or "").split(":", 1)[0].lstrip("@")


def _is_mxid(q: str) -> bool:
    return q.startswith("@") and ":" in q


def normalize_handle(q: str) -> str:
    """Canonical comparison form of a handle, label or display name.

    Apostrophe look-alikes to "'" → NFKC → strip → drop a leading "@" →
    casefold → drop a trailing possessive "'s" on any token → drop remaining
    apostrophes → runs of whitespace / "_" / "-" become one "-" → strip "-".
    So "Bassil's Sutando", "bassil sutando", "bassil’s sutando" and
    "@Bassil's Sutando" are all "bassil-sutando", and a localpart such as
    "sutando-qingyun-001" is its own normal form.

    The look-alikes are U+2019 ’, U+2018 ‘, U+201B ‛, U+02BC ʼ, U+2032 ′,
    U+0060 ` and U+00B4 ´ — the same set the web client's
    `normalizeMentionText` folds (cinny `mentionCandidates.ts`), so a handle
    typed in either place lands on one spelling; extend both together. The map
    runs FIRST because NFKC decomposes U+00B4 into a space plus a combining
    mark, after which it is no longer one character to map.
    """
    s = (q or "").translate(_APOSTROPHES)
    s = unicodedata.normalize("NFKC", s).strip().lstrip("@").casefold()
    s = _POSSESSIVE_RE.sub("", s)
    s = s.replace("'", "")
    return _SEPARATORS_RE.sub("-", s).strip("-")


def slug_handle(q: str) -> str:
    """The platform's localpart spelling of a name, as a best-effort guess.

    Registration turns every run of non-alphanumerics into one "-", so the
    possessive's "s" survives as its own token: "Bassil's Sutando" →
    "bassil-s-sutando", "Susan's bot" → "susan-s-bot". A wrong guess is a miss,
    never a wrong hit — the broker still has to find a member spelled that way.
    """
    s = unicodedata.normalize("NFKC", q or "").casefold()
    return _NON_SLUG_RE.sub("-", s).strip("-")


def _tokens(s: str) -> list:
    return [t for t in s.split("-") if t]


def _token_prefix(qn: str, cand: str) -> bool:
    """Each query token, in order, is a prefix of a later candidate token.

    "bassil-sutando" ~ "bassil-s-sutando": the platform keeps the possessive's
    "s" as a token that `normalize_handle` drops from the query, and the
    `.agent` suffix rides the last token ("sutando" ~ "sutando.agent").
    Earliest-match is optimal here: taking a later token never helps an
    earlier query token, so no backtracking is needed.
    """
    qt, ct = _tokens(qn), _tokens(cand)
    if not qt or not ct:
        return False
    i = 0
    for t in qt:
        while i < len(ct) and not ct[i].startswith(t):
            i += 1
        if i == len(ct):
            return False
        i += 1
    return True


def _is_agent(mxid: str) -> bool:
    """members.classify_member, imported lazily: `members` is the module that
    reaches the network, and `resolve` must stay importable (and cheap) without
    it. Unknown reads as "not an agent" — the tie then stays a tie, which is the
    safe side (a refusal, never a wrong pick)."""
    try:
        from members import classify_member
    except ImportError:
        return False
    return classify_member(mxid) == "agent"


def _names(entry: dict) -> list:
    """The human-facing names an entry carries — a directory `label`, a member
    `display_name`, or both — normalised, empties dropped."""
    out = []
    for key in ("label", "display_name"):
        n = normalize_handle(str(entry.get(key) or ""))
        if n and n not in out:
            out.append(n)
    return out


def match_agent(query: str, agents: list, *, prefer_agents: bool = False) -> dict:
    """Resolve `query` to a single mxid from `agents` ({id, label?, display_name?}).

    Ranking, best first: exact localpart → exact label/display name →
    token-prefix of either → substring of the localpart → substring of a name,
    every side through `normalize_handle`. A single winner at the best
    populated tier resolves; a tie there is reported as `candidates`
    (ambiguous, caller disambiguates) — unless `prefer_agents` and exactly one
    of the tied entries is agent-kind, which is the tie a person and their
    agent sharing a name produce. Pure — no I/O — so the ranking is unit-tested
    directly.
    """
    q = (query or "").strip()
    if not q:
        return {"ok": False, "mxid": None, "candidates": [], "reason": "empty query"}
    # A full mxid is already resolved — trust it (still normalise via the directory
    # if present, but never fail just because the directory is stale/unreachable).
    if _is_mxid(q):
        return {"ok": True, "mxid": q, "candidates": [], "reason": "already an mxid"}
    qn = normalize_handle(q)
    if not qn:
        return {"ok": False, "mxid": None, "candidates": [], "reason": f"no agent matches {query!r}"}

    # exact localpart, exact name, token-prefix, substring localpart, substring name
    tiers = ([], [], [], [], [])
    for a in agents or []:
        mxid = (a.get("id") or "") if isinstance(a, dict) else ""
        if not mxid:
            continue
        lp = normalize_handle(_localpart(mxid))
        names = _names(a)
        if lp == qn:
            tiers[0].append(mxid)
        elif qn in names:
            tiers[1].append(mxid)
        elif _token_prefix(qn, lp) or any(_token_prefix(qn, n) for n in names):
            tiers[2].append(mxid)
        elif qn in lp:
            tiers[3].append(mxid)
        elif any(qn in n for n in names):
            tiers[4].append(mxid)

    for tier in tiers:
        # De-dup while preserving order (an agent can't match two tiers, but a
        # directory could list a dup id).
        uniq = list(dict.fromkeys(tier))
        if len(uniq) > 1 and prefer_agents:
            agent_kind = [m for m in uniq if _is_agent(m)]
            if len(agent_kind) == 1:
                uniq = agent_kind
        if len(uniq) == 1:
            return {"ok": True, "mxid": uniq[0], "candidates": [], "reason": None}
        if len(uniq) > 1:
            return {"ok": False, "mxid": None, "candidates": uniq,
                    "reason": f"ambiguous — {len(uniq)} agents match {query!r}"}
    return {"ok": False, "mxid": None, "candidates": [], "reason": f"no agent matches {query!r}"}


def match_member(query: str, members: list, *, prefer_agents: bool = True) -> dict:
    """Resolve `query` against a room's MEMBERS, same ranking as match_agent.

    `members` is what `members.room_members` returns ({user_id, display_name,
    kind}) or a bare list of mxids (the older callers); both become
    {id, display_name} entries so the name tiers apply — a peer is usually
    named the way the room shows it, not by its localpart. Scoping to one room
    is what makes this safe to fall back to: a handle can only resolve to
    someone already in the room being posted to. Agents are preferred on a tie
    because a mention is a hand-off: when "Alex Sutando" is both a person and
    their agent, the agent is the one that acts on it.
    """
    entries = []
    for m in members or []:
        if isinstance(m, dict):
            mxid = m.get("user_id") or m.get("id") or ""
            name = m.get("display_name") or m.get("label") or ""
        else:
            mxid, name = (m or ""), ""
        if mxid:
            entries.append({"id": str(mxid), "display_name": str(name)})
    return match_agent(query, entries, prefer_agents=prefer_agents)


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


def parse_resolve_user_response(res) -> dict:
    """Pure reading of an `op: resolve_user` body → {ok, mxid, display_name, candidates, reason}.

    The broker's three answers (verified against matrix_ingest.py, 2026-09-10):
    a hit is `{"mxid", "display_name"}`; too many is
    `{"error": "ambiguous: @a:hs, @b:hs"}` — the mxids after the colon become
    `candidates`; nobody is `{"error": "... not found"}`. Anything else is a
    malformed answer, reported as a miss with a reason — never as a hit.
    """
    empty = {"ok": False, "mxid": None, "display_name": "", "candidates": []}
    if not isinstance(res, dict):
        return {**empty, "reason": "malformed gateway response"}
    mxid = res.get("mxid")
    if isinstance(mxid, str) and _is_mxid(mxid.strip()):
        return {"ok": True, "mxid": mxid.strip(),
                "display_name": str(res.get("display_name") or ""),
                "candidates": [], "reason": None}
    err = res.get("error")
    if err is None and res.get("ok") is False:
        err = res.get("reason") or "gateway declined"
    if isinstance(err, str) and err.strip():
        text = err.strip()
        cands = _MXID_RE.findall(text) if text.lower().startswith("ambiguous") else []
        return {**empty, "candidates": list(dict.fromkeys(cands)), "reason": text}
    return {**empty, "reason": "malformed gateway response"}


def is_ambiguous(res: dict) -> bool:
    """True when a resolver answer names too many — the answer to refuse on.

    `candidates` is the normal carrier; the reason prefix is the fallback for a
    broker that says "ambiguous" without listing whom, so a format drift there
    degrades to a refusal and never to a widened search.
    """
    return bool(res.get("candidates")) or str(res.get("reason") or "").lower().startswith("ambiguous")


_UNKNOWN_OP_RE = re.compile(r"unknown\s+op", re.IGNORECASE)


def _error_body(err):
    """The JSON an HTTPError's body carries; None when it is empty or not JSON.

    The body is single-shot, so it is read here once and the parsed value
    feeds both the reason and `op_unsupported` — an empty body must stay
    distinguishable from a JSON `{}`, which is why "" is None and not `{}`.
    """
    try:
        raw = err.read().decode("utf-8").strip()
        return json.loads(raw) if raw else None
    except Exception:
        return None


def op_unsupported(code: int, parsed, reason: str) -> bool:
    """True only when the gateway has no `resolve_user` op at all.

    Measured live 2026-09-11: the broker answers a MISS with HTTP 404 and a
    JSON body — `{"error": "no member matching 'x' in this room — not found"}`
    — so a 404 is not "no such verb" by status alone. Every 404 used to read
    as unsupported, which skipped the caller's `slug_handle` retry on every
    real miss and misreported the broker. The op is missing when a 404/405
    carries no JSON body at all (a router with no route answers bare), or
    when the server names it: an "unknown op" error at 400, 404 or 405. Any
    other 404/405 with a JSON body is the op answering, and reads as a miss.
    """
    if code in (400, 404, 405) and _UNKNOWN_OP_RE.search(reason or ""):
        return True
    return code in (404, 405) and parsed is None


def resolve_in_room(query: str, room_id: str) -> dict:
    """POST /v1/room {op: resolve_user} → {ok, mxid, display_name, candidates, reason, unsupported}.

    The broker resolves within the room's own roster, display names included,
    so this is the first thing to ask when the directory does not know a
    handle. `unsupported` is True when the gateway has no such op — a bare
    404/405, or an "unknown op" error (see `op_unsupported`) — which tells the
    caller to fall back to the member list rather than to report a miss nobody
    measured. A 404 that carries the broker's JSON is its own miss (or its
    "ambiguous"), read exactly like a 200 answer so the candidates still carry.
    Network/HTTP failures are `ok: False` with a reason and never raise.
    """
    out = {"ok": False, "mxid": None, "display_name": "", "candidates": [], "reason": None,
           "unsupported": False}
    q = (query or "").strip()
    if not q:
        return {**out, "reason": "empty query"}
    if not room_id:
        return {**out, "reason": "room_id required"}
    base, headers = gateway()
    if not base:
        return {**out, "reason": "no gateway configured"}
    try:
        _status, parsed = http_json("POST", f"{base}/v1/room", headers,
                                    {"op": "resolve_user", "room_id": room_id, "query": q})
    except HTTPError as e:
        parsed = _error_body(e)
        reason = degrade_reason_parsed(e.code, parsed)
        if op_unsupported(e.code, parsed, reason):
            return {**out, "reason": reason, "unsupported": True}
        if e.code in (404, 405) and parsed is not None:
            answer = parse_resolve_user_response(parsed)
            if answer["ok"]:
                # An mxid under an error status is a contradiction, never a resolve.
                return {**out, "reason": f"malformed gateway response (HTTP {e.code} with an mxid)"}
            return {**out, **answer}
        return {**out, "reason": reason}
    except (URLError, TimeoutError) as e:
        return {**out, "reason": f"network error: {e}"}
    except ValueError as e:
        return {**out, "reason": f"parse error: {e}"}
    return {**out, **parse_resolve_user_response(parsed)}
