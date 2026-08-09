#!/usr/bin/env python3
"""Transport-contract test for the SCP LAN WSS transport (ws_transport.py).

Proves the network-edge contract in isolation with a STUB dispatcher (the REAL
dispatcher is exercised end-to-end by runtime-api-e2e). What must hold at the
edge, because this transport is network-exposed where the UDS socket is not:

  1. one dispatcher, N transports — a read method dispatches through the SAME
     dispatcher.handle and returns its result over WSS;
  2. no / wrong bearer token → 401 (the socket's 0600 has no network analogue);
  3. a mutating method is refused at the edge by the read-only allowlist and
     NEVER reaches the dispatcher;
  4. a malformed frame is a clean JSON-RPC parse error and the connection
     survives to serve the next request;
  5. a dispatcher ProtocolError surfaces as a JSON-RPC error frame;
  6. the ?token= query param authorizes (clients that can't set headers).

Run: python3 tests/runtime-api-ws-transport.test.py   (needs aiohttp)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RA = REPO / "src" / "runtime-api"
sys.path.insert(0, str(RA))

from aiohttp import WSServerHandshakeError  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from protocol import ProtocolError  # noqa: E402
from ws_transport import READ_ONLY_METHODS, WsTransport  # noqa: E402

TOKEN = "test-scp-token-abc123"
FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class StubDispatcher:
    """Records calls; a mutating method reaching here would be a leak."""

    def __init__(self):
        self.calls: list = []

    async def handle(self, method, params):
        self.calls.append((method, params))
        if method == "sutando.info":
            return {"agentId": "@stub:example.org"}
        if method == "runtime.health":
            return {"state": "online"}
        if method == "agent.status":
            raise ProtocolError(-32602, "unknown agent")
        return {"echo": method}


def rpc(req_id, method, params=None):
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method,
                       "params": params or {}})


async def run() -> None:
    # Sanity: the allowlist is read-only by construction — no mutating verb
    # should ever be in it (guards a future careless addition).
    mutating = {"task.submit", "task.cancel", "capability.execute",
                "approval.request", "elicitation.request", "request.cancel",
                "task.subscribe", "human_action.complete"}
    check(not (READ_ONLY_METHODS & mutating),
          "read-only allowlist contains no mutating verb")

    disp = StubDispatcher()
    tp = WsTransport(disp, token=TOKEN, log=lambda *a: None)
    server = TestServer(tp.build_app())
    client = TestClient(server)
    await client.start_server()
    try:
        # 2. no token → 401
        try:
            await client.ws_connect("/scp")
            check(False, "unauthenticated connect should be rejected")
        except WSServerHandshakeError as e:
            check(e.status == 401, "no bearer token → 401")
        # 2b. wrong token → 401
        try:
            await client.ws_connect("/scp",
                                    headers={"Authorization": "Bearer nope"})
            check(False, "wrong token should be rejected")
        except WSServerHandshakeError as e:
            check(e.status == 401, "wrong bearer token → 401")

        # 1. authorized read dispatches through the shared dispatcher
        ws = await client.ws_connect(
            "/scp", headers={"Authorization": f"Bearer {TOKEN}"})
        await ws.send_str(rpc(1, "sutando.info"))
        r1 = json.loads(await ws.receive_str())
        check(r1.get("id") == 1
              and r1.get("result", {}).get("agentId") == "@stub:example.org",
              "read method dispatches through the shared dispatcher over WSS")

        # 3. mutating method refused at the edge, dispatcher NOT called
        calls_before = len(disp.calls)
        await ws.send_str(rpc(2, "task.submit", {"task": "x"}))
        r2 = json.loads(await ws.receive_str())
        check(r2.get("id") == 2 and r2.get("error", {}).get("code") == -32601
              and "not permitted" in r2["error"]["message"]
              and len(disp.calls) == calls_before,
              "mutating method refused at edge, never reaches dispatcher")

        # 4. malformed frame → clean parse error, connection survives
        await ws.send_str("{not json")
        r3 = json.loads(await ws.receive_str())
        check(r3.get("error", {}).get("code") == -32700,
              "malformed frame → -32700 parse error")
        await ws.send_str(rpc(4, "runtime.health"))
        r4 = json.loads(await ws.receive_str())
        check(r4.get("id") == 4 and r4.get("result", {}).get("state") == "online",
              "connection survives a bad frame and serves the next request")

        # 5. dispatcher ProtocolError → JSON-RPC error frame
        await ws.send_str(rpc(5, "agent.status", {"agentId": "nope"}))
        r5 = json.loads(await ws.receive_str())
        check(r5.get("id") == 5 and r5.get("error", {}).get("code") == -32602
              and "unknown agent" in r5["error"]["message"],
              "dispatcher ProtocolError surfaces as a JSON-RPC error frame")
        await ws.close()

        # 6. ?token= query param authorizes (header-less clients)
        wsq = await client.ws_connect(f"/scp?token={TOKEN}")
        await wsq.send_str(rpc(6, "sutando.info"))
        r6 = json.loads(await wsq.receive_str())
        check(r6.get("result", {}).get("agentId") == "@stub:example.org",
              "?token= query param authorizes the connection")
        await wsq.close()
    finally:
        await client.close()


def main() -> int:
    asyncio.run(run())
    print(f"\n{'PASS — SCP WSS transport contract green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
