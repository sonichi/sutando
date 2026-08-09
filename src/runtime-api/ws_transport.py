"""ws_transport.py — LAN WSS transport for SCP (Sutando Client Protocol).

A SECOND transport for the runtime-api daemon, alongside the Unix socket.
Same SCP methods, same dispatcher — one dispatcher, N transports. This is the
LAN-native leg: a phone-class client on the same network reaches the agent by
dialing the Server's own WSS listener (no relay, no cloud).

Where the UDS transport is same-user local (0600, no auth needed), this
transport is NETWORK-EXPOSED, so it adds two edge protections the socket does
not need:

  1. A bearer token on connect — Sutando's own credential. The socket's
     filesystem permission has no analogue over the network; the token is the
     precursor to per-device pairing credentials.
  2. A read-only method allowlist. Network peers get the READ surface
     (sutando.*, runtime.*, task.status/get_result, agent.*) only. Mutating
     methods (task.submit/cancel, capability.execute, terminal.input, ...) are
     refused at the transport edge until per-device authz lands in the
     dispatcher. This is a coarse network-exposure stopgap — the fine-grained
     per-device authorization stays a DISPATCHER concern (transport ≠ authz).

Binding defaults to loopback; exposing on the LAN is an explicit opt-in
(SUTANDO_SCP_WSS_HOST). The transport itself is opt-in (off unless
SUTANDO_SCP_WSS_ENABLE) so UDS-only deployments are unchanged.
"""
from __future__ import annotations

import hmac

from aiohttp import WSMsgType, web

from protocol import (MAX_LINE_BYTES, ProtocolError, error_frame,  # noqa: E402
                      parse_line, result_frame)

# The read surface a network peer may call before per-device authz exists.
# Anything that mutates state or reaches a governed capability is excluded and
# refused at the edge (see module docstring). Kept as a frozenset so it cannot
# be mutated at runtime.
READ_ONLY_METHODS = frozenset({
    "sutando.info", "sutando.status", "sutando.owner", "sutando.allowlist",
    "runtime.health", "runtime.details",
    "agent.list", "agent.status",
    "task.status", "task.get_result", "task.list", "task.list_results",
    "task.details",
    "request.get", "request.wait", "request.list",
    "human_action.status",
})


class WsWriterSink:
    """Adapts a WebSocketResponse to the StreamWriter push interface
    (`write(bytes)` + `async drain()`) so a WSS subscriber drops into the SAME
    subscriber sets as the Unix-socket writers — the server's push watchers
    (_emit_new_results / _push_activity) stay transport-agnostic and untouched.

    A failed send raises ConnectionResetError so the watchers' existing
    dead-writer cleanup discards it, exactly as it does for a dropped socket."""

    def __init__(self, ws):
        self._ws = ws
        self._pending: list = []

    def write(self, frame) -> None:
        self._pending.append(frame)

    async def drain(self) -> None:
        while self._pending:
            frame = self._pending.pop(0)
            text = frame.decode("utf-8") if isinstance(frame, (bytes, bytearray)) else frame
            try:
                await self._ws.send_str(text)
            except Exception as e:  # noqa: BLE001 — normalize to the socket signal
                raise ConnectionResetError(str(e)) from e

    def close(self) -> None:  # StreamWriter parity; the ws owns its own close
        pass


