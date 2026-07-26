"""default_observer — the built-in 👀 observed-receipt (default-on with events).

When the event channel is running, every `message.created` authored by someone
OTHER than this agent gets a 👀 reaction posted back to the room — the visible
"this agent saw it" receipt, with zero per-user setup. Default ON whenever
SPARROW_EVENTS is enabled; disable with SPARROW_OBSERVE_REACT=0.

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
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import quote

# 👀 = "observed" — distinct from the task-intake ack the gateway emits
# server-side, so a glance separates "seen" from "accepted as a task".
OBSERVE_REACTION = "\U0001F440"

# Bounded at-least-once dedup: a redelivered (re-drained) event must not
# double-react. FIFO eviction; 4096 message-ids ≈ weeks of a busy room.
_SEEN_CAP = 4096


class ReactObserverHandler:
    """Tee wrapper: react 👀 to others' new messages, then delegate offer()."""

    def __init__(self, inner, base_url: str, headers: dict, agent_mxid: str,
                 log=print, timeout: float = 10.0):
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
        self._seen: set = set()
        self._order: list = []

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
        url = f"{self._base}/v1/rooms/{quote(str(room))}/react"
        data = json.dumps({"event_id": msg_id, "key": OBSERVE_REACTION}).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers,
                                     method="POST")
        try:
            urllib.request.urlopen(req, timeout=self._timeout).close()
        except urllib.error.HTTPError as e:
            # Duplicate-react rejections (a previous run already reacted) and
            # permission degrades are benign for a courtesy receipt.
            self._log(f"react-observer: HTTP {e.code} reacting to {msg_id} (ignored)")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self._log(f"react-observer: network error reacting to {msg_id}: {e} (ignored)")

    def _mark_seen(self, msg_id) -> None:
        self._seen.add(msg_id)
        self._order.append(msg_id)
        if len(self._order) > _SEEN_CAP:
            self._seen.discard(self._order.pop(0))
