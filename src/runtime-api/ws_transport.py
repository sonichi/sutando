"""ws_transport.py — network transport for SCP (Sutando Client Protocol).

A SECOND transport for the runtime-api daemon, alongside the Unix socket.
Same SCP methods, same dispatcher — one dispatcher, N transports. This is the
network leg: a phone-class client on the same network reaches the agent by
dialing the Server's own listener (no relay, no cloud). The PRIMARY listener
is plain ws:// (the SUTANDO_SCP_WSS_* env names predate that split); an
OPTIONAL TLS sibling (SUTANDO_SCP_WSS_TLS, self-signed) exists for clients
that require a secure context.

Where the UDS transport is same-user local (0600, no auth needed), this
transport is NETWORK-EXPOSED, so authorization is per-credential at the edge
(_resolve_auth), not read-only across the board:

  1. Shared bearer token — confined to READ_ONLY_METHODS plus task.subscribe.
     Cannot mutate state.
  2. Pairing token — pair.redeem only (plus the pre-auth client.hello
     handshake, which every authenticated credential may call).
  3. Paired device credential — authorized by its own per-device grants
     (DEFAULT_DEVICE_GRANTS): the read surface PLUS task.submit/task.cancel
     and voice.open/voice.close (voice.interrupt rides the voice.open grant).
     A paired device can MUTATE owner-tier state by default; the credential,
     not the transport, is the security boundary on the network leg.

Binding defaults to loopback; exposing beyond loopback is an explicit opt-in
(SUTANDO_SCP_WSS_HOST) with no source-address filtering. The transport itself
is opt-in (off unless SUTANDO_SCP_WSS_ENABLE) so UDS-only deployments are
unchanged.
"""
from __future__ import annotations

import hmac
import json
from pathlib import Path

from aiohttp import WSMsgType, web

