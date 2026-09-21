#!/usr/bin/env python3
"""room-ops · mention — @-mention one or more agents reliably, by friendly handle.

The whole point: an agent should never hand-craft a peer's mxid (and get it wrong,
or forget it — the single most-repeated delivery failure). Give `mention` a handle
("qingyun-001", a label, or the name the room shows — "Bassil's Sutando") and a
message; it resolves the one canonical mxid — the live /v1/agents directory
first (narrowed to the room's members when it over-matches), then the broker's
room-scoped resolver, then the room's own member list — and posts an
op:message with those mxids leading the body plus `mentions:[mxid, ...]`.
The broker routes on the mxid either way: it stamps `mentions` into `m.mentions`,
and a member mxid in the plain body is matched as a whole token, auto-mentioned
and rendered as a pill — so the peer is actually triggered and a human reading
the room sees who was asked.

Multiple handles post ONE message with a real structured mention per resolved
peer — not N separate posts. This closes a gap `say` cannot: on this backend,
an agent-authored message is only ever routed to another agent via `m.mentions`
(`services/matrix-transaction-processor/intake/receiver.py`, `bridge_core.
mentioned_user_ids` — "a plain '@name' in the body does NOT mention"; the
body-text fallback in `addressed_agents` exists only for HUMAN senders). `say`
never sets `mentions` by design (see say.py), so writing several `@handle:
ag2.space` strings into one `say` body reads correctly to a human in scrollback
but creates zero real mentions for agent-to-agent addressing — the "tag the
fleet in one say" pattern this session used throughout never actually pinged
anyone through the structured channel. (Found live 2026-09-15: John's Sutando
wrote a correct room reply naming a peer in prose; the peer's own agent had no
task created for it, and tracing why led here.) `mention` with several handles
is the fix: it is the only room-ops call that can set `m.mentions` at all, so
multi-target notification has to go through it, not through `say`.

Ambiguity never posts: any handle that fails to resolve — unclear or not found
— refuses the WHOLE call before any network write, carrying every handle's own
outcome (not just the first), because mentioning the wrong agent, or only some
of the intended agents, is worse than mentioning nobody. `build_body` (pure) is
separated from the network post so the mention-construction is unit-tested
without a gateway.
"""
from __future__ import annotations

import os

from _gateway import gate_allows, load_gate, gateway, http_json, degrade_reason, HTTPError, URLError
from resolve import resolve_user, match_member, resolve_in_room, slug_handle, is_ambiguous
import receipt as _receipt
from relations import RelationError, relation_fields


def _result(ok, *, room_id=None, mxid=None, mxids=None, event_id=None, candidates=None,
            reason=None, resolved_by=None, failures=None):
    """`mxid` is the single resolved id (unchanged shape, every existing
    caller); `mxids` is always the full list, one-element for one handle
    too. `failures` (multi-handle only) is one entry per handle that did NOT
    resolve, so a refusal names every broken handle, not just the first."""
    return {"ok": bool(ok), "room_id": room_id, "mxid": mxid, "mxids": mxids or [],
            "event_id": event_id, "candidates": candidates or [], "reason": reason,
            "resolved_by": resolved_by, "failures": failures or []}


def build_body(mxids: "str | list[str]", message: str) -> str:
    """Compose the room message so the mention(s) actually trigger the peer(s).

    The mxid(s) LEAD the body, space-separated when there is more than one:
    `is_mention` matches a peer's localpart as a whole token, and leading
    mxids are unambiguous + read as a directed ask. An em dash separates them
    from the message when there is one.
    """
    if isinstance(mxids, str):
        mxids = [mxids]
    lead = " ".join(mxids)
    message = (message or "").strip()
    return f"{lead} — {message}" if message else lead


_UNREAD = object()   # the roster was not fetched yet (None = fetched and unreadable)


def _read_roster(room_id: str, agent_mxid: str | None) -> "list | None":
    """The room's members ({user_id, display_name, kind}); None when unreadable.

    `members` is imported lazily: it reaches the network, and the directory
    path must not pay for it. A failed import or read is None — "could not
    look", never "nobody is there" — so the caller keeps its own reason.
    """
    try:
        from members import room_members
    except ImportError:
        return None
    read = room_members(room_id, agent_mxid)
    if not read.get("ok"):
        return None
    return read.get("members") or []


