"""voice_bridge.py — the boundary between SCP media framing and voice intelligence.

The WSS/SCP transport only frames and ROUTES; it must never own VoiceSession or
understand PCM. This module is where a media stream binds to a voice session.
The transport calls open() / on_audio() / close() by stream_id and knows nothing
about what happens inside.

THIS SLICE ships StubVoiceBridge only — it proves stream lifecycle + binary
routing WITHOUT pulling Bodhi/Gemini into the Server. The next slice adds a real
bridge that reuses the existing VoiceSession (lifting the phone-conversation
audio-pipe pattern), behind this same interface — so wiring the voice stack does
not touch the transport.

Interface (a bridge implements these):
    open(params, send_media=None) -> {"streamId": int, "streamType": "audio", ...}
        send_media: async callable(payload: bytes) — the DOWNSTREAM path. The
        transport builds it per-stream (it wraps the payload in the media
        envelope and writes a binary frame to that connection); the bridge may
        invoke it at any time from the event loop (e.g. when the voice stack
        produces response audio).
    on_audio(stream_id, payload: bytes) -> None      (upstream, device → server)
    close(stream_id) -> bool
"""
from __future__ import annotations

import asyncio
import threading


class StubVoiceBridge:
    """First-slice stub: proves stream lifecycle + BOTH audio directions with no
    voice dependency. open({"loopback": true}) echoes every upstream payload
    back downstream — so a device can round-trip mic → server → speaker and
    measure the full path before any voice stack exists."""

    def __init__(self):
        self._lock = threading.Lock()
        self._streams: dict[int, dict] = {}
        self._next_id = 1

    def open(self, params: dict | None = None, send_media=None,
             send_event=None) -> dict:  # send_event unused: stub emits no state
        p = params or {}
        with self._lock:
            sid = self._next_id
            self._next_id = (self._next_id % 0xFFFF) + 1
            self._streams[sid] = {"frames": 0, "bytes": 0,
                                  "send": send_media,
                                  "loopback": bool(p.get("loopback"))}
        return {"streamId": sid, "streamType": "audio",
                "loopback": bool(p.get("loopback"))}

    def on_audio(self, stream_id: int, payload: bytes) -> None:
        with self._lock:
            s = self._streams.get(stream_id)
            if s is None:
                return  # unknown/closed stream — drop, never crash the transport
            s["frames"] += 1
            s["bytes"] += len(payload)
            send = s["send"] if s["loopback"] else None
        if send is not None:
            # Called from the transport's event loop; schedule, don't block.
            try:
                asyncio.get_running_loop().create_task(send(payload))
            except RuntimeError:
                pass  # no loop (unit-test direct call) — loopback is loop-only

    def interrupt(self, stream_id: int) -> bool:
        with self._lock:
            s = self._streams.get(stream_id)
            if s is None:
                return False
            s["interrupts"] = s.get("interrupts", 0) + 1
            return True

    def close(self, stream_id: int) -> bool:
        with self._lock:
            return self._streams.pop(stream_id, None) is not None

    # test/inspection helper — not part of the transport-facing interface
    def stats(self, stream_id: int) -> dict | None:
        with self._lock:
            s = self._streams.get(stream_id)
            return dict(s) if s else None
