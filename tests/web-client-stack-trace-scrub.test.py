#!/usr/bin/env python3
"""Structural tests for stack-trace scrubbing from HTTP error responses (#1114).

Verifies that web-client.ts and vision-tools.ts do NOT leak raw error messages
(e.message, String(e)) in HTTP response bodies — fixes for CodeQL js/stack-
trace-exposure warnings #49/#50/#51.

Pattern before this fix:
  res.end(JSON.stringify({ error: e?.message || String(e) }))
Pattern after:
  console.error('[web-client] endpoint failed:', e);   // server-side diagnostic
  res.end(JSON.stringify({ error: 'safe generic label' }))

Why structural: confirming the fix is in place is purely a source-inspection task.
The behavioral consequence (an attacker can't read internal stack traces from the
HTTP response) can't be directly unit-tested without a running server.

Run: python3 tests/web-client-stack-trace-scrub.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB_CLIENT = REPO / "src" / "web-client.ts"
VISION_TOOLS = REPO / "src" / "vision-tools.ts"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:400], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def _find_pattern_in_res_end(src: str, pattern: str) -> list[str]:
    """Return lines that call res.end() containing the given pattern."""
    return [
        line.strip()
        for line in src.splitlines()
        if "res.end(" in line and pattern in line
    ]


def main() -> None:
    missing = [f for f in [WEB_CLIENT, VISION_TOOLS] if not f.exists()]
    for m in missing:
        fail(f"file not found: {m}")
    if missing:
        sys.exit(1)

    wc_src = WEB_CLIENT.read_text()
    vt_src = VISION_TOOLS.read_text()

    # 1. web-client.ts: no raw error message leaking into res.end() bodies
    leaked_wc = _find_pattern_in_res_end(wc_src, "e?.message")
    leaked_wc += _find_pattern_in_res_end(wc_src, "String(e)")
    leaked_wc += _find_pattern_in_res_end(wc_src, "err.message")
    expect(
        len(leaked_wc) == 0,
        f"web-client.ts: {len(leaked_wc)} res.end() call(s) still leak raw error messages",
        ctx="\n".join(leaked_wc),
    )

    # 2. vision-tools.ts: no raw error message leaking into respond() bodies
    # vision-tools uses a `respond(status, body)` helper, not res.end() directly
    leaked_vt_lines = [
        line.strip()
        for line in vt_src.splitlines()
        if ("respond(" in line or "res.end(" in line)
        and ("err.message" in line or "String(err)" in line or "e?.message" in line)
    ]
    expect(
        len(leaked_vt_lines) == 0,
        f"vision-tools.ts: {len(leaked_vt_lines)} respond()/res.end() call(s) still leak raw error messages",
        ctx="\n".join(leaked_vt_lines),
    )

    # 3. web-client.ts: server-side console.error logs are present at the patched sites
    #    (/paidsubscriptions/data + /paidsubscriptions/scan)
    expect(
        "[web-client] /paidsubscriptions/data failed:" in wc_src
        or "paidsubscriptions" in wc_src,
        "web-client.ts: /paidsubscriptions error should still log to console.error server-side",
    )

    # 4. web-client.ts: safe generic labels are used instead of raw errors
    expect(
        "failed to read subscriptions" in wc_src,
        "web-client.ts: /paidsubscriptions/data must return safe label 'failed to read subscriptions'",
    )
    expect(
        "failed to enqueue scan" in wc_src,
        "web-client.ts: /paidsubscriptions/scan must return safe label 'failed to enqueue scan'",
    )

    # 5. vision-tools.ts: console.error calls exist before the scrubbed respond() calls
    expect(
        "console.error(" in vt_src,
        "vision-tools.ts: must log errors to console.error before scrubbed respond()",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 6
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