def _narrow_to_room(res: dict, handle: str, roster: list) -> dict:
    """An ambiguous directory answer, narrowed to the candidates in the room.

    Measured live 2026-09-11: an owner with several agent identities has the
    directory over-match "Bassil's Sutando" against 13 stale `sutando-…` ids,
    while the room being posted to holds exactly one of them — and `mention`
    refused as "ambiguous — 13" although the broker and the roster both
    resolved it. Membership narrows, never widens: exactly one candidate
    present resolves (`resolved_by: "directory+room"`); two or more present
    stay a refusal carrying just those; none present leaves the directory's
    list untouched for the room-scoped sources to answer instead.
    """
    present = set()
    for m in roster or []:
        present.add(str((m.get("user_id") or m.get("id") or "") if isinstance(m, dict) else (m or "")))
    kept = [c for c in res.get("candidates") or [] if c in present]
    if len(kept) == 1:
        return {"ok": True, "mxid": kept[0], "candidates": [], "reason": None,
                "resolved_by": "directory+room"}
    if len(kept) > 1:
        return {**res, "candidates": kept, "resolved_by": "directory+room",
                "reason": f"ambiguous — {len(kept)} agents match {handle!r} in this room"}
    return {**res, "resolved_by": "directory"}


def _resolve_from_room(handle: str, room_id: str, agent_mxid: str | None,
                       *, roster=_UNREAD, roster_cache: "dict | None" = None) -> "dict | None":
    """Second chance for `handle` inside the target room: broker, then roster.

    (a) The broker's `op: resolve_user` sees the room's display names, so it is
        asked first — with the handle as given, then (on a plain miss) with the
        platform's localpart spelling of it (`slug_handle`), since the broker
        normalises nothing. A hit resolves. An AMBIGUOUS answer is returned as
        the refusal it is: a second source could only turn "too many" into a
        guess, so it is never widened.
    (b) When the gateway has no such op (older broker), the broker found
        nobody, or the network failed, the member list is read — unless the
        caller already read it (`roster`: the list, or None for a read that
        failed and is not retried) — and matched with its display names,
        agents preferred on a tie.

    The roster fetch stays LAZY — only reached when the broker could not
    answer (a) — even with `roster_cache` set: a multi-handle `mention()` call
    whose every handle resolves via the broker must never touch the roster.
    `roster_cache`, when given, is written the moment a lazy fetch actually
    happens (fetched or not: a `None` unreadable-roster is cached too), so a
    LATER handle that also needs this path reuses it instead of re-fetching.

    None when neither source could answer, so the caller keeps the directory's
    own reason rather than reporting a membership miss that never happened.
    """
    got = resolve_in_room(handle, room_id)
    if not got.get("ok") and not is_ambiguous(got) and not got.get("unsupported"):
        slug = slug_handle(handle)
        # The broker matches case-insensitively, so a case-only difference is
        # the same query and not worth a round trip.
        if slug and slug != (handle or "").strip().casefold():
            got = resolve_in_room(slug, room_id)
    if got.get("ok"):
        return {**got, "resolved_by": "broker"}
    if is_ambiguous(got):
        return {**got, "resolved_by": "broker"}

    if roster is _UNREAD:
        roster = _read_roster(room_id, agent_mxid)
        if roster_cache is not None:
            roster_cache["roster"] = roster
    if roster is None:
        return None
    hit = match_member(handle, roster, prefer_agents=True)
    if hit.get("ok") or hit.get("candidates"):
        return {**hit, "resolved_by": "room"}
    return None


def _resolve_one(handle: str, room_id: str, agent_mxid: str | None, agents: list | None,
                  roster_cache: dict) -> tuple[dict, str]:
    """Resolve one handle -> (res, source), same pipeline `mention` always used:
    directory (narrowed to the room on over-match) -> broker -> room roster.

    `roster_cache` is a single mutable {} shared across every handle in one
    `mention()` call, keyed "roster" once a fetch has actually happened for
    ANY handle — a multi-handle mention that needs the room member list reads
    it at most once, not once per handle, and a handle whose resolution never
    reaches the roster (directory hit, or broker hit) never triggers a fetch
    at all, exactly as a single-handle `mention()` always has.
    """
    res = resolve_user(handle, agents=agents)
    # A full mxid counts as the directory's answer: resolve_user short-circuits it.
    source = "directory"
    if not res.get("ok") and res.get("candidates"):
        # Too many in the directory: who is actually in the room decides,
        # and the one roster read also serves the fallback below.
        if "roster" not in roster_cache:
            roster_cache["roster"] = _read_roster(room_id, agent_mxid)
        roster = roster_cache["roster"]
        if roster is not None:
            res = _narrow_to_room(res, handle, roster)
            source = res["resolved_by"]
    if not res.get("ok"):
        # /v1/agents lists only this account's own agents, so a peer agent in
        # the room resolves nowhere and the mention would be unreachable.
        room_res = _resolve_from_room(handle, room_id, agent_mxid,
                                       roster=roster_cache.get("roster", _UNREAD),
                                       roster_cache=roster_cache)
        if room_res is not None:
            res, source = room_res, room_res.get("resolved_by")
    return res, source


