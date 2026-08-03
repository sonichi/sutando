"""event_channel — Sparrow's persistent Workspace-Event delivery channel (#AWP P0).

An ADDITIVE, ISOLATED channel that runs ALONGSIDE task delivery and never touches
it. It keeps an outbound SSE connection to `/v1/events/stream`, writes every
authorized event durably to the local EventInbox (at-least-once), and resumes
from the durable cursor after any disconnect/restart.

Isolation contract (owner's bottom line: do NOT disrupt task delivery):
  - runs in its OWN thread; `run()` swallows every exception, so a channel
    failure (network death, auth loss, bad frame) can NEVER propagate to the
    task loop or crash the process.
  - its own connection / backoff / cursor — shares no state with task polling.

SSE handling is the productionized form of the events client's `stream()`:
sticky `id:` cursor, accumulating `data:`, `:`-comment keepalives, Last-Event-ID
resume (durable cursor), a read timeout to detect black-holed connections, and
fatal-vs-retryable classification (401/403/404 = fatal, else reconnect).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

# If not even a keepalive arrives for this long the TCP path is dead — reconnect.
STREAM_READ_TIMEOUT = 120
_FATAL_HTTP = frozenset({401, 403, 404})


def _sse_events(resp):
    """Yield ("comment"|"event", cursor_or_None, text) from a live SSE response.
    Sticky id, accumulating data, blank-line dispatch — SSE spec / #184 contract."""
    data, last_id = [], None
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data:
                yield ("event", last_id, "\n".join(data))
                data = []
            continue
        if line.startswith(":"):
            yield ("comment", None, line[1:].lstrip())
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
        elif field == "id":
            last_id = value


class EventChannel:
    def __init__(self, inbox, base_url: str, headers: dict,
                 log=print, max_backoff: float = 30.0, auth_retry: bool = False):
        self._inbox = inbox
        self._base = base_url.rstrip("/")
        # Held BY REFERENCE, not copied: the owning bridge mutates this dict
        # in place when the bearer rotates (auth-rejection recovery), and the
        # next (re)connect must carry the new token without a restart. Every
        # request already works on a per-request copy (dict(self._headers)).
        self._headers = headers
        # Cloudflare rejects urllib's default UA with 403 — which _consume_once
        # classifies as FATAL, so without this the channel would stop
        # permanently on first real-gateway connect (review P1). Same explicit
        # client UA the bridge's request path sets.
        self._headers.setdefault("User-Agent", "sutando-gateway-client/1.0")
        self._log = log
        self._max_backoff = max_backoff
        # auth_retry: the owning bridge has file-based token-rotation recovery
        # armed, so a 401/403 is a WINDOW (revoked key awaiting re-connect),
        # not a terminal condition — keep reconnecting with backoff; the next
        # attempt reads the shared header dict, which the bridge hot-swaps
        # when the rotated token lands. Without it (default), 401/403 stays
        # fatal exactly as before. 404 is fatal either way (no such route —
        # rotation can't fix a missing endpoint). Review P1: during a
        # recovery window the SSE channel could 401, stop permanently, and
        # nothing restarted it after the poller resumed.
        self._auth_retry = auth_retry
        self.health = {"status": "init", "last_cursor": inbox.durable_cursor(),
                       "last_event_at": None, "retry_count": 0, "error": None}

    def _set(self, **kw):
        self.health.update(kw)

    def _open(self):
        h = dict(self._headers)
        h["Accept"] = "text/event-stream"
        resume = self._inbox.durable_cursor()
        if resume is not None:
            # Header wins server-side over ?cursor= (#184) — resume from the last
            # DURABLY-written event, so nothing between durable and received is lost.
            h["Last-Event-ID"] = str(int(resume))
        req = urllib.request.Request(f"{self._base}/v1/events/stream", headers=h, method="GET")
        return urllib.request.urlopen(req, timeout=STREAM_READ_TIMEOUT)

    def _consume_once(self) -> bool:
        """One SSE connection. Returns True if it ended retryably (reconnect),
        False if fatal (stop). Never raises."""
        try:
            resp = self._open()
        except urllib.error.HTTPError as e:
            if e.code in _FATAL_HTTP:
                if self._auth_retry and e.code in (401, 403):
                    self._set(status="auth_failed", error=f"HTTP {e.code}")
                    self._log(f"event-channel: auth rejected HTTP {e.code} — "
                              "retrying (token-rotation recovery armed)")
                    return True
                self._set(status="auth_failed", error=f"HTTP {e.code}")
                self._log(f"event-channel: fatal HTTP {e.code} — stopping")
                return False
            self._set(status="reconnecting", error=f"HTTP {e.code}")
            return True
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self._set(status="reconnecting", error=f"connect: {e}")
            return True
        self._set(status="connected", error=None)
        try:
            for kind, sse_id, payload in _sse_events(resp):
                if kind == "comment":
                    continue  # keepalive
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue  # one garbled frame never kills the stream
                if not isinstance(event, dict):
                    continue  # valid JSON but not an event object (e.g. `data: []`)
                    # — skipping keeps the stream alive; inserting would raise,
                    # reconnect from the SAME cursor, and replay the bad frame forever.
                if sse_id is not None and "cursor" not in event:
                    try:
                        event["cursor"] = int(sse_id)
                    except (TypeError, ValueError):
                        pass
                # received → durable: the insert's COMMIT is the durable point.
                # A crash before it just replays this event (idempotent) next time.
                self._inbox.insert(event)
                self._set(last_cursor=self._inbox.durable_cursor(),
                          last_event_at=time.time())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self._set(status="reconnecting", error=f"stream dropped: {e}")
        except Exception as e:  # noqa: BLE001 — isolation: never escape the channel
            self._set(status="reconnecting", error=f"channel error: {e}")
            self._log(f"event-channel: swallowed {e}")
        finally:
            try:
                resp.close()
            except OSError:
                pass
        if self.health["status"] == "connected":
            # Clean EOF: the stream is over. Without this, run()'s backoff sleep
            # happens while gateway-status still advertises events "connected".
            self._set(status="reconnecting", error="stream EOF")
        return True  # EOF / drop / handled error → reconnect

    def run(self, stop=lambda: False) -> None:
        """Persistent loop: connect, consume, reconnect with backoff until stop()
        or a fatal auth error. ISOLATED — any failure stays inside this method."""
        backoff = 1.0
        while not stop():
            got_before = self.health["last_cursor"]
            retryable = self._consume_once()
            if not retryable:
                return  # fatal (auth) — do not spin
            if self.health["last_cursor"] != got_before:
                backoff = 1.0  # progress → reset the ladder
            self._set(retry_count=self.health["retry_count"] + 1)
            # sleep in small slices so stop() is responsive during a long backoff
            slept = 0.0
            while slept < backoff and not stop():
                time.sleep(min(0.5, backoff - slept))
                slept += 0.5
            backoff = min(backoff * 2.0, self._max_backoff)
        self._set(status="stopped")
