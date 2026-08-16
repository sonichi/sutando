#!/usr/bin/env python3
"""room-ops · read — pull recent room/channel history for an agent.

A synchronous pull-on-demand read, orthogonal to the task file bridge. Gateway-only
(the generic verb `GET {GATEWAY}/v1/rooms/{room}/messages`); membership is enforced
gateway-side. See _gateway.py for the shared boundary + gate.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from _gateway import (gate_allows, load_gate, gateway, http_request, degrade_reason,
                    quote, urlencode, HTTPError, URLError)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# How many times the raw-event window may widen before we stop. Small and FIXED so the
# widening below is a bounded `for` rather than a resident loop: the room-ops skill bans
# new ones outright (tests/events-plane-boundary.test.py freezes the allowlist to two
# grandfathered entries and shrinks it monotonically). The widening was always finite —
# it is capped by MAX_LIMIT — so the bounded ladder is the honest shape too, not merely
# the one that satisfies the gate.
#
# NB the guard regexes every LINE, comments included, so spelling the banned construct
# out here — even inside backticks — would itself trip it.
_MAX_WIDENINGS = 6


def _windows(start, cap):
    """Raw-event window sizes to try, smallest first. Precomputed and bounded."""
    out, raw = [], max(1, min(int(start), cap))
    for _ in range(_MAX_WIDENINGS):
        out.append(raw)
        if raw >= cap:
            break
        raw = min(cap, max(raw * 3, raw + 10))
    if out[-1] != cap:
        out.append(cap)
    return out


def _result(ok, messages=None, reason=None, room_id=None, complete=None):
    # `complete` is False when the raw-event window was still growing when we stopped, so a
    # caller can tell "this room is quiet" from "I did not look far enough". None on error
    # paths, where the question does not arise.
    return {"ok": bool(ok), "room_id": room_id, "reason": reason, "messages": messages or [],
            "complete": complete}


_REDACTOR = None


def _log_fallback(msg):
    """Announce a degraded redactor once, at selection.

    Withheld bodies and genuinely-secret-laden ones look identical downstream,
    and the choice is cached for the process, so without this an operator
    cannot tell a broken install from a quiet room.
    """
    print("room-ops read: %s" % msg, file=sys.stderr)


def _redactor():
    """The WRITE path's own filter, resolved once — not a second implementation.

    `remote_gateway_bridge` scrubs a pasted secret before the task file is
    persisted; this reader returned the same message verbatim, so a secret the
    bridge removed on the way in came back out on the same room. Delegating to
    `src/chat_redaction` — the chain both paths now call — keeps them symmetric
    by construction; a regex copied to this side would drift from its twin.
    """
    global _REDACTOR
    if _REDACTOR is not None:
        return _REDACTOR
    src = str((Path(__file__).resolve().parents[2] / "src"))
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        # The shared chain, not one half of it: chat_secret_filter alone is blind
        # to a vault-set value with no provider signature, at any length.
        from chat_redaction import redact_chat_body
        _REDACTOR = redact_chat_body
    except Exception as exc:
        try:
            # Narrower, but the vault-set grammar is the case this reader was
            # reported on, so a partial filter still beats passing it through.
            from vault_set_grammar import redact_vault_commands
            _REDACTOR = lambda s: redact_vault_commands(
                s, placeholder="[VAULT-SET-REDACTED: filter unavailable]")
            _log_fallback("chat_secret_filter unavailable (%s) — using the narrower "
                          "vault_set_grammar; non-vault secrets are NOT filtered" % exc)
        except Exception as exc2:
            # Fail CLOSED: returning the raw body IS the defect, and both modules
            # are tracked files, so absence means a broken install.
            _REDACTOR = lambda s: "[REDACTION UNAVAILABLE — body withheld]" if s else s
            _log_fallback("no redaction module importable (%s / %s) — WITHHOLDING every "
                          "body; this is pinned for the life of the process" % (exc, exc2))
    return _REDACTOR


def _normalize(items):
    out = []
    redact = _redactor()
    for m in items or []:
        body = m.get("body") or m.get("text") or m.get("message")
        norm = {
            "sender": m.get("sender") or m.get("user_id") or m.get("from"),
            "ts": m.get("ts") or m.get("timestamp"),
            "body": redact(body) if isinstance(body, str) else body,
            "event_id": m.get("event_id") or m.get("id"),
            # The gateway annotates each message with its reactions (key + sender);
            # without carrying it through, a worker can't see the 👀 delivery-ack on
            # its own messages (reactions are never pushed as tasks — read is the
            # only surface). Always a list so consumers need no None-check.
            "reactions": m.get("reactions") or [],
        }
        # `fetch` needs media_ref as its handle, so dropping it leaves an attachment
        # visible but unfetchable. Set only when present, keeping the shape additive.
        ref = m.get("media_ref")
        if ref:
            norm["media_ref"] = ref
            # Also conditional: the gateway is an external producer, so a media event
            # arriving without msgtype must not grow an explicit null either.
            if (mt := m.get("msgtype")):
                norm["msgtype"] = mt
        out.append(norm)
    return out


def read_room(room_id, agent_mxid=None, limit=DEFAULT_LIMIT, *, gate=None, before=None):
    """Pull up to `limit` recent messages from `room_id` via the gateway verb."""
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    if not room_id:
        return _result(False, reason="no room_id given")
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, reason=f"client gate denied for {agent_mxid}", room_id=room_id)
    base, headers = gateway()
    if not base:
        return _result(False, reason="no gateway configured", room_id=room_id)
    # `limit` here means MESSAGES, but the gateway applies its limit to RAW TIMELINE
    # EVENTS and only some of those are messages — reactions, receipts, membership and
    # media events all consume the budget. Measured on a live room 2026-08-05:
    #
    #     limit=  4 ->  0 messages      <- ok:true, no error, reads as an empty room
    #     limit= 10 ->  1
    #     limit= 20 ->  8
    #     limit= 40 -> 14
    #     limit=100 -> 14               <- history exhausted
    #
    # Nine non-message events sat in front of the newest message, so a caller asking for
    # the last few messages got a confident, silent ZERO. That is the dangerous shape: a
    # small limit is exactly what a cheap "has anyone replied yet?" probe passes, and the
    # answer it gets back is indistinguishable from a genuinely quiet room.
    #
    # So widen the raw window until we actually hold `limit` messages, the server stops
    # producing new ones (history exhausted), or MAX_LIMIT is reached. Each response is a
    # superset of the previous one — the window only grows — so the last one is the answer.
    import json

    def _fetch(raw_limit):
        params = {"limit": raw_limit}
        if before:
            params["before"] = before
        url = f"{base}/v1/rooms/{quote(room_id)}/messages?" + urlencode(params)
        _, body, _h = http_request("GET", url, headers)
        parsed = json.loads(body.decode("utf-8") or "{}")
        items = parsed.get("messages") if isinstance(parsed, dict) else parsed
        return _normalize(items)

    # NO EARLY STOP ON A REPEATED COUNT. An unchanged message count across a wider raw
    # window looks like "history exhausted" and is not: the extra raw events may all be
    # non-messages, with older messages just past the wall. Both reviewers of #2678
    # produced the control independently — a 20-event front gap makes windows [3, 13]
    # return zero twice, and stopping there rebuilds the exact false-empty this function
    # exists to remove. Guarding the check on "we have seen a message" was MY first
    # attempt and only moves the wall: one message ahead of a 60-event gap still returned
    # 1 of 5 with complete=True. There is no safe repeated-count inference.
    #
    # The gateway offers no genuine exhaustion signal either — this endpoint returns only
    # message-type items, so a short page is indistinguishable from a noisy window. So the
    # only sound stop conditions are "we have what was asked for" and "we have looked as
    # far as the API allows", and `complete` means strictly the former.
    messages = []
    try:
        for raw in _windows(limit, MAX_LIMIT):
            messages = _fetch(raw)
            if len(messages) >= limit:
                break
    except HTTPError as e:
        return _result(False, reason=degrade_reason(e.code), room_id=room_id)
    except (URLError, TimeoutError) as e:
        return _result(False, reason=f"network error: {e}", room_id=room_id)
    except ValueError as e:
        return _result(False, reason=f"parse error: {e}", room_id=room_id)

    # `complete` is the half that makes a short result readable, and it UNDER-claims on
    # purpose: short-of-limit is reported False even for a room that may genuinely hold
    # nothing more, because from here those two cases are indistinguishable. A caller that
    # sees complete=False knows only "do not treat this as the whole story", which is the
    # safe direction for a function whose failure mode is a confident empty.
    return _result(True, messages[:limit], room_id=room_id,
                   complete=len(messages) >= limit)