import media_frame  # noqa: E402
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
    "schedule.list",
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
                 voice_bridge=None,
                 agent_id: str | None = None,
                 host: str = "127.0.0.1", port: int = 8787,
                 route: str = "/scp", log=print):
        self.dispatcher = dispatcher
        self.token = token
        # The AGENT this server fronts (owner-model: clients key connection
        # profiles by agent_id, not by machine). Stamped into pair.redeem so a
        # device knows WHO authorized it from the very first exchange.
        self.agent_id = agent_id
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
        # Media-plane handler. The transport frames + routes binary streams to
        # it by stream_id; it must NOT be imported/understood here beyond the
        # open/on_audio/close interface (voice semantics live behind it).
        self._voice = voice_bridge
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
                        "label": dev.get("label"),
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
                            sink: "WsWriterSink", auth: dict, streams: set,
                            data: str) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        try:
            req_id, method, params = parse_line(raw)
        except ProtocolError as e:
            await ws.send_str(error_frame(e.req_id, e.code, e.message).decode())
            return
        grants = auth["grants"]
        if method in ("voice.open", "voice.close", "voice.interrupt"):
            # Media-plane control. The transport owns the stream's CONNECTION
            # lifecycle (which streams this connection may route binary to, and
            # teardown on disconnect); the voice bridge owns the session. The
            # transport never touches audio semantics here.
            # voice.interrupt is gated by the voice.open grant: cutting short a
            # reply on a stream this connection opened adds no capability.
            gate = "voice.open" if method == "voice.interrupt" else method
            if self._voice is None or gate not in grants:
                await ws.send_str(error_frame(
                    req_id, -32601, f"{method} not permitted").decode())
                return
            if method == "voice.interrupt":
                sid = params.get("streamId")
                if not isinstance(sid, int):
                    await ws.send_str(error_frame(
                        req_id, -32602, "streamId must be an integer").decode())
                    return
                ok = sid in streams and bool(
                    getattr(self._voice, "interrupt", lambda _sid: False)(sid))
                await ws.send_str(result_frame(
                    req_id, {"interrupted": ok, "streamId": sid}).decode())
                return
            if method == "voice.open":
                # Downstream path: the bridge gets a per-stream async callback
                # that wraps payloads in the media envelope and writes a binary
                # frame back to THIS connection. The transport still never
                # interprets audio — it only frames both directions.
                sid_box: list = []

                async def _send_media(payload: bytes) -> None:
                    if sid_box:
                        await ws.send_bytes(media_frame.encode(
                            media_frame.STREAM_AUDIO, sid_box[0], payload))

                async def _send_event(ev: dict) -> None:
                    # UI-metadata notifications (voice.state) — stamped with the
                    # stream id, JSON-RPC notification shape (no id).
                    if sid_box:
                        params_out = dict(ev.get("params") or {})
                        params_out["streamId"] = sid_box[0]
                        await ws.send_str(json.dumps({
                            "jsonrpc": "2.0", "method": ev["method"],
                            "params": params_out}))

                # Server-stamped device identity (overwrites any client-supplied
                # key — the resolved credential, not the request, names the
                # device) so downstream consumers can attribute the session.
                params = dict(params)
                if auth["kind"] == "device":
                    params["device"] = {"deviceId": auth.get("device_id"),
                                        "label": auth.get("label")}
                else:
                    params.pop("device", None)
                opened = self._voice.open(params, _send_media, _send_event)
                sid_box.append(opened["streamId"])
                streams.add(opened["streamId"])
                await ws.send_str(result_frame(req_id, opened).decode())
            else:
                sid = params.get("streamId")
                # Validate before the set lookup: an unhashable streamId (list/
                # dict from JSON) would raise outside the dispatch try/except
                # and kill the whole connection.
                if not isinstance(sid, int):
                    await ws.send_str(error_frame(
                        req_id, -32602, "streamId must be an integer").decode())
                    return
                ok = sid in streams and self._voice.close(sid)
                streams.discard(sid)
                await ws.send_str(result_frame(
                    req_id, {"closed": bool(ok), "streamId": sid}).decode())
            return
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
            if self.agent_id:
                issued["agent_id"] = self.agent_id
            await ws.send_str(result_frame(req_id, issued).decode())
            return
        if method == "client.hello":
            # A device advertises its LIVE capabilities + form factor. Recorded
            # onto its device record (descriptive self-report — never widens
            # authz). Non-device connections (shared token) just get an ack.
            recorded = None
            if auth["kind"] == "device" and self.device_store is not None:
                rec = self.device_store.record_hello(
                    auth.get("device_id"), params.get("device_type"),
                    params.get("capabilities"))
                if rec is not None:
                    recorded = {"device_id": rec.get("device_id"),
                                "device_type": rec.get("device_type"),
                                "capabilities": rec.get("capabilities", [])}
            await ws.send_str(result_frame(
                req_id, {"hello": True, "recorded": recorded}).decode())
            return
        if method == "task.subscribe":
            # Transport mode-switch (read-only stream), before the grant check —
            # the sink joins the server's subscriber sets; watchers do the rest.
            if "task.subscribe" not in grants or self._result_subs is None:
                await ws.send_str(error_frame(
                    req_id, -32601, "task.subscribe not permitted").decode())
                return
            # NB: named ack_streams — `streams` is the per-connection MEDIA
            # stream set parameter; shadowing it here would be a trap.
            ack_streams = []
            if params.get("results", True):
                self._result_subs.add(sink)
                ack_streams.append("results")
            if params.get("activity") and self._activity_subs is not None:
                self._activity_subs.add(sink)
                ack_streams.append("activity")
            if params.get("requests") and self._request_subs is not None:
                self._request_subs.add(sink)
                ack_streams.append("requests")
            await ws.send_str(result_frame(
                req_id, {"subscribed": True, "streams": ack_streams}).decode())
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
        streams: set = set()  # media stream_ids opened on THIS connection
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch_one(ws, sink, auth, streams, msg.data)
                elif msg.type == WSMsgType.BINARY:
                    self._route_binary(streams, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            # a leaving connection must not linger in the push sets, and its
            # media streams must be torn down (connection lifecycle = transport).
            if self._result_subs is not None:
                self._result_subs.discard(sink)
            if self._activity_subs is not None:
                self._activity_subs.discard(sink)
            if self._request_subs is not None:
                self._request_subs.discard(sink)
            if self._voice is not None:
                for sid in streams:
                    self._voice.close(sid)
        return ws

    def _route_binary(self, streams: set, data: bytes) -> None:
        """Decode the media envelope and route the payload to the voice bridge
        by stream_id — but ONLY for a stream this connection opened (no
        cross-connection routing). The transport does not interpret the payload.
        A malformed or unknown-stream frame is dropped, never fatal."""
        if self._voice is None:
            return
        try:
            _stream_type, stream_id, payload = media_frame.decode(data)
        except media_frame.MediaFrameError:
            return
        if stream_id not in streams:
            return
        self._voice.on_audio(stream_id, payload)

    async def _scp_voice_js(self, _request: web.Request):
        """Drop-in toggle-live voice client module (ES module + window global)
        for any web surface — the desktop chat's talk button imports this."""
        page = Path(__file__).with_name("scp-voice.js")
        try:
            return web.Response(text=page.read_text(encoding="utf-8"),
                                content_type="application/javascript")
        except OSError:
            return web.Response(status=404, text="scp-voice.js not installed")

    async def _companion(self, _request: web.Request):
        """The web-companion page — a phone-browser SCP client served from the
        same origin as the WSS endpoint. The static page carries no secrets and
        needs no auth; its WebSocket authenticates with the user's token."""
        page = Path(__file__).with_name("companion.html")
        try:
            return web.Response(text=page.read_text(encoding="utf-8"), content_type="text/html")
        except OSError:
            return web.Response(status=404, text="companion page not installed")

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get(self.route, self._handle)
        app.router.add_get("/companion", self._companion)
        app.router.add_get("/scp-voice.js", self._scp_voice_js)
        return app

    # ── lifecycle (runs on the daemon's asyncio loop, non-blocking) ──────────
    async def start(self, ssl_context=None) -> None:
        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port,
                           ssl_context=ssl_context)
        await site.start()
        scheme = "wss" if ssl_context else "ws"
        self._log(
            f"SCP WSS listening on {scheme}://{self.host}:{self.port}{self.route}")

    async def cleanup(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
