"""Small helper for posting one-shot vision frames to the active voice session.

Used by the Discord and Telegram bridges (and any other Python integration)
to push an image into Gemini Live's vision context when a photo arrives and
voice is connected. No-op when voice-agent isn't reachable — the photo will
still flow through the regular task pipeline as a file attachment.

Contract mirrors the JS-side controller in src/vision-tools.ts:
  - POST /vision/start  {source, mode:'push'}   enter push mode
  - POST /vision/frame  <binary JPEG/PNG>       inject one frame
  - POST /vision/stop                            leave push mode

Single-shot helper: starts push mode, sends the frame, leaves push mode on.
Calling it again while push mode is already on just sends another frame
(the start call is idempotent).
"""

import mimetypes
import os
import urllib.request
import urllib.error


VISION_PORT = int(os.environ.get("VISION_CONTROL_PORT", "7847"))
VISION_BASE = f"http://127.0.0.1:{VISION_PORT}"

# Skip bytes below typical JPEG minimum to avoid forwarding corrupted or
# in-flight downloads — a partial fetch returning under 2 KB is almost
# certainly not a real frame (header + tiny payload at best).
MIN_FRAME_BYTES = 2048


def _post(path: str, body: bytes, content_type: str, timeout: float = 3.0) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"{VISION_BASE}{path}",
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError):
        return 0, b""


def is_voice_ready(timeout: float = 1.0) -> bool:
    """Return True if a voice session is up and can accept frames."""
    try:
        with urllib.request.urlopen(f"{VISION_BASE}/vision/state", timeout=timeout) as resp:
            import json
            data = json.loads(resp.read())
            return bool(data.get("sessionReady"))
    except Exception:
        return False


def push_image(path: str, source: str = "bridge") -> bool:
    """Inject the JPEG/PNG at *path* into the active voice session's vision stream.

    Returns True if the frame was accepted. Silently returns False if voice
    isn't ready or the file can't be read — callers shouldn't break on this
    because the photo still flows through the normal task path.
    """
    if not os.path.isfile(path):
        return False
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        # Unknown extension — assume JPEG. Gemini will reject if it's wrong.
        mime = "image/jpeg"
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    if len(data) < MIN_FRAME_BYTES:
        return False  # blank/tiny/partial

    if not is_voice_ready():
        return False

    # Idempotent: starting push mode while already in push mode is fine; the
    # controller resets counters and continues to accept frames.
    _post(
        "/vision/start",
        b'{"source":"' + source.encode() + b'","mode":"push"}',
        "application/json",
    )
    status, _ = _post("/vision/frame", data, mime)
    return 200 <= status < 300
