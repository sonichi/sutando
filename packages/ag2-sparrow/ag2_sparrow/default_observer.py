"""default_observer — the built-in 👀 observed-receipt (OPT-IN).

When the event channel is running, every `message.created` authored by someone
OTHER than this agent gets a 👀 reaction posted back to the room — the visible
"this agent saw it" receipt. Enable with SPARROW_OBSERVE_REACT=1; OFF by
default even when SPARROW_EVENTS is on.

Why opt-in and not default-on (#2319 review): this module scopes reactions by
the incoming room_id alone — no owner/DM restriction, no per-room allowlist, no
membership-size bound, no addressed/mention test. Default-on would therefore
make enabling the event plane post visible reactions to every human message in
every subscribed SHARED room, where the other participants gave no consent and
have no opt-out. Until that scoping policy is decided, the default must not be
able to surprise a room.

Why client-side (and not a server default): the receipt's meaning is "THIS
agent's consumer observed the event", so it must live and die with the agent's
own event consumption. A server-side default would emit receipts on behalf of
dead agents — a false liveness signal.

Shape: a chain-transparent WRAPPER around the consumer's handler, not a chain
member. HandlerChain routes each event to ONE claiming handler; the receipt is
a side effect that must fire for every event while leaving routing/settlement
(taskify batching, decision routing) untouched — so offer() tees the react and
returns the inner handler's result unchanged. Reacting is a courtesy signal:
every failure is swallowed + logged, and can never break event consumption.

Delivery is asynchronous: offer() only enqueues the prepared request onto a
bounded queue drained by a single daemon worker, so a slow or wedged /react
endpoint can never delay the inner handler or the sequential event drain.
Overflow policy: when the queue is full the receipt is dropped and logged —
never blocked, never retried (it's a courtesy signal, not a delivery
guarantee; the message stays marked seen so redelivery won't double-react).
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

# 👀 = "observed" — distinct from the task-intake ack the gateway emits
# server-side, so a glance separates "seen" from "accepted as a task".
OBSERVE_REACTION = "\U0001F440"

# Bounded at-least-once dedup: a redelivered (re-drained) event must not
# double-react. FIFO eviction; 4096 message-ids ≈ weeks of a busy room.
_SEEN_CAP = 4096

# Pending-receipt bound: with the 10s network timeout this is minutes of
# backlog against a wedged endpoint before receipts start dropping — far past
# the point where they stopped being a meaningful "seen just now" signal.
_QUEUE_CAP = 256

# Catch-up guard: only react to messages younger than this (seconds). A fresh
# install's first event drain replays the room's FULL history (observed live:
# a new inbox consumed cursor 0→570 in seconds) — an armed observer would 👀
# every historical message across rooms, bounded only by _SEEN_CAP. The
# receipt means "seen just NOW", so old events get marked seen without a
# react. Events without a usable `ts` are treated as live (the field is
# gateway-standard; dropping receipts on a missing field would silently
# disable the feature). Tune with SPARROW_OBSERVE_MAX_AGE_S; 0/negative
# disables the guard.
_MAX_AGE_S_DEFAULT = 300.0


class ReactObserverHandler:
    """Tee wrapper: react 👀 to others' new messages, then delegate offer()."""

    def __init__(self, inner, base_url: str, headers: dict, agent_mxid: str,
                 log=print, timeout: float = 10.0, queue_cap: int = _QUEUE_CAP,
                 max_age_s: "float | None" = None):
        self._inner = inner
        self._base = base_url.rstrip("/")
        self._headers = dict(headers)
        self._headers["Content-Type"] = "application/json"
        # Same explicit UA as the event channel — the gateway's edge rejects
        # urllib's default UA with 403.
        self._headers.setdefault("User-Agent", "sutando-gateway-client/1.0")
        self._mxid = agent_mxid
        self._log = log
        self._timeout = timeout
        if max_age_s is None:
            try:
                max_age_s = float(os.environ.get("SPARROW_OBSERVE_MAX_AGE_S")
                                  or _MAX_AGE_S_DEFAULT)
            except ValueError:
                max_age_s = _MAX_AGE_S_DEFAULT
        self._max_age_s = max_age_s
        self._seen: set = set()
        self._order: list = []
        self._queue: queue.Queue = queue.Queue(maxsize=queue_cap)
        self._worker = None
        self._worker_lock = threading.Lock()

    # ---- chain transparency: the consumer contract passes straight through
    @property
    def last_path(self):
        return getattr(self._inner, "last_path", None)

    def has_pending(self) -> bool:
        hp = getattr(self._inner, "has_pending", None)
        return bool(hp()) if callable(hp) else False

    def offer(self, event: dict) -> list:
        try:
            self._maybe_react(event)
        except Exception as e:  # noqa: BLE001 — courtesy signal, never breaks the chain
            self._log(f"react-observer: swallowed {e}")
        return self._inner.offer(event)

    # ---- the receipt itself
    def _maybe_react(self, event) -> None:
        if not isinstance(event, dict) or event.get("type") != "message.created":
            return
        if not self._mxid or event.get("actor_id") == self._mxid:
            # No own-mxid means self-echo can't be detected — reacting to our
            # own messages is noise, so without it the receipt stays off.
            return
        room = event.get("room_id")
        # content.message_id is the reactable id — the envelope's own event_id
        # names the delivery, not the message.
        msg_id = (event.get("content") or {}).get("message_id")
        if not room or not msg_id or msg_id in self._seen:
            return
        self._mark_seen(msg_id)
        # Catch-up guard: a backlog/replayed event (first-boot inbox drain,
        # deep reconnect) is marked seen but NOT reacted — the receipt means
        # "seen just now", and first-boot history would otherwise be 👀-spammed
        # across every room. ts is epoch-millis (gateway event schema).
        if self._max_age_s > 0:
            ts = event.get("ts")
            if isinstance(ts, (int, float)) and ts > 0:
                if (time.time() - ts / 1000.0) > self._max_age_s:
                    return
        # safe="" so every reserved char in the room id is escaped — the
        # default safe="/" would leave a room id containing "/" split across
        # URL path segments, misrouting the react.
        url = f"{self._base}/v1/rooms/{quote(str(room), safe='')}/react"
        data = json.dumps({"event_id": msg_id, "key": OBSERVE_REACTION}).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers,
                                     method="POST")
        try:
            self._queue.put_nowait((msg_id, req))
        except queue.Full:
            self._log(f"react-observer: queue full, dropping receipt for {msg_id}")
            return
        self._ensure_worker()

    # ---- asynchronous delivery (a slow /react must never stall the drain)
    def _ensure_worker(self) -> None:
        w = self._worker
        if w is not None and w.is_alive():
            return
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._deliver_loop, name="react-observer", daemon=True)
                self._worker.start()

    def _deliver_loop(self) -> None:
        while True:
            msg_id, req = self._queue.get()
            try:
                self._send(msg_id, req)
            finally:
                self._queue.task_done()

    def _send(self, msg_id, req) -> None:
        try:
            urllib.request.urlopen(req, timeout=self._timeout).close()
        except urllib.error.HTTPError as e:
            # Duplicate-react rejections (a previous run already reacted) and
            # permission degrades are benign for a courtesy receipt.
            self._log(f"react-observer: HTTP {e.code} reacting to {msg_id} (ignored)")
        except Exception as e:  # noqa: BLE001 — the worker must never die
            self._log(f"react-observer: error reacting to {msg_id}: {e} (ignored)")

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued receipts are delivered (tests / graceful stops)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _mark_seen(self, msg_id) -> None:
        self._seen.add(msg_id)
        self._order.append(msg_id)
        if len(self._order) > _SEEN_CAP:
            self._seen.discard(self._order.pop(0))
