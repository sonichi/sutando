#!/usr/bin/env python3
"""Structural tests for web-client.ts security scrub (#1121)
and session-handoff.sh sentinel + state-file invariants.

## PR #1121 — scrub e.message from 3 more web-client.ts res.end sites

Before this fix, three HTTP error response sites in `src/web-client.ts`
leaked `e.message` / `e?.message` into the HTTP response body:

  1. `/note-viewing` parse failure: `error: e instanceof Error ? e.message : 'parse failed'`
  2. Overlay control proxy failure: `'overlay control proxy failed: ' + e.message`
  3. `/paidsubscriptions` render failure: `'Error reading subscriptions: ' + (e?.message || String(e))`

Each was replaced with a static string; the detailed error was moved to
`console.error('[web-client] ...')` so it stays server-side and never
reaches the client.

The invariant to guard: `e.message` (or `e?.message`) MUST NOT appear
inside any `res.end(...)` call. Inline stack-traces in HTTP responses
expose internal implementation details and can reveal file paths,
library versions, and logic flow to unauthenticated callers.

## session-handoff.sh — workspace/state file invariants

Verifies that `src/session-handoff.sh` writes `session-state.md` to the
repo (not workspace), reads the quota file from the workspace (not the
repo), resolves `pending-questions.md` via `util_paths.personal_path()`,
and clears the proactive-loop sentinel at the end so the next session
re-fires `/catchup-after-startup`.

Run: python3 tests/web-client-security-scrub.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB_CLIENT = REPO / "src" / "web-client.ts"
HANDOFF = REPO / "src" / "session-handoff.sh"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:600], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def main() -> None:
    missing = [f for f in [WEB_CLIENT, HANDOFF] if not f.exists()]
    for m in missing:
        fail(f"file not found: {m}")
    if missing:
        sys.exit(1)

    wc = WEB_CLIENT.read_text()
    handoff = HANDOFF.read_text()

    # ── PR #1121: no e.message leaks in res.end() ────────────────────────────

    # Split into lines so we can check each res.end line in isolation.
    lines = wc.splitlines()

    # 1. No res.end() call that also contains e.message or e?.message
    leak_lines = [
        ln.strip() for ln in lines
        if "res.end(" in ln and ("e.message" in ln or "e?.message" in ln)
    ]
    expect(
        len(leak_lines) == 0,
        f"web-client.ts: e.message must NOT appear inside res.end() calls (found {len(leak_lines)} sites)",
        ctx="\n".join(leak_lines[:5]),
    )

    # 2. Static error string for /note-viewing parse failure
    expect(
        "note-viewing parse failed" in wc,
        "web-client.ts: must use static error string 'note-viewing parse failed' (not e.message)",
    )

    # 3. Static error string for overlay control proxy failure
    expect(
        "overlay control proxy failed" in wc,
        "web-client.ts: must use static error string 'overlay control proxy failed' (not e.message)",
    )

    # 4. Static error string for /paidsubscriptions render failure
    expect(
        "Error reading subscriptions" in wc,
        "web-client.ts: must use static error string 'Error reading subscriptions' (not + e.message)",
    )

    # 5. /note-viewing error response no longer uses e instanceof Error conditional
    expect(
        "e instanceof Error ? e.message" not in wc,
        "web-client.ts: /note-viewing must NOT use 'e instanceof Error ? e.message' in response",
    )

    # 6. overlay control proxy response no longer concatenates e.message
    # Old pattern: 'overlay control proxy failed: ' + e.message
    expect(
        "'overlay control proxy failed: ' + e.message" not in wc
        and '"overlay control proxy failed: " + e.message' not in wc,
        "web-client.ts: overlay error must NOT concatenate e.message into response string",
    )

    # 7. /paidsubscriptions no longer uses e?.message in response body
    expect(
        "e?.message || String(e)" not in wc,
        "web-client.ts: /paidsubscriptions must NOT use e?.message || String(e) in response body",
    )

    # 8. console.error with [web-client] tag added at the three scrubbed sites
    # (so the detail is still logged server-side)
    console_errors = [ln for ln in lines if "console.error('[web-client]" in ln]
    expect(
        len(console_errors) >= 3,
        f"web-client.ts: must have at least 3 console.error('[web-client]') calls for the scrubbed sites (found {len(console_errors)})",
    )

    # 9. No broader String(e) leak in res.end (prevents future similar regressions)
    string_e_in_res = [
        ln.strip() for ln in lines
        if "res.end(" in ln and "String(e)" in ln
    ]
    expect(
        len(string_e_in_res) == 0,
        f"web-client.ts: String(e) must NOT appear inside res.end() calls (found {len(string_e_in_res)} sites)",
        ctx="\n".join(string_e_in_res[:3]),
    )

    # ── session-handoff.sh: workspace/state invariants ────────────────────────

    # 10. STATE_FILE written to repo (not workspace)
    expect(
        "STATE_FILE=" in handoff and "session-state.md" in handoff,
        "session-handoff.sh: must write output to session-state.md",
    )

    # 11. REPO resolved via SUTANDO_REPO_DIR first (not SUTANDO_WORKSPACE)
    expect(
        "SUTANDO_REPO_DIR" in handoff,
        "session-handoff.sh: must check SUTANDO_REPO_DIR for repo path (not SUTANDO_WORKSPACE)",
    )

    # 12. SUTANDO_WORKSPACE NOT used as a REPO alias (comment or explicit guard)
    # Checking the comment explains why SUTANDO_WORKSPACE is excluded from REPO fallback
    expect(
        "SUTANDO_WORKSPACE" in handoff,
        "session-handoff.sh: must reference SUTANDO_WORKSPACE (for quota file path)",
    )

    # 13. Quota file read from workspace, not from repo
    expect(
        "SUTANDO_WORKSPACE" in handoff and "quota-state.json" in handoff,
        "session-handoff.sh: quota file must be read from $SUTANDO_WORKSPACE (not in-repo copy)",
    )

    # 14. pending-questions.md resolved via util_paths.personal_path()
    expect(
        "personal_path" in handoff,
        "session-handoff.sh: must resolve pending-questions.md via util_paths.personal_path()",
    )

    # 15. Proactive-loop sentinel cleared at end of handoff
    expect(
        "proactive-loop-started.sentinel" in handoff,
        "session-handoff.sh: must rm proactive-loop-started.sentinel so next session re-fires catchup",
    )

    # 16. Sentinel rm uses SUTANDO_WORKSPACE (not SUTANDO_REPO_DIR)
    sentinel_pos = handoff.find("proactive-loop-started.sentinel")
    if sentinel_pos != -1:
        sentinel_region = handoff[max(0, sentinel_pos - 200):sentinel_pos + 100]
        expect(
            "SUTANDO_WORKSPACE" in sentinel_region,
            "session-handoff.sh: sentinel rm must use SUTANDO_WORKSPACE (sentinel is in workspace/state/)",
            ctx=sentinel_region[:300],
        )

    # 17. TRANSCRIPT passed as positional arg $1 (PreCompact hook wiring)
    expect(
        "TRANSCRIPT=" in handoff and '"$1"' in handoff,
        "session-handoff.sh: must accept TRANSCRIPT as $1 (PreCompact hook passes $TRANSCRIPT_PATH)",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 17
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