class WsTransport:
    """A WSS front-end that dispatches SCP frames through a shared dispatcher.

    The dispatcher is the SAME object the UDS transport uses — this class owns
    only the websocket mechanics + the network-edge gates (auth, allowlist).
    """

    def __init__(self, dispatcher, *, token: str,
                 method_allow=READ_ONLY_METHODS,
                 device_store=None,
                 result_subscribers: set | None = None,
                 activity_subscribers: set | None = None,
                 request_subscribers: set | None = None,
                 host: str = "127.0.0.1", port: int = 8787,
                 route: str = "/scp", log=print):
        self.dispatcher = dispatcher
        self.token = token
        self.method_allow = frozenset(method_allow)
        # Per-device credentials + pairing (opaque-bearer v0). When present, a
        # connection is authorized by ITS device credential's grants, not the
        # shared read-only allowlist. None → single shared bearer only.
        self.device_store = device_store
        # The server's OWN subscriber sets (shared with the UDS transport); a
        # WSS subscriber is a WsWriterSink added to these, so the server's push
        # watchers reach it with no transport-specific code. None → the
        # transport serves request/response only (no streaming).
        self._result_subs = result_subscribers
        self._activity_subs = activity_subscribers
        self._request_subs = request_subscribers
        self.host = host
        self.port = port
        self.route = route
        self._log = log
        self._runner: web.AppRunner | None = None

    # ── auth (network edge) ──────────────────────────────────────────────────
    def _presented(self, request: web.Request) -> str:
        auth = request.headers.get("Authorization", "")
        return (auth[len("Bearer "):] if auth.startswith("Bearer ")
                else request.query.get("token", ""))

    def _resolve_auth(self, request: web.Request) -> dict | None:
        """Resolve the presented bearer to a connection auth context, or None
        (→ 401). Precedence: a paired DEVICE credential (its own grants) → the
        shared read-only token → a valid PAIRING token (may only pair.redeem).
        `grants` is the effective per-connection method set."""
        tok = self._presented(request)
        if not tok:
            return None
        if self.device_store is not None:
            dev = self.device_store.authenticate(tok)
            if dev is not None:
                return {"kind": "device", "token": tok,
                        "device_id": dev.get("device_id"),
                        "grants": frozenset(dev.get("granted_methods", ()))}
        if self.token and hmac.compare_digest(tok, self.token):
            # shared read-only bearer — includes the streaming subscribe
            return {"kind": "shared", "token": tok,
                    "grants": self.method_allow | {"task.subscribe"}}
        if self.device_store is not None and self.device_store.pending_pairing(tok):
            return {"kind": "pairing", "token": tok, "grants": frozenset({"pair.redeem"})}
        return None

    # ── dispatch ─────────────────────────────────────────────────────────────
    async def _dispatch_one(self, ws: web.WebSocketResponse,
                            sink: "WsWriterSink", auth: dict, data: str) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        try:
            req_id, method, params = parse_line(raw)
        except ProtocolError as e:
            await ws.send_str(error_frame(e.req_id, e.code, e.message).decode())
            return
        grants = auth["grants"]
        if method == "pair.redeem":
            # A connection authenticated by a pairing token exchanges it for a
            # long-term per-device credential (returned once). No dispatcher
            # round-trip — pairing state is the device store's, not the agent's.
            if auth["kind"] != "pairing" or self.device_store is None:
                await ws.send_str(error_frame(
                    req_id, -32601, "pair.redeem requires a pairing token").decode())
                return
            issued = self.device_store.redeem_pairing(
                auth["token"], label=params.get("label"),
                capabilities=params.get("capabilities"))
            if issued is None:
                await ws.send_str(error_frame(
                    req_id, -32000, "pairing token invalid, expired, or used").decode())
                return
            await ws.send_str(result_frame(req_id, issued).decode())
            return
        if method == "task.subscribe":
            # Transport mode-switch (read-only stream), before the grant check —
            # the sink joins the server's subscriber sets; watchers do the rest.
            if "task.subscribe" not in grants or self._result_subs is None:
                await ws.send_str(error_frame(
                    req_id, -32601, "task.subscribe not permitted").decode())
                return
            streams = []
            if params.get("results", True):
                self._result_subs.add(sink)
                streams.append("results")
            if params.get("activity") and self._activity_subs is not None:
                self._activity_subs.add(sink)
                streams.append("activity")
            if params.get("requests") and self._request_subs is not None:
                self._request_subs.add(sink)
                streams.append("requests")
            await ws.send_str(result_frame(
                req_id, {"subscribed": True, "streams": streams}).decode())
            return
        if method not in grants:
            await ws.send_str(error_frame(
                req_id, -32601,
                f"method not permitted for this credential: {method}").decode())
            return
        try:
            result = await self.dispatcher.handle(method, params)
            await ws.send_str(result_frame(req_id, result).decode())
        except ProtocolError as e:
            await ws.send_str(error_frame(req_id, e.code, e.message).decode())
        except Exception as e:  # noqa: BLE001 — one bad request ≠ dead transport
            self._log(f"ws handler error: {e}")
            await ws.send_str(
                error_frame(req_id, -32000, f"server error: {e}").decode())

    async def _handle(self, request: web.Request):
        auth = self._resolve_auth(request)
        if auth is None:
            return web.Response(status=401, text="unauthorized")
        ws = web.WebSocketResponse(max_msg_size=MAX_LINE_BYTES + 1024)
        await ws.prepare(request)
        sink = WsWriterSink(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch_one(ws, sink, auth, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
                # non-text frames are ignored — SCP is a text JSON protocol
        finally:
            # a subscribed connection leaving must not linger in the push sets
            if self._result_subs is not None:
                self._result_subs.discard(sink)
            if self._activity_subs is not None:
                self._activity_subs.discard(sink)
            if self._request_subs is not None:
                self._request_subs.discard(sink)
        return ws

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get(self.route, self._handle)
        return app

    # ── lifecycle (runs on the daemon's asyncio loop, non-blocking) ──────────
    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self._log(
            f"SCP WSS listening on ws://{self.host}:{self.port}{self.route}")

    async def cleanup(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