def mention(handle: "str | list[str]", message: str, room_id: str,
            agent_mxid: str | None = None, *, gate=None, agents: list | None = None,
            reply_to: str | None = None) -> dict:
    """Resolve one or more `handle`s -> mxids and post ONE triggering @-mention
    message into `room_id`, carrying every resolved mxid in `m.mentions`.

    `handle` is a single string (unchanged, single-target shape) or a list of
    strings (post-once, mention-many). Returns {ok, room_id, mxid, mxids,
    event_id, candidates, reason, resolved_by, failures}. `mxid`/`candidates`/
    `reason`/`resolved_by` describe the single handle, or (multi-handle) the
    FIRST handle that failed — kept for every caller written against the old
    single-handle shape; `failures` carries EVERY failed handle's own outcome,
    checked by a caller that wants to report more than one bad handle at once.

    On ANY unresolved or ambiguous handle it does NOT post — nothing, not even
    the handles that did resolve — because a partial mention silently drops an
    intended recipient, which is worse than mentioning nobody and being asked
    to fix the handle.
    """
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    if not room_id:
        return _result(False, room_id=room_id, reason="room_id required")
    handles = [handle] if isinstance(handle, str) else list(handle or [])
    handles = [h for h in handles if h]
    if not handles:
        return _result(False, room_id=room_id, reason="handle required")

    # Validated before resolve/gate/network for the same reason as in `say`: a
    # mention citing the wrong event is worse than one that is refused.
    try:
        rel = relation_fields(reply_to=reply_to)
    except RelationError as e:
        return _result(False, room_id=room_id, reason=str(e))

    roster_cache: dict = {}
    resolved: list[tuple[str, str, str]] = []   # (handle, mxid, source)
    failures: list[dict] = []
    for h in handles:
        res, source = _resolve_one(h, room_id, agent_mxid, agents, roster_cache)
        if res.get("ok"):
            resolved.append((h, res["mxid"], source))
        else:
            failures.append({"handle": h, "candidates": res.get("candidates") or [],
                             "reason": res.get("reason") or "could not resolve handle",
                             "resolved_by": source})

    if failures:
        first = failures[0]
        return _result(False, room_id=room_id, candidates=first["candidates"],
                       reason=first["reason"], resolved_by=first["resolved_by"],
                       failures=failures)

    mxids = [mxid for _h, mxid, _s in resolved]
    mxid = mxids[0]
    source = resolved[0][2]

    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, room_id=room_id, mxid=mxid, mxids=mxids, resolved_by=source,
                       reason=f"client gate denied for {agent_mxid}")

    base, headers = gateway()
    if not base:
        return _result(False, room_id=room_id, mxid=mxid, mxids=mxids, resolved_by=source,
                       reason="no gateway configured")

    body = build_body(mxids, message)
    try:
        # `mentions` -> m.mentions; the mxid(s) LEADING `body` are the routing
        # token AND the literal pills for humans — never move them.
        cid = os.environ.get("SUTANDO_WORKER_SEAT") or os.environ.get("SUTANDO_CORE_ID")
        worker = os.environ.get("SUTANDO_WORKER_ID") or (f"worker-{cid}" if cid else None)
        _color = (os.environ.get("SUTANDO_WORKER_ACCENT")
                  or os.environ.get("SUTANDO_WORKER_COLOR"))  # COLOR: one-release alias
        _stripe = os.environ.get("SUTANDO_WORKER_STRIPE")
        _attn = os.environ.get("SUTANDO_WORKER_ATTENTION") == "1"
        _style = os.environ.get("SUTANDO_WORKER_STYLE")
        _styles = ("stripe", "highlight", "none")
        _w = ({"id": worker,
               **({"color": _color} if _color else {}),
               **({"stripe": _stripe != "0"} if _stripe in ("0", "1") else {}),
               **({"style": _style} if _style in _styles else {}),
               **({"attention": True} if _attn else {})}
              if worker else None)
        stamp = {"extra_content": {"space.ag2.worker": _w}} if _w else {}
        _status, parsed = http_json(
            "POST", f"{base}/v1/room", headers,
            {"op": "message", "room_id": room_id, "body": body, "mentions": mxids, **rel, **stamp},
        )
    except HTTPError as e:
        return _result(False, room_id=room_id, mxid=mxid, mxids=mxids, resolved_by=source,
                       reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, room_id=room_id, mxid=mxid, mxids=mxids, resolved_by=source,
                       reason=f"network error: {e}")
    # Same envelope as `say`, so the same reading — see receipt.py.
    _state, event_id, _reason = _receipt.classify(parsed)
    return _result(True, room_id=room_id, mxid=mxid, mxids=mxids, event_id=event_id,
                   resolved_by=source)
