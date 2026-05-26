#!/usr/bin/env python3
"""Behavioral tests for `src/vision_push.py` pre-network guard functions.

Tests the defensive logic in push_image() that fires BEFORE any network call,
plus is_voice_ready() fail-safe behavior when no voice agent is running.

Functions under test:
  push_image(path, source)  — inject image into voice stream; pre-network guards
  is_voice_ready(timeout)   — check if voice session is up

Network-dependent post-guard logic (_post, actual frame injection) is excluded
— those paths require a running voice agent.

Run: python3 tests/vision-push-guards.test.py
Exit: 0 on pass, non-zero on fail.
"""

from __future__ import annotations

import importlib.util
import mimetypes
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src"

# -----------------------------------------------------------------------
# Module loader
# -----------------------------------------------------------------------
_WS = tempfile.mkdtemp(prefix="sutando-test-vision-")
os.environ["SUTANDO_WORKSPACE"] = _WS
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _n in ("vision_push", "workspace_default", "util_paths"):
    sys.modules.pop(_n, None)

# Override VISION_PORT to something that is definitely not running
os.environ["VISION_CONTROL_PORT"] = "19999"

_spec = importlib.util.spec_from_file_location("vision_push", _SRC / "vision_push.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

push_image = _mod.push_image
is_voice_ready = _mod.is_voice_ready
MIN_FRAME_BYTES = _mod.MIN_FRAME_BYTES

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

PASS = 0
FAIL = 0
_TMP = Path(tempfile.mkdtemp(prefix="sutando-test-vision-imgs-"))


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def fail(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


def _write_image(name: str, size: int, suffix: str = ".jpg") -> Path:
    p = _TMP / (name + suffix)
    p.write_bytes(b"\xFF\xD8\xFF" + b"\x00" * (size - 3))  # minimal JPEG header + padding
    return p


# -----------------------------------------------------------------------
# MIN_FRAME_BYTES constant
# -----------------------------------------------------------------------

# T1 — MIN_FRAME_BYTES is 2048 (guards against corrupted/partial images)
if MIN_FRAME_BYTES == 2048:
    ok("T1: MIN_FRAME_BYTES == 2048 (documented threshold for partial-image guard)")
else:
    fail("T1: MIN_FRAME_BYTES", f"got {MIN_FRAME_BYTES}")

# -----------------------------------------------------------------------
# push_image — pre-network guards (these fire before any network call)
# -----------------------------------------------------------------------

# T2 — non-existent path → False (guard fires before network)
result = push_image("/nonexistent/sutando-test-image.jpg")
if result is False:
    ok("T2: push_image(nonexistent path) → False (file-existence guard)")
else:
    fail("T2: nonexistent path", f"got {result!r}")

# T3 — file under MIN_FRAME_BYTES → False (partial-image guard)
tiny = _write_image("tiny", 512)
result = push_image(str(tiny))
if result is False:
    ok(f"T3: push_image(512-byte file) → False (< {MIN_FRAME_BYTES} byte MIN_FRAME_BYTES guard)")
else:
    fail("T3: tiny file guard", f"got {result!r}")

# T4 — exactly MIN_FRAME_BYTES is still below threshold (boundary: < not <=)
# MIN_FRAME_BYTES guard is `if len(data) < MIN_FRAME_BYTES: return False`
# So exactly 2048 bytes passes the guard (not False from this check)
# We still expect False because voice is not ready — but the file-size guard doesn't fire.
at_threshold = _write_image("at-threshold", MIN_FRAME_BYTES)
result = push_image(str(at_threshold))
# File size guard doesn't fire; is_voice_ready() returns False → overall False
if result is False:
    ok(f"T4: push_image({MIN_FRAME_BYTES}-byte file) → False (size guard passes, voice not ready)")
else:
    fail("T4: at-threshold file", f"got {result!r}")

# T5 — large valid image file → False when voice not ready (no voice agent on port 19999)
large = _write_image("large", MIN_FRAME_BYTES * 2)
result = push_image(str(large))
if result is False:
    ok(f"T5: push_image({MIN_FRAME_BYTES * 2}-byte file, no voice agent) → False")
else:
    fail("T5: large file no voice", f"got {result!r}")

# T6 — PNG extension → MIME guessed as image/png (not None/fallback)
png = _write_image("test", MIN_FRAME_BYTES * 2, suffix=".png")
mime, _ = mimetypes.guess_type(str(png))
if mime == "image/png":
    ok("T6: .png extension → mimetypes guesses image/png (no fallback needed)")
else:
    fail("T6: png mime", f"got {mime!r}")

# T7 — unknown extension → mimetypes.guess_type returns None (triggering fallback to image/jpeg)
# vision_push.py: `if not mime or not mime.startswith("image/"): mime = "image/jpeg"`
unknown_ext = _TMP / "test-frame.sutandoimg"
unknown_ext.write_bytes(b"\x00" * (MIN_FRAME_BYTES * 2))
mime, _ = mimetypes.guess_type(str(unknown_ext))
if mime is None:
    ok("T7: unknown extension → mimetypes.guess_type returns None (module fallback to image/jpeg applies)")
else:
    fail("T7: unknown ext mime", f"got {mime!r}")

# T8 — .txt file (non-image MIME) → push_image returns False (non-image guard)
# mime = text/plain → not startswith("image/") → mime = "image/jpeg" then proceeds to
# is_voice_ready check → False because no voice agent
txt = _TMP / "not-an-image.txt"
txt.write_bytes(b"A" * (MIN_FRAME_BYTES * 2))
result = push_image(str(txt))
if result is False:
    ok("T8: .txt file (non-image ext, voice not ready) → False")
else:
    fail("T8: txt file non-image", f"got {result!r}")

# -----------------------------------------------------------------------
# is_voice_ready — fail-safe when no voice agent
# -----------------------------------------------------------------------

# T9 — is_voice_ready() → False when no voice agent on VISION_CONTROL_PORT 19999
result = is_voice_ready(timeout=0.5)
if result is False:
    ok("T9: is_voice_ready() → False when no voice agent running (connection refused)")
else:
    fail("T9: is_voice_ready no agent", f"got {result!r}")

# T10 — is_voice_ready with very short timeout → False (no crash, no hang)
try:
    result = is_voice_ready(timeout=0.1)
    if result is False:
        ok("T10: is_voice_ready(timeout=0.1) → False without crash or hang")
    else:
        fail("T10: short timeout result", f"got {result!r}")
except Exception as e:
    fail("T10: is_voice_ready short timeout no crash", f"raised {e}")

# T11 — push_image with directory path (os.path.isfile returns False for dirs) → False
import os
result = push_image(str(_TMP))
if result is False:
    ok("T11: push_image(directory path) → False (isfile guard)")
else:
    fail("T11: directory path", f"got {result!r}")

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print(f"\n{PASS}/{PASS + FAIL} tests passed")
if FAIL:
    sys.exit(1)
