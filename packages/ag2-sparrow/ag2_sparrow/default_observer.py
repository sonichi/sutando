"""👀 observed-receipt: a chain-transparent tee reacting to other actors' fresh
messages. OPT-IN (SPARROW_OBSERVE_REACT=1) — it scopes by room_id alone."""
from __future__ import annotations

import os
import queue
import threading
import time

# 👀 = "observed" — distinct from the task-intake ack the gateway emits
# server-side, so a glance separates "seen" from "accepted as a task".
OBSERVE_REACTION = "\U0001F440"

# Bounded at-least-once dedup: a redelivered (re-drained) event must not
# double-react. FIFO eviction; 4096 message-ids ≈ weeks of a busy room.
_SEEN_CAP = 4096

# Pending-receipt bound: past this a wedged endpoint drops receipts rather
# than blocking — a stale "seen just now" is worth less than a live drain.
_QUEUE_CAP = 256

# Max message age (s) to react to; SPARROW_OBSERVE_MAX_AGE_S overrides, <=0
# disables. A first drain replays full room history — do not 👀 all of it.
_MAX_AGE_S_DEFAULT = 300.0


class ReactObserverHandler:
    """Tee: react 👀 to others' new messages, then delegate offer(). `react` is
    injected by the adapter edge; this module never names the room-verb endpoint."""

    def __init__(self, inner, react, agent_mxid: str, log=print,
                 queue_cap: int = _QUEUE_CAP, max_age_s: "float | None" = None):
        self._inner = inner
        self._react = react
        self._mxid = agent_mxid
        self._log = log
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
        # Replayed backlog is marked seen but NOT reacted. ts is epoch-millis;
        # a missing/unusable ts counts as live so the feature can't go silent.
        if self._max_age_s > 0:
            ts = event.get("ts")
            if isinstance(ts, (int, float)) and ts > 0:
                if (time.time() - ts / 1000.0) > self._max_age_s:
                    return
        try:
            self._queue.put_nowait((msg_id, room))
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
            msg_id, room = self._queue.get()
            try:
                self._send(msg_id, room)
            finally:
                self._queue.task_done()

    def _send(self, msg_id, room) -> None:
        try:
            self._react(room, msg_id, OBSERVE_REACTION)
        # Duplicate-react rejections (a previous run already reacted) and
        # permission degrades are benign for a courtesy receipt.
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
