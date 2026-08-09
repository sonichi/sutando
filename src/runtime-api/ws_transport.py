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


class WsTransport:
    """A WSS front-end that dispatches SCP frames through a shared dispatcher.

    The dispatcher is the SAME object the UDS transport uses — this class owns
    only the websocket mechanics + the network-edge gates (auth, allowlist).
    """

    def __init__(self, dispatcher, *, token: str,
                 method_allow=READ_ONLY_METHODS,
                 host: str = "127.0.0.1", port: int = 8787,
                 route: str = "/scp", log=print):
        self.dispatcher = dispatcher
        self.token = token
        self.method_allow = frozenset(method_allow)
        self.host = host
        self.port = port
        self.route = route
        self._log = log
        self._runner: web.AppRunner | None = None

    # ── auth (network edge) ──────────────────────────────────────────────────
    def _authorized(self, request: web.Request) -> bool:
        """Constant-time bearer check. Accepts `Authorization: Bearer <t>` or a
        `?token=<t>` query param (browsers/WebSocket clients can't always set
        headers on the upgrade). An empty configured token authorizes no one."""
        auth = request.headers.get("Authorization", "")
        presented = (auth[len("Bearer "):] if auth.startswith("Bearer ")
                     else request.query.get("token", ""))
        return bool(self.token) and hmac.compare_digest(presented, self.token)

    # ── dispatch ─────────────────────────────────────────────────────────────
    async def _dispatch_one(self, ws: web.WebSocketResponse, data: str) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        try:
            req_id, method, params = parse_line(raw)
        except ProtocolError as e:
            await ws.send_str(error_frame(e.req_id, e.code, e.message).decode())
            return
        if method not in self.method_allow:
            await ws.send_str(error_frame(
                req_id, -32601,
                f"method not permitted on this transport: {method}").decode())
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
        if not self._authorized(request):
            return web.Response(status=401, text="unauthorized")
        ws = web.WebSocketResponse(max_msg_size=MAX_LINE_BYTES + 1024)
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await self._dispatch_one(ws, msg.data)
            elif msg.type == WSMsgType.ERROR:
                break
            # non-text frames are ignored — SCP is a text JSON protocol
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
