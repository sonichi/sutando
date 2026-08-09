"""media_frame.py — the thin envelope on SCP binary (media-plane) frames.

One SCP WebSocket connection carries two planes: text frames = JSON-RPC control
+ events, binary frames = media streams. A binary frame is NOT implicitly audio;
it carries a 4-byte envelope so the same connection can multiplex audio today
and terminal / camera / other streams later without changing the transport.

Layout (v0), big-endian:
    byte 0      version   (0x01)
    byte 1      stream_type   1=audio  [reserved: 2=terminal, 3=camera/media]
    bytes 2-3   stream_id (uint16) — assigned by voice.open / <stream>.open
    bytes 4..   payload

The transport parses this and ROUTES by stream_id; it does not interpret the
payload (that belongs to the stream's handler — e.g. the VoiceBridge). Keeping
the envelope here keeps the framing pure and unit-testable.
"""
from __future__ import annotations

VERSION = 0x01
HEADER_LEN = 4

STREAM_AUDIO = 1
STREAM_TERMINAL = 2   # reserved
STREAM_CAMERA = 3     # reserved


class MediaFrameError(ValueError):
    pass


def encode(stream_type: int, stream_id: int, payload: bytes) -> bytes:
    if not (0 <= stream_type <= 0xFF):
        raise MediaFrameError("stream_type out of range")
    if not (0 <= stream_id <= 0xFFFF):
        raise MediaFrameError("stream_id out of range")
    return bytes((VERSION, stream_type,
                  (stream_id >> 8) & 0xFF, stream_id & 0xFF)) + bytes(payload)


def decode(frame: bytes) -> tuple[int, int, bytes]:
    """→ (stream_type, stream_id, payload). Raises MediaFrameError on a short
    or wrong-version frame so the transport can drop it without crashing."""
    if len(frame) < HEADER_LEN:
        raise MediaFrameError("media frame shorter than header")
    if frame[0] != VERSION:
        raise MediaFrameError(f"unknown media frame version {frame[0]}")
    stream_type = frame[1]
    stream_id = (frame[2] << 8) | frame[3]
    return stream_type, stream_id, frame[HEADER_LEN:]
