#!/usr/bin/env python3
"""Unit test for remote-gateway-bridge's interaction_type serialization
(interaction-planes step 1): pass-through of whitelisted values, degradation
of unknown remote values to "message" (the gateway is outside the trust
boundary), and the default when the field is absent.

Run: python3 tests/remote-gateway-interaction-type.test.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The module writes task files into <workspace>/tasks at _write_task time and
# resolves the workspace at import time — point it at a throwaway dir via the
# supported per-clone config the resolver reads? No: resolve_workspace reads
# repo config. Instead just let it resolve normally but never call main();
# _write_task takes the target from module-level TASKS_DIR, which we redirect
# to a temp dir after import (plain Path attribute, no other holder).
spec = importlib.util.spec_from_file_location(
    "remote_gateway_bridge", REPO / "src" / "remote-gateway-bridge.py")
rgb = importlib.util.module_from_spec(spec)
sys.modules["remote_gateway_bridge"] = rgb
spec.loader.exec_module(rgb)

tmp = Path(tempfile.mkdtemp(prefix="rgb-it-test-"))
rgb.TASKS_DIR = tmp / "tasks"
rgb.RESULTS_DIR = tmp / "results"
rgb.ARCHIVE_RESULTS_DIR = tmp / "results" / "archive"


def _headers(task):
    written = rgb._write_task(task)
    assert written, f"_write_task rejected {task!r}"
    tid = written[0]
    body = (rgb.TASKS_DIR / f"{tid}.txt").read_text()
    # This serializer emits `task:` mid-file (field order = _TASK_FIELDS; every
    # header is newline-stripped by _one_line, access_tier last) — so parse all
    # `key: value` lines rather than stopping at `task:` like consumers of the
    # task-last shapes do.
    out = {}
    for line in body.split("\n"):
        if ": " in line:
            k, v = line.split(": ", 1)
            out.setdefault(k, v)
    return out


failures = []

def check(name, cond):
    print(("  ok  " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


# 1. Whitelisted value passes through verbatim.
h = _headers({"id": "task-it-1", "task": "hello", "interaction_type": "realtime_audio"})
check("whitelisted value passes through", h.get("interaction_type") == "realtime_audio")

# 2. Unknown remote value degrades to message (trust boundary).
h = _headers({"id": "task-it-2", "task": "hello", "interaction_type": "evil\nvalue"})
check("unknown value degrades to message", h.get("interaction_type") == "message")

# 3. Absent field defaults to message.
h = _headers({"id": "task-it-3", "task": "hello"})
check("absent field defaults to message", h.get("interaction_type") == "message")

# 4. Every vocabulary value round-trips.
for v in sorted(rgb._INTERACTION_TYPES):
    h = _headers({"id": f"task-it-{v}", "task": "x", "interaction_type": v})
    check(f"vocab round-trip: {v}", h.get("interaction_type") == v)

# 5. access_tier must remain the ONLY access_tier line and the final recognized
# task header. Bridge-authored security guidance intentionally follows as prose.
body = (rgb.TASKS_DIR / "task-it-1.txt").read_text()
lines = [l for l in body.split("\n") if l]
_tier_at = [i for i, l in enumerate(lines) if l.startswith("access_tier:")]
_tail = lines[_tier_at[0] + 1:] if len(_tier_at) == 1 else None
_known_headers = set(rgb._TASK_FIELDS) | {
    "interaction_type", "content_modalities", "media_form", "attachments",
}
check("access_tier is the last header",
      _tail is not None and not any(
          line.startswith(f"{key}:")
          for line in _tail
          for key in _known_headers
      ))

if failures:
    sys.exit(1)
print("PASS — gateway interaction_type serialization covered")
