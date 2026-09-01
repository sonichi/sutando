#!/usr/bin/env python3
"""room-ops · events — client half of Agent Event Subscription & Delivery (#184).

Four surfaces, all speaking the FIXED #184 contract (the server half is built
separately against the same contract — this code targets the contract, never a
server implementation):

  - subscription management — `events_subscribe` / `events_unsubscribe` /
    `events_subscriptions` ops on the generic `POST {base}/v1/room` envelope
    (one privileged endpoint, many ops — same shape as doc/mention).
  - typed emit — the `event` op on that same envelope: send one typed
    `space.ag2.*` TIMELINE event as this agent. The receive half above had no
    counterpart, so an agent could subscribe to typed events it had no way to
    produce.
  - long-poll pull — `GET {base}/v1/events?cursor=<int>&wait=<sec>`; one
    bounded round per call; timeout = empty `events` + unchanged cursor.
  - SSE push — `GET {base}/v1/events/stream`; each event is `id: <cursor>` +
    `data: <json envelope>` + blank line; `:`-comment lines are keepalives.
    Resume via the `Last-Event-ID` request header (header wins server-side
    over `?cursor=`, so the header is the only resume channel used here).

Delivery is at-least-once. The client's dedup/replay anchor is the CURSOR the
caller persists (stream_with_resume + save_cursor's write-fsync-rename), not
any server-side state — reconnecting with an old cursor replays events, and
that is correct behaviour; consumers dedup on event_id/cursor.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from _gateway import (HTTP_TIMEOUT, gate_allows, load_gate, gateway, http_json,
                      degrade_reason, urlencode, HTTPError, URLError)
import receipt as _receipt

DEFAULT_WAIT = 30

# If not even a keepalive comment arrives for this long, the TCP path is dead
# (NAT/timeout black hole) — without a read timeout a silently-dropped
# connection would hang the consumer forever with no reconnect. Keepalive
# cadence is server-chosen but well under this bound.
STREAM_READ_TIMEOUT = 120


class StreamDisconnected(ConnectionError):
    """The SSE stream dropped (EOF / network death / transient HTTP). Raised
    instead of silently returning so the CALLER owns the reconnect policy
    (#184): stream_with_resume backs off and reconnects; bare stream() callers
    decide for themselves."""


# emit's fields live HERE, not in a private builder: all seven `_op_call`
# failure paths return through this one, so a split would KeyError on denial.
def _result(ok, *, room_id=None, subscription_id=None, subscriptions=None, reason=None,
            event_id=None, state=None):
    return {"ok": bool(ok), "room_id": room_id, "subscription_id": subscription_id,
            "subscriptions": subscriptions, "reason": reason,
            "event_id": event_id, "state": state}


# --------------------------------------------------------------------------- #
# Subscription management — the generic /v1/room op envelope
# --------------------------------------------------------------------------- #
def _op_call(op, room_id, agent_mxid, gate, extra=None, *, gated=True):
    """Gate check (room-scoped ops only) → op-envelope POST → response dict or
    a degraded _result. Mirrors doc._call — same envelope, same degrade map."""
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    if gated:
        if not room_id:
            return _result(False, room_id=room_id, reason="room_id is required")
        gate = load_gate() if gate is None else gate
        if not gate_allows(agent_mxid, room_id, gate):
            return _result(False, room_id=room_id,
                           reason=f"client gate denied for {agent_mxid}")
    base, headers = gateway()
    if not base:
        return _result(False, room_id=room_id, reason="no gateway configured")
    payload = {"op": op, **({"room_id": room_id} if room_id else {}), **(extra or {})}
    try:
        _status, res = http_json("POST", f"{base}/v1/room", headers, payload)
    except HTTPError as e:
        return _result(False, room_id=room_id, reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, room_id=room_id, reason=f"network error: {e}")
    if not isinstance(res, dict):
        return _result(False, room_id=room_id, reason="malformed gateway response")
    if res.get("error"):
        return _result(False, room_id=room_id, reason=str(res["error"]))
    return res


def subscribe(room_id, event_types, filters=None, agent_mxid=None, *, gate=None):
    """op `events_subscribe` → {ok, subscription_id, room_id, reason} (#184)."""
    extra = {"event_types": list(event_types or [])}
    # `filters` is an opaque server-side match object per #184 — passed through
    # verbatim, and only when given (absent means "no additional filtering").
    if filters is not None:
        extra["filters"] = filters
    res = _op_call("events_subscribe", room_id, agent_mxid, gate, extra)
    if res.get("ok") is False:
        return res
    # #184 contract: the op answers {"subscription": {subscription_id, ...}} —
    # the id is nested, NOT top-level. Read it from that object; fall back to a
    # top-level field only for forward-compat if the server ever flattens it.
    sub_obj = res.get("subscription")
    sub_id = sub_obj.get("subscription_id") if isinstance(sub_obj, dict) else None
    if sub_id is None:
        sub_id = res.get("subscription_id")
    return _result(True, room_id=room_id, subscription_id=sub_id)


def unsubscribe(room_id, agent_mxid=None, *, gate=None):
    """op `events_unsubscribe` → {ok, room_id, reason} (#184)."""
    res = _op_call("events_unsubscribe", room_id, agent_mxid, gate)
    if res.get("ok") is False:
        return res
    return _result(True, room_id=room_id)


def subscriptions(agent_mxid=None):
    """op `events_subscriptions` → the CALLER's own subscriptions (#184).
    No room target, so the per-room client gate does not apply (gated=False) —
    which subscriptions exist is the gateway's own record for this bearer."""
    res = _op_call("events_subscriptions", None, agent_mxid, None, gated=False)
    if res.get("ok") is False:
        return res
    return _result(True, subscriptions=res.get("subscriptions") or [])


def emit(room_id, event_type, content, agent_mxid=None, *, gate=None):
    """op `event` → one typed TIMELINE event sent as this agent.

    The accepted type namespace is the SERVER's rule, not re-encoded here — a
    client copy would drift. Do NOT infer the refusal SHAPE either: a rejected
    type may return a bare HTTP 502 with no `reason` rather than refuse legibly.
    """
    res = _op_call("event", room_id, agent_mxid, gate,
                   {"type": event_type, "content": content})
    if res.get("ok") is False:
        return res
    # Same three-state receipt as say/mention: UNCONFIRMED stays ok:true so a
    # caller cannot re-send an event that already landed.
    state, event_id, reason = _receipt.classify(res)
    return _result(True, room_id=room_id, event_id=event_id, reason=reason, state=state)


# --------------------------------------------------------------------------- #
# Long-poll pull — GET /v1/events
# --------------------------------------------------------------------------- #
def _http_get_json(url, headers, timeout):
    """Module-local GET: (a) a seam the tests patch, and (b) long-poll needs its
    OWN socket timeout — _gateway.http_request pins HTTP_TIMEOUT (15s), which
    would kill a wait=30 poll mid-hold."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8") or "{}")


def pull(cursor=0, wait=DEFAULT_WAIT):
    """One long-poll round → the parsed contract response {ok, events, cursor}.

    Timeout on the server side is NOT an error: it answers with empty `events`
    and the unchanged cursor (#184). Degrades to ok:false like every other verb
    — callers loop on the returned cursor either way."""
    try:
        wait = max(0, int(wait))
    except (TypeError, ValueError):
        wait = DEFAULT_WAIT
    base, headers = gateway()
    if not base:
        return {"ok": False, "events": [], "cursor": cursor, "reason": "no gateway configured"}
    url = f"{base}/v1/events?" + urlencode({"cursor": int(cursor), "wait": wait})
    try:
        # Socket timeout must OUTLIVE the server's hold window, or every quiet
        # poll ends in a spurious timeout instead of the contract's
        # empty-events response.
        res = _http_get_json(url, headers, wait + HTTP_TIMEOUT)
    except HTTPError as e:
        return {"ok": False, "events": [], "cursor": cursor, "reason": degrade_reason(e.code)}
    except (URLError, TimeoutError, OSError) as e:
        return {"ok": False, "events": [], "cursor": cursor, "reason": f"network error: {e}"}
    except ValueError as e:
        return {"ok": False, "events": [], "cursor": cursor, "reason": f"parse error: {e}"}
    if not isinstance(res, dict):
        return {"ok": False, "events": [], "cursor": cursor, "reason": "malformed gateway response"}
    return res


# --------------------------------------------------------------------------- #
# SSE push — GET /v1/events/stream
# --------------------------------------------------------------------------- #
def sse_frames(lines):
    """Parse decoded SSE lines (no trailing newline) into frames.

    Yields uniform 3-tuples: ("comment", None, text) per keepalive comment,
    ("event", last_event_id, data) on each blank-line dispatch. Per the SSE
    spec: `data:` lines ACCUMULATE (joined with \\n) until the blank line;
    `id:` is STICKY — the last-seen id applies to later frames that omit their
    own, which is what makes Last-Event-ID resume correct; one leading space
    after the colon is stripped; unknown fields (`event:`, `retry:`) are
    ignored — the #184 contract only uses id/data.
    """
    data = []
    last_id = None
    for line in lines:
        if line == "":
            # Blank line = dispatch. A blank line with no accumulated data
            # (e.g. after a comment) dispatches nothing, per spec.
            if data:
                yield ("event", last_id, "\n".join(data))
                data = []
            continue
        if line.startswith(":"):
            text = line[1:]
            if text.startswith(" "):
                text = text[1:]
            yield ("comment", None, text)
            continue
        field, _sep, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
        elif field == "id":
            last_id = value


def _iter_lines(resp):
    for raw in resp:
        yield raw.decode("utf-8", "replace").rstrip("\r\n")


def _open_stream(url, headers):
    """Seam for tests; returns the live response object (iterable by line)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=STREAM_READ_TIMEOUT)


def _cursor_of(sse_id, envelope):
    # The `id:` line is the authoritative cursor per #184; the envelope's own
    # `cursor` field is the fallback for a frame that omitted its id.
    try:
        return int(sse_id)
    except (TypeError, ValueError):
        cur = envelope.get("cursor") if isinstance(envelope, dict) else None
        return cur if isinstance(cur, int) else None


def stream(cursor=None, on_event=None, keepalive_cb=None, *, stop=None, max_events=None):
    """One SSE connection. Calls on_event(cursor:int, envelope:dict) per event.

    Returns the last delivered cursor when it ends VOLUNTARILY (stop() truthy
    or max_events reached). Any disconnect — EOF, network death, transient
    HTTP — raises StreamDisconnected so the caller owns the reconnect policy
    (#184). Config/permission problems (no gateway, 401/403/404) raise
    RuntimeError instead: retrying those in a loop would spin forever without
    ever succeeding.
    """
    base, headers = gateway()
    if not base:
        raise RuntimeError("no gateway configured")
    h = dict(headers)
    h["Accept"] = "text/event-stream"
    if cursor is not None:
        # Header wins server-side over ?cursor= (#184), so the header is the
        # only resume channel this client uses.
        h["Last-Event-ID"] = str(int(cursor))
    try:
        resp = _open_stream(f"{base}/v1/events/stream", h)
    except HTTPError as e:
        if e.code in (401, 403, 404):
            raise RuntimeError(degrade_reason(e.code)) from e
        raise StreamDisconnected(f"HTTP {e.code} opening stream") from e
    except (URLError, TimeoutError, OSError) as e:
        raise StreamDisconnected(f"connect failed: {e}") from e
    delivered = 0
    last = cursor
    try:
        try:
            for kind, sse_id, payload in sse_frames(_iter_lines(resp)):
                if stop is not None and stop():
                    return last
                if kind == "comment":
                    if keepalive_cb is not None:
                        keepalive_cb(payload)
                    continue
                try:
                    envelope = json.loads(payload)
                except ValueError:
                    continue  # one garbled frame is not worth killing the stream
                cur = _cursor_of(sse_id, envelope)
                if on_event is not None:
                    on_event(cur, envelope)
                if cur is not None:
                    last = cur
                delivered += 1
                if max_events is not None and delivered >= max_events:
                    return last
        except (URLError, TimeoutError, OSError) as e:
            raise StreamDisconnected(f"stream dropped: {e}") from e
    finally:
        try:
            resp.close()
        except OSError:
            pass
    raise StreamDisconnected("stream closed by server (EOF)")


# --------------------------------------------------------------------------- #
# Durable cursor + reconnect wrapper
# --------------------------------------------------------------------------- #
def load_cursor(path):
    """None when absent/garbled — the stream then starts from the server's
    default (new events only), which is the safe cold-start."""
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def save_cursor(path, cursor):
    """write → fsync → atomic rename → DIRECTORY fsync. This file is the
    client's at-least-once dedup anchor (#184): a torn write would silently
    reset replay or wedge resume, so the value must be durable AND never
    observable half-written. The directory fsync is load-bearing for the
    COLD-START anchor (review P1): losing a later cursor update to a crash
    merely replays, but losing the first-ever anchor's directory entry leaves
    no cursor file at all — restart sends no Last-Event-ID, the server
    resumes new-events-only, and a withheld batch is lost permanently."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(str(int(cursor)))
        f.flush()
        os.fsync(f.fileno())                 # data durable before the rename
    os.replace(tmp, path)
    dfd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
    try:
        os.fsync(dfd)                        # rename itself durable
    finally:
        os.close(dfd)


def stream_with_resume(cursor_file, on_event, keepalive_cb=None, *, stop=None,
                       max_events=None, max_backoff=30.0, should_persist=None):
    """stream() forever with a durable cursor.

    Loads the cursor from `cursor_file`, persists it AFTER each delivered event
    (post-callback on purpose: a crash mid-callback REPLAYS the event on the
    next run instead of dropping it — at-least-once; dedup is the consumer's
    job), and reconnects on disconnect with 1s, 2s, 4s, … max_backoff backoff
    (reset once events flow again). Runs until KeyboardInterrupt, stop()
    truthy, or max_events total deliveries. Returns the last cursor.

    `should_persist(cur, envelope) -> bool` (optional) gates cursor persistence.
    A BATCHING consumer (e.g. taskify) accumulates events in memory and only
    commits them durably at a flush; until then the persisted cursor must NOT
    advance past those pending events, or a restart resumes beyond them and they
    are lost forever (#2292 P1-2). Such a consumer passes a predicate that
    returns True only when nothing is pending in its batch. Default None =
    persist after every event (correct for stateless react/print consumers).
    """
    state = {"cursor": load_cursor(cursor_file), "count": 0, "got": False}

    def _deliver(cur, envelope):
        # COLD-START REPLAY ANCHOR (review P1): with no cursor file, a restart
        # sends no Last-Event-ID and the server defaults to NEW EVENTS ONLY —
        # so any event a batching consumer was holding in memory when the run
        # died would never be replayed (lost, not at-least-once). Before the
        # FIRST delivered event can enter a withheld batch, persist `cur - 1`
        # as the pre-batch anchor: a crash any time after this replays from
        # this event onward. Stateless consumers overwrite it one line later.
        if cur is not None and state["cursor"] is None and not state["got"]:
            save_cursor(cursor_file, cur - 1)
            state["cursor"] = cur - 1
        on_event(cur, envelope)
        # Hold the cursor while the consumer has un-flushed in-memory state:
        # advancing past accumulated-but-not-yet-committed events drops them on
        # the next restart. `should_persist` is evaluated AFTER on_event, so it
        # sees the post-callback batch state.
        if cur is not None and (should_persist is None or should_persist(cur, envelope)):
            save_cursor(cursor_file, cur)
            state["cursor"] = cur
        state["count"] += 1
        state["got"] = True

    backoff = 1.0
    while True:
        if stop is not None and stop():
            return state["cursor"]
        remaining = None if max_events is None else max_events - state["count"]
        if remaining is not None and remaining <= 0:
            return state["cursor"]
        state["got"] = False
        try:
            stream(cursor=state["cursor"], on_event=_deliver,
                   keepalive_cb=keepalive_cb, stop=stop, max_events=remaining)
            return state["cursor"]  # voluntary end (stop / max_events) — done
        except StreamDisconnected:
            pass  # expected during normal operation; back off and reconnect
        if state["got"]:
            backoff = 1.0  # progress happened — a fresh drop restarts the ladder
        time.sleep(backoff)
        backoff = min(backoff * 2.0, float(max_backoff))
