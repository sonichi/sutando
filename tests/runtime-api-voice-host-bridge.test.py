#!/usr/bin/env python3
"""Voice slice 3 (Python half): SCP streams bind to an external voice-host.

Runs a MOCK voice-host (speaking the exact voice-host wire: /session WS, {"open"}
handshake, binary audio both ways) and boots the REAL daemon with
SUTANDO_VOICE_HOST_URL pointing at it. Then over ONE device connection:

  device → voice.open → daemon's NodeVoiceBridge connects to the host,
  device audio frames → bridge → host (which transforms: reversed payload)
  host downstream → bridge → device as enveloped binary frames.

The transform (reversal) proves the audio went THROUGH the host — a loopback
echo could not produce it. Also: teardown on voice.close, and a dead host does
not break control-plane RPC (fail-soft).

The real Node voice-host (VoiceSession + work tool) slots in against this same
proven client next slice — its wire is what the mock speaks.

Run: python3 tests/runtime-api-voice-host-bridge.test.py   (needs aiohttp)
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "src" / "runtime-api" / "server.py"
sys.path.insert(0, str(REPO / "src" / "runtime-api"))
import media_frame as mf  # noqa: E402
from device_store import DeviceStore  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def wait_port(port, timeout=10):
    dl = time.time() + timeout
    while time.time() < dl:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close(); return True
        except OSError:
            time.sleep(0.1)
    return False


TMP = tempfile.mkdtemp(prefix="voice-host-")
PORT = free_port()          # daemon WSS
HOST_PORT = free_port()     # mock voice-host
STATE = Path(TMP) / "state"
URL = f"http://127.0.0.1:{PORT}/scp"
ENV = {**os.environ,
       "SUTANDO_RUN_DIR": str(Path(TMP) / "run"),
       "SUTANDO_INSTANCE_ID": "voice-host-test",
       "SUTANDO_RUNTIME_SOCKET": str(Path(TMP) / "rt.sock"),
       "SUTANDO_RUNTIME_DB": str(Path(TMP) / "rt.sqlite"),
       "SUTANDO_HA_DIR": str(Path(TMP) / "ha"),
       "SUTANDO_RUNTIME_STATE": str(STATE),
       "SUTANDO_AGENT_ID": "@voice-host:x",
       "SUTANDO_HOST_LABEL": "vh-host",
       "SUTANDO_INSTANCE_REGISTRY": str(Path(TMP) / "inst"),
       "SUTANDO_TMUX_SOCKET": "/tmp/vh-tmux.sock",
       "SUTANDO_TMUX_SESSION": "vh-core",
       "SUTANDO_LAUNCHER_EXECUTABLE": str(REPO / "bin" / "sutando"),
       "SUTANDO_LAUNCHER_ARGS": '["serve"]',
       "SUTANDO_SCP_WSS_ENABLE": "1",
       "SUTANDO_SCP_WSS_TOKEN": "sh",
       "SUTANDO_SCP_WSS_PORT": str(PORT),
       "SUTANDO_SCP_WSS_HOST": "127.0.0.1",
       "SUTANDO_VOICE_HOST_URL": f"http://127.0.0.1:{HOST_PORT}"}

MOCK_HOST = f'''
import asyncio, json
from aiohttp import web, WSMsgType

OPENED = []

async def session(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    first = await ws.receive()
    obj = json.loads(first.data)
    OPENED.append(obj.get("open"))
    await ws.send_str(json.dumps({{"ok": True}}))
    async for msg in ws:
        if msg.type == WSMsgType.BINARY:
            await ws.send_bytes(bytes(reversed(msg.data)))  # transform ≠ loopback
            await ws.send_str(json.dumps({{"method": "voice.state",
                                           "params": {{"state": "speaking"}}}}))
    return ws

app = web.Application()
app.router.add_get("/session", session)
web.run_app(app, host="127.0.0.1", port={HOST_PORT}, print=None)
'''


async def probe():
    import aiohttp
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "core-status.json").write_text(json.dumps(
        {"status": "running", "step": "voice-host e2e", "ts": 1}))
    store = DeviceStore(STATE / "auth")
    pairing = store.mint_pairing("vh-watch")

    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(URL, headers={"Authorization": f"Bearer {pairing}"}) as w:
            await w.send_str(json.dumps({"jsonrpc": "2.0", "id": 1,
                                         "method": "pair.redeem", "params": {}}))
            cred = json.loads((await w.receive()).data)["result"]["credential"]

        async with sess.ws_connect(URL, headers={"Authorization": f"Bearer {cred}"}) as w:
            async def rpc(method, params=None, rid=1):
                await w.send_str(json.dumps({"jsonrpc": "2.0", "id": rid,
                                             "method": method, "params": params or {}}))
                while True:
                    m = await asyncio.wait_for(w.receive(), timeout=10)
                    o = json.loads(m.data)
                    if o.get("id") == rid:
                        return o

            vo = await rpc("voice.open", {"lang": "en"}, rid=2)
            sid = vo.get("result", {}).get("streamId")
            check(isinstance(sid, int) and vo["result"].get("host") is True,
                  "voice.open binds the stream to the external voice-host")
            await asyncio.sleep(0.5)  # let the bridge finish the host handshake

            probe_payload = bytes(range(64))
            await w.send_bytes(mf.encode(mf.STREAM_AUDIO, sid, probe_payload))
            echo = None
            dl = time.monotonic() + 6
            while time.monotonic() < dl:
                msg = await asyncio.wait_for(w.receive(), timeout=6)
                if msg.type == aiohttp.WSMsgType.BINARY:
                    echo = msg.data; break
            st_e, sid_e, pl_e = mf.decode(echo) if echo else (None, None, b"")
            check(sid_e == sid and pl_e == bytes(reversed(probe_payload)),
                  "audio went THROUGH the host (transformed, not looped) and came back enveloped")

            # host text events forward as JSON-RPC notifications, stream-stamped
            ev = None
            dl = time.monotonic() + 6
            while time.monotonic() < dl:
                msg = await asyncio.wait_for(w.receive(), timeout=6)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    o = json.loads(msg.data)
                    if o.get("method") == "voice.state":
                        ev = o; break
            check(ev is not None
                  and ev.get("params", {}).get("state") == "speaking"
                  and ev.get("params", {}).get("streamId") == sid,
                  "host voice.state event reaches the device as a stamped notification")

            c = await rpc("voice.close", {"streamId": sid}, rid=3)
            check(c.get("result", {}).get("closed") is True,
                  "voice.close tears down the host-bound stream")

            # fail-soft: with the host DOWN, voice.open still answers and the
            # control plane keeps serving (the session task fails internally).
            # (mock host is killed by the runner between probes — see main)
            return w  # noqa: unreachable-return-style — kept simple


def main() -> int:
    host = subprocess.Popen([sys.executable, "-c", MOCK_HOST],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    daemon = subprocess.Popen([sys.executable, str(SERVER)], env=ENV,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not (wait_port(HOST_PORT) and wait_port(PORT)):
            raise AssertionError("mock host or daemon never came up")
        asyncio.run(probe())

        # fail-soft leg: kill the host, daemon must keep serving control RPC
        host.terminate(); host.wait(timeout=5)

        async def failsoft():
            import aiohttp
            store = DeviceStore(STATE / "auth")
            p2 = store.mint_pairing("vh2")
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(URL, headers={"Authorization": f"Bearer {p2}"}) as w:
                    await w.send_str(json.dumps({"jsonrpc": "2.0", "id": 1,
                                                 "method": "pair.redeem", "params": {}}))
                    cred = json.loads((await w.receive()).data)["result"]["credential"]
                async with sess.ws_connect(URL, headers={"Authorization": f"Bearer {cred}"}) as w:
                    await w.send_str(json.dumps({"jsonrpc": "2.0", "id": 2,
                                                 "method": "voice.open", "params": {}}))
                    vo = json.loads((await w.receive()).data)
                    ok_open = "result" in vo  # open answers even with host down
                    await w.send_str(json.dumps({"jsonrpc": "2.0", "id": 3,
                                                 "method": "sutando.status", "params": {}}))
                    st = json.loads((await w.receive()).data)
                    check(ok_open and st.get("result", {}).get("step") == "voice-host e2e",
                          "with the host DOWN the daemon fail-softs: RPC keeps serving")
        asyncio.run(failsoft())
    finally:
        for p in (host, daemon):
            try:
                p.terminate(); p.wait(timeout=5)
            except Exception:
                p.kill()
    print(f"\n{'PASS — voice-host bridge green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
