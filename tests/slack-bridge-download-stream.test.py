#!/usr/bin/env python3
"""_download_slack_file streams to disk instead of buffering (#2172).

Behavioral proof, not a source grep: urlopen is stubbed with a response whose
read(n) counts calls. shutil.copyfileobj drains it in bounded chunks, so a
payload larger than one chunk MUST produce multiple bounded reads; the pre-fix
`f.write(resp.read())` shape produced exactly one unbounded read. Also
round-trips the payload bytes to prove content integrity.

slack-bridge imports slack_bolt + exits without tokens, so we stub the SDK and
set fake tokens for a hermetic load (same pattern as slack-writeside-attachments).

Run: python3 tests/slack-bridge-download-stream.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp()
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-real")

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# Stub slack_bolt so slack-bridge imports without the real SDK.
if "slack_bolt" not in sys.modules:
    bolt = types.ModuleType("slack_bolt")
    bolt.App = type("App", (), {"__init__": lambda self, **kw: None,
                                "event": staticmethod(lambda *a, **k: (lambda fn: fn)),
                                "message": staticmethod(lambda *a, **k: (lambda fn: fn))})
    sys.modules["slack_bolt"] = bolt
    adapter = types.ModuleType("slack_bolt.adapter")
    socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket_mode.SocketModeHandler = type("SocketModeHandler", (), {"__init__": lambda self, *a, **k: None})
    sys.modules["slack_bolt.adapter"] = adapter
    sys.modules["slack_bolt.adapter.socket_mode"] = socket_mode

sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("slack_bridge", REPO / "src" / "slack-bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules["slack_bridge"] = bridge
spec.loader.exec_module(bridge)


class CountingResponse(io.RawIOBase):
    """Fake urlopen response: serves `payload`, counts read() calls + max size."""

    def __init__(self, payload: bytes):
        self._buf = io.BytesIO(payload)
        self.read_calls = 0
        self.max_read_size = 0

    def read(self, size=-1):
        self.read_calls += 1
        data = self._buf.read(size)
        if data:
            self.max_read_size = max(self.max_read_size, len(data))
        return data

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# 1 MiB payload — far larger than copyfileobj's default chunk (64 KiB), so a
# streaming implementation must take many bounded reads; a buffering one takes 1.
payload = os.urandom(1024 * 1024)
resp = CountingResponse(payload)

captured = {}


def fake_urlopen(req, timeout=None):
    captured["timeout"] = timeout
    captured["auth"] = req.headers.get("Authorization")
    return resp


bridge.urllib.request.urlopen = fake_urlopen

local = bridge._download_slack_file(
    {"url_private_download": "https://files.slack.com/fake", "name": "big.bin", "id": "F123"}
)

check("download returns a path", local is not None)
if local:
    on_disk = Path(local).read_bytes()
    check("payload round-trips intact", on_disk == payload,
          f"len {len(on_disk)} vs {len(payload)}")
check("auth header carries bot token", (captured.get("auth") or "").startswith("Bearer "))
check("timeout still passed", captured.get("timeout") == 30, f"got {captured.get('timeout')}")
check("streamed in multiple bounded reads (not one buffered read)",
      resp.read_calls > 1, f"read_calls={resp.read_calls}")
check("no single read spanned the whole payload",
      resp.max_read_size < len(payload), f"max_read_size={resp.max_read_size}")

print()
if failures:
    print(f"FAIL — {len(failures)} check(s): {failures}")
    sys.exit(1)
print("PASS — slack download streams to disk")
