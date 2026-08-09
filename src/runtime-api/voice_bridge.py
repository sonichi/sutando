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
    open(params) -> {"streamId": int, "streamType": "audio", ...}
    on_audio(stream_id, payload: bytes) -> None
    close(stream_id) -> bool
"""
from __future__ import annotations

import threading


class StubVoiceBridge:
    """First-slice stub: records opened streams and received audio bytes so the
    lifecycle + routing can be proven end-to-end with no voice dependency. A
    real audio downstream (server→device) is a later slice; open() returns a
    stream id and on_audio() counts frames/bytes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._streams: dict[int, dict] = {}
        self._next_id = 1

    def open(self, params: dict | None = None) -> dict:
        with self._lock:
            sid = self._next_id
            self._next_id = (self._next_id % 0xFFFF) + 1
            self._streams[sid] = {"frames": 0, "bytes": 0}
        return {"streamId": sid, "streamType": "audio"}

    def on_audio(self, stream_id: int, payload: bytes) -> None:
        with self._lock:
            s = self._streams.get(stream_id)
            if s is None:
                return  # unknown/closed stream — drop, never crash the transport
            s["frames"] += 1
            s["bytes"] += len(payload)

    def close(self, stream_id: int) -> bool:
        with self._lock:
            return self._streams.pop(stream_id, None) is not None

    # test/inspection helper — not part of the transport-facing interface
    def stats(self, stream_id: int) -> dict | None:
        with self._lock:
            s = self._streams.get(stream_id)
            return dict(s) if s else None
