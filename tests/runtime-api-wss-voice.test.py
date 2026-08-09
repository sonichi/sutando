#!/usr/bin/env python3
"""V1 voice slice 1: one SCP connection carries the RPC/event plane AND a media
(audio) stream plane, routed to a STUB handler — no VoiceSession/Gemini yet.

Boots the REAL daemon with the WSS transport, pairs a device (its default grants
include voice.open/close), then over ONE connection:
  - a text sutando.status request round-trips (control plane still works);
  - voice.open returns a streamId;
  - binary media frames (envelope + payload) route to the voice bridge for that
    stream — proven by the bridge's byte/frame counters via a follow-up control
    call... except the stub's counters are server-internal, so instead we prove
    routing black-box: frames for the OPENED stream are accepted (no error /
    connection stays up), a frame for an UNOPENED stream is dropped, and
    voice.close tears the stream down (a later close reports closed:false).
  - a device WITHOUT the grant is refused voice.open.

Run: python3 tests/runtime-api-wss-voice.test.py   (needs aiohttp)
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
from device_store import DEFAULT_DEVICE_GRANTS, DeviceStore  # noqa: E402

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


TMP = tempfile.mkdtemp(prefix="wss-voice-")
PORT = free_port()
STATE = Path(TMP) / "state"
URL = f"http://127.0.0.1:{PORT}/scp"
ENV = {**os.environ,
       "SUTANDO_RUN_DIR": str(Path(TMP) / "run"),
       "SUTANDO_INSTANCE_ID": "wss-voice-test",
       "SUTANDO_RUNTIME_SOCKET": str(Path(TMP) / "rt.sock"),
       "SUTANDO_RUNTIME_DB": str(Path(TMP) / "rt.sqlite"),
       "SUTANDO_HA_DIR": str(Path(TMP) / "human-actions"),
       "SUTANDO_RUNTIME_STATE": str(STATE),
       "SUTANDO_AGENT_ID": "@wss-voice:example.org",
       "SUTANDO_HOST_LABEL": "wss-voice-host",
       "SUTANDO_INSTANCE_REGISTRY": str(Path(TMP) / "instances"),
       "SUTANDO_TMUX_SOCKET": "/tmp/wss-voice-tmux.sock",
       "SUTANDO_TMUX_SESSION": "wss-voice-core",
       "SUTANDO_LAUNCHER_EXECUTABLE": str(REPO / "bin" / "sutando"),
       "SUTANDO_LAUNCHER_ARGS": '["serve"]',
       "SUTANDO_SCP_WSS_ENABLE": "1",
       "SUTANDO_SCP_WSS_TOKEN": "shared-ro",
       "SUTANDO_SCP_WSS_PORT": str(PORT),
       "SUTANDO_SCP_WSS_HOST": "127.0.0.1"}


async def rpc(ws, method, params=None, rid=1):
    await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": rid,
                                  "method": method, "params": params or {}}))
    return json.loads((await ws.receive()).data)


async def probe():
    import aiohttp
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "core-status.json").write_text(json.dumps(
        {"status": "running", "step": "voice e2e", "ts": 1}))
    store = DeviceStore(STATE / "auth")

    # a device WITH voice grants (default) and one WITHOUT
    tok_full = store.mint_pairing("watch")
    tok_novoice = store.mint_pairing(
        "novoice", grants=[g for g in DEFAULT_DEVICE_GRANTS
                           if g not in ("voice.open", "voice.close")])

    async with aiohttp.ClientSession() as sess:
        # redeem the voice-capable device
        async with sess.ws_connect(URL, headers={"Authorization": f"Bearer {tok_full}"}) as w:
            cred = (await rpc(w, "pair.redeem", {"label": "watch"}))["result"]["credential"]

        async with sess.ws_connect(URL, headers={"Authorization": f"Bearer {cred}"}) as w:
            # control plane still works on the same connection
            st = await rpc(w, "sutando.status")
            check(st.get("result", {}).get("step") == "voice e2e",
                  "control plane (sutando.status) works over the voice connection")

            # voice.open → streamId
            vo = await rpc(w, "voice.open", {})
            sid = vo.get("result", {}).get("streamId")
            check(isinstance(sid, int) and vo["result"].get("streamType") == "audio",
                  "voice.open returns an audio stream id")

            # binary media frames for the opened stream are accepted (connection
            # stays up, next control call still answers)
            for i in range(3):
                await w.send_bytes(mf.encode(mf.STREAM_AUDIO, sid, bytes([i]) * 320))
            # a frame for an UNOPENED stream id is dropped (no crash)
            await w.send_bytes(mf.encode(mf.STREAM_AUDIO, 0xABCD, b"orphan"))
            st2 = await rpc(w, "sutando.info", rid=2)
            check(st2.get("result", {}).get("agentId") == "@wss-voice:example.org",
                  "connection survives audio frames (opened + orphan) and still serves RPC")

            # voice.interrupt: own open stream → interrupted; unopened → not;
            # malformed sid → clean -32602 (same crash-path contract as close)
            vi = await rpc(w, "voice.interrupt", {"streamId": sid}, rid=20)
            check(vi.get("result", {}).get("interrupted") is True,
                  "voice.interrupt on an own open stream reports interrupted")
            vi2 = await rpc(w, "voice.interrupt", {"streamId": 0xABCD}, rid=21)
            check(vi2.get("result", {}).get("interrupted") is False,
                  "voice.interrupt on an unopened stream reports not-interrupted")
            vi3 = await rpc(w, "voice.interrupt", {"streamId": "x"}, rid=22)
            check(vi3.get("error", {}).get("code") == -32602,
                  "voice.interrupt malformed streamId → clean -32602")

            # voice.close tears the stream down; a second close reports not-open
            c1 = await rpc(w, "voice.close", {"streamId": sid}, rid=3)
            c2 = await rpc(w, "voice.close", {"streamId": sid}, rid=4)
            check(c1.get("result", {}).get("closed") is True
                  and c2.get("result", {}).get("closed") is False,
                  "voice.close tears down the stream (idempotent: second close = not open)")

            # crash-path regression: a malformed streamId (unhashable JSON list)
            # must be a clean -32602, not a connection-killing TypeError.
            bad = await rpc(w, "voice.close", {"streamId": []}, rid=13)
            check(bad.get("error", {}).get("code") == -32602,
                  "malformed streamId → clean -32602 (connection survives)")
            st_alive = await rpc(w, "sutando.info", rid=14)
            check("result" in st_alive,
                  "connection still serves RPC after the malformed close")

            # FULL DUPLEX: a loopback stream echoes upstream audio back
            # downstream — device → server → device round-trip proven with no
            # voice stack (what the M5/web echo-test will exercise).
            vo2 = await rpc(w, "voice.open", {"loopback": True}, rid=5)
            sid2 = vo2["result"]["streamId"]
            check(vo2["result"].get("loopback") is True,
                  "voice.open {loopback} opens an echo stream")
            probe_payload = bytes(range(200)) * 2
            await w.send_bytes(mf.encode(mf.STREAM_AUDIO, sid2, probe_payload))
            echo = None
            dl = time.monotonic() + 5
            while time.monotonic() < dl:
                msg = await asyncio.wait_for(w.receive(), timeout=5)
                if msg.type == aiohttp.WSMsgType.BINARY:
                    echo = msg.data
                    break
            st_e, sid_e, pl_e = mf.decode(echo) if echo else (None, None, b"")
            check(st_e == mf.STREAM_AUDIO and sid_e == sid2 and pl_e == probe_payload,
                  "upstream audio echoes back downstream (full-duplex round trip)")
            await rpc(w, "voice.close", {"streamId": sid2}, rid=6)

        # a device WITHOUT the voice grant is refused voice.open
        async with sess.ws_connect(URL, headers={"Authorization": f"Bearer {tok_novoice}"}) as w:
            cred2 = (await rpc(w, "pair.redeem", {"label": "novoice"}))["result"]["credential"]
        async with sess.ws_connect(URL, headers={"Authorization": f"Bearer {cred2}"}) as w:
            r = await rpc(w, "voice.open", {})
            check(r.get("error", {}).get("code") == -32601,
                  "a device without the voice.open grant is refused")
            ri = await rpc(w, "voice.interrupt", {"streamId": 1}, rid=2)
            check(ri.get("error", {}).get("code") == -32601,
                  "voice.interrupt rides the voice.open grant (no grant → refused)")


def main() -> int:
    proc = subprocess.Popen([sys.executable, str(SERVER)], env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        if not wait_port(PORT):
            print(proc.stdout.read()); raise AssertionError("WSS port never came up")
        asyncio.run(probe())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\n{'PASS — SCP voice-plane scaffold green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
