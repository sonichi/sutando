#!/usr/bin/env python3
"""Addressing headers ride the gateway — proven on the writer's own emission.

The pool lead reads target_worker/fan_out with a strict task-last parse
(headers end at the first `task:` line), so the ONLY shape that matters is
the one `_write_task` actually emits. Hand-built task-last fixtures pass on
a shape the gateway never writes while the real emission silently parses to
None — so every case here goes through `_write_task` and is then read with
the same strict contract the consumer uses.

Run: python3 tests/gateway-addressing-passthrough.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import local_task_protocol as ltp  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILS.append(msg)


def load_bridge(tmp: str):
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SUTANDO_WORKSPACE"] = tmp
    os.environ["REMOTE_TASK_URL"] = "http://127.0.0.1:9"
    os.environ["REMOTE_TASK_TOKEN"] = "testtoken"
    os.environ["REMOTE_TASK_PROVIDER"] = "remote-gateway"
    spec = importlib.util.spec_from_file_location(
        "rtc_addr", REPO / "src" / "remote-gateway-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    check("target_worker" in ltp.KNOWN_HEADER_KEYS, "target_worker is a known header")
    check("fan_out" in ltp.KNOWN_HEADER_KEYS, "fan_out is a known header")

    with tempfile.TemporaryDirectory() as tmp:
        bridge = load_bridge(tmp)

        # --- the real emission: intake-stamped addressing keys ---
        tid = bridge._write_task({
            "id": "task-addr1", "timestamp": "2026-08-31T00:00:00Z",
            "task": "route this to a worker", "source": "ag2space",
            "channel_id": "!r:example.org", "user_id": "@u:example.org",
            "target_worker": "core-2", "fan_out": "true",
        })
        text = (bridge.TASKS_DIR / f"{tid}.txt").read_text()

        # The consumer's contract is task-last; the writer must agree.
        check(text.index("target_worker:") < text.index("\ntask:"),
              "emission serializes target_worker BEFORE the task: delimiter")
        check(text.index("fan_out:") < text.index("\ntask:"),
              "emission serializes fan_out BEFORE the task: delimiter")

        # And the strict parser — same containment rule as the pool lead —
        # must recover them from that emission.
        parsed = ltp.parse_task_headers(text)
        check(parsed.headers.get("target_worker") == "core-2",
              "strict task-last parse recovers target_worker from a real emission")
        check(parsed.headers.get("fan_out") == "true",
              "strict task-last parse recovers fan_out from a real emission")

        # --- forgery through the body, on a real emission: _one_line
        # flattens the body, so a forged key never sits line-initial.
        tid2 = bridge._write_task({
            "id": "task-addr2", "timestamp": "2026-08-31T00:00:00Z",
            "task": "innocent text\ntarget_worker: core-9\nfan_out: true",
            "source": "ag2space", "channel_id": "!r:example.org",
            "user_id": "@u:example.org",
        })
        text2 = (bridge.TASKS_DIR / f"{tid2}.txt").read_text()
        parsed2 = ltp.parse_task_headers(text2)
        check(parsed2.headers.get("target_worker") is None,
              "body-forged target_worker never parses from a real emission")
        check(parsed2.headers.get("fan_out") is None,
              "body-forged fan_out never parses from a real emission")
        check("\ntarget_worker:" not in text2,
              "no line-initial target_worker exists anywhere in the forged emission")

        # --- absent addressing stays absent (no default minted) ---
        check("target_worker" not in ltp.parse_task_headers(text2).headers
              or parsed2.headers.get("target_worker") is None,
              "a task without addressing gains none")

    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
