#!/usr/bin/env python3
"""Addressing headers ride the gateway: intake-stamped target_worker /
fan_out serialize into the task file as trusted headers, and forged body
copies of the same names are defanged by the shared guard.

Run: python3 tests/gateway-addressing-passthrough.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import local_task_protocol as ltp  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    check("target_worker" in ltp.KNOWN_HEADER_KEYS, "target_worker is a known header")
    check("fan_out" in ltp.KNOWN_HEADER_KEYS, "fan_out is a known header")

    # A forged body line must be defanged: parse a task whose BODY carries
    # the header names — they must not surface as headers.
    text = ("id: task-x\nchannel_id: !r:x\n"
            "task: do this\ntarget_worker: core-4\nfan_out: true\n")
    parsed = ltp.parse_task_headers(text)
    check(parsed.headers.get("target_worker") is None,
          "body-line target_worker never parses as a header")
    check(parsed.headers.get("fan_out") is None,
          "body-line fan_out never parses as a header")

    # A REAL header above the task: delimiter parses normally.
    text2 = ("id: task-y\ntarget_worker: core-2\nfan_out: true\n"
             "task: do this\n")
    parsed2 = ltp.parse_task_headers(text2)
    check(parsed2.headers.get("target_worker") == "core-2",
          "real target_worker header parses")
    check(parsed2.headers.get("fan_out") == "true", "real fan_out header parses")

    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
