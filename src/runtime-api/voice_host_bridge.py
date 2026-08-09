"""voice_host_bridge.py — bridge SCP audio streams to a voice-host process.

The REAL voice intelligence (bodhi VoiceSession + the injected work tool) lives
in a Node process — the voice-host — because VoiceSession is TypeScript and this
daemon is Python. This bridge is the Python half of that boundary: it implements
the same interface as StubVoiceBridge, but binds each SCP media stream to a
session on the voice-host over a local WebSocket.

Wire to the voice-host (deliberately minimal, one WS per stream):
    connect   ws://127.0.0.1:<port>/session
    text      {"open": {...params}}          (first frame; host acks {"ok": true})
    binary →  upstream audio (device mic)
    binary ←  downstream audio (host/agent voice)
    close     closing the WS closes the session

The daemon selects this bridge when SUTANDO_VOICE_HOST_URL is set; otherwise the
stub serves (loopback testing, no voice stack). The transport is unchanged
either way — same open/on_audio/close interface, same layering.
"""
from __future__ import annotations

import asyncio
import json
import threading


class NodeVoiceBridge:
    """Binds SCP media streams to voice-host sessions. All I/O runs on the
    daemon's event loop; on_audio is called from it (see ws_transport)."""

    def __init__(self, host_url: str, log=print):
        self.host_url = host_url.rstrip("/")
        self._log = log
        self._lock = threading.Lock()
        self._streams: dict[int, dict] = {}
        self._next_id = 1

    # ── bridge interface ─────────────────────────────────────────────────────
    def open(self, params: dict | None = None, send_media=None,
             send_event=None) -> dict:
        with self._lock:
            sid = self._next_id
            self._next_id = (self._next_id % 0xFFFF) + 1
            st = {"ws": None, "queue": asyncio.Queue(), "task": None,
                  "send": send_media, "send_event": send_event,
                  "params": params or {}}
            self._streams[sid] = st
        # The session runs as a task on the running loop: connect → open →
        # pump upstream (from queue) and downstream (to send_media) until closed.
        st["task"] = asyncio.get_running_loop().create_task(self._run(sid, st))
        return {"streamId": sid, "streamType": "audio", "host": True}

    def on_audio(self, stream_id: int, payload: bytes) -> None:
        with self._lock:
            st = self._streams.get(stream_id)
        if st is not None:
            st["queue"].put_nowait(payload)

    def interrupt(self, stream_id: int) -> bool:
        # Rides the upstream queue so ordering vs. queued audio is preserved;
        # the pump sends dict entries as text control frames.
        with self._lock:
            st = self._streams.get(stream_id)
        if st is None:
            return False
        st["queue"].put_nowait({"interrupt": True})
        return True

    def close(self, stream_id: int) -> bool:
        with self._lock:
            st = self._streams.pop(stream_id, None)
        if st is None:
            return False
        st["queue"].put_nowait(None)  # sentinel → _run tears down
        return True

    # ── session pump ─────────────────────────────────────────────────────────
    async def _run(self, sid: int, st: dict) -> None:
        import aiohttp  # local import: keep module importable without aiohttp
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(self.host_url + "/session") as ws:
                    st["ws"] = ws
                    await ws.send_str(json.dumps({"open": st["params"]}))
                    ack = await asyncio.wait_for(ws.receive(), timeout=10)
                    if ack.type != aiohttp.WSMsgType.TEXT or \
                            not json.loads(ack.data).get("ok"):
                        raise RuntimeError(f"voice-host refused session: {ack.data!r}")

                    async def upstream():
                        while True:
                            payload = await st["queue"].get()
                            if payload is None:
                                await ws.close()
                                return
                            if isinstance(payload, dict):  # control (interrupt)
                                await ws.send_str(json.dumps(payload))
                            else:
                                await ws.send_bytes(payload)

                    async def downstream():
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.BINARY and st["send"]:
                                await st["send"](msg.data)
                            elif msg.type == aiohttp.WSMsgType.TEXT and \
                                    st.get("send_event"):
                                # Host UI-state events (voice.state) — metadata
                                # only; audio never waits on these.
                                try:
                                    ev = json.loads(msg.data)
                                except ValueError:
                                    continue
                                if isinstance(ev, dict) and ev.get("method"):
                                    await st["send_event"](ev)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                              aiohttp.WSMsgType.ERROR):
                                return

                    up = asyncio.create_task(upstream())
                    try:
                        await downstream()
                    finally:
                        up.cancel()
        except Exception as e:  # noqa: BLE001 — a dead host must not kill the daemon
            self._log(f"voice-host session {sid} ended: {e}")
        finally:
            with self._lock:
                self._streams.pop(sid, None)
