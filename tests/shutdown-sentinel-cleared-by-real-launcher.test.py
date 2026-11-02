#!/usr/bin/env python3
"""Every production launcher must clear the shutdown sentinel, loudly (#2165).

`restart.sh --stop-only` leaves the sentinel set on purpose — it IS the clean-exit
signal — and `watch-tasks-stream.sh` gates on its bare existence. If the next real
core boot does not clear it, every task that session is silently skipped: a
healthy-looking core that stops answering. `shutdown-sentinel.test.py` cannot see
this; it drives the helper directly and never asks which launchers call it.

This test deliberately does NOT execute `shutdown.py clear`: that helper resolves
the REAL workspace (`resolve_workspace()`), so running it here would clear an
owner's intentional stop. Behaviour of clear itself is covered by the helper suite.

Run: python3 tests/shutdown-sentinel-cleared-by-real-launcher.test.py  (exit 0/1)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# startup.sh is deliberately NOT here: it clears ~850 lines before its own
# `exec start-cli.sh`, so it can never know a core came up (#2165 P1).
LAUNCHERS = {
    "src/agent/claude/cli/start-cli.sh": "the launcher the desktop app actually uses",
    "src/agent/codex/cli/start-cli.sh": "the Codex-runtime launcher",
}
DELEGATES = {
    "src/startup.sh": "always ends in `exec start-cli.sh`, which clears once a core is verified",
}
CLEAR_RE = re.compile(r'^[^\n]*shutdown\.py"?\s+clear[^\n]*$', re.M)
failures: list[str] = []

for rel, why in LAUNCHERS.items():
    path = REPO / rel
    if not path.exists():
        failures.append(f"{rel}: not found ({why})")
        continue
    lines = CLEAR_RE.findall(path.read_text())
    if not lines:
        failures.append(
            f"{rel}: never clears shutdown.sentinel ({why}) — after "
            f"`restart.sh --stop-only` the intake gate holds every task all session")
        continue
    for ln in lines:
        if "2>/dev/null" in ln and re.search(r"\|\|\s*true", ln):
            failures.append(
                f"{rel}: clear discards its own failure (`2>/dev/null || true`) — "
                f"a failed transition then reports success")
        if re.search(r"(^|\s)python3\s", ln):
            failures.append(
                f"{rel}: clear runs a bare `python3`, which can resolve to the "
                f"Xcode-CLT stub; route it through the resolved interpreter")

for rel, why in DELEGATES.items():
    path = REPO / rel
    if path.exists() and CLEAR_RE.search(path.read_text()):
        failures.append(
            f"{rel}: clears the sentinel itself, but {why} — a clear here precedes "
            f"every way the launch can still fail, opening intake with no core")

if failures:
    for f in failures:
        print(f"FAIL: {f}")
    print(f"\nResults: {len(failures)} failure(s)")
    sys.exit(1)
print("OK: both CLI launchers clear the sentinel via a resolved interpreter without "
      "discarding failure, and startup.sh delegates rather than clearing early")

def test_clear_is_past_every_reuse_exit_in_the_claude_launcher():
    """Attaching to a live core must NOT clear: that cancels a --stop-only that is
    still waiting to be observed. Ordering, not presence."""
    src = (REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh").read_text(encoding="utf-8")
    reuse = src.index("already running.")            # the attach/no-op exit
    # invocations only — the definition legitimately precedes every call site
    calls = [m.start() for m in re.finditer(r"^\s*clear_shutdown_sentinel\s*(?:#.*)?$", src, re.M)]
    assert calls, "no clear_shutdown_sentinel call site"
    early = [c for c in calls if c < reuse]
    assert not early, f"clear runs before the reuse exit at {len(early)} site(s) — attach would cancel --stop-only"


def test_codex_launcher_clears_too():
    """The dispatcher can select codex; a core boot there must clear the sentinel."""
    src = (REPO / "src" / "agent" / "codex" / "cli" / "start-cli.sh").read_text(encoding="utf-8")
    # Match the INVOCATION, not the substring: the failure message on the next
    # line also contains "shutdown.py clear" and would satisfy a bare `in`.
    m = re.search(r'"\$\w+"\s+"\$REPO/src/shutdown\.py"\s+clear\b', src)
    assert m, "codex launcher never INVOKES shutdown.py clear"
    reuse = src.index("already running (codex).")
    call = m.start()
    assert call > reuse, "codex clear runs before its reuse exit"


test_clear_is_past_every_reuse_exit_in_the_claude_launcher()
test_codex_launcher_clears_too()
print("Results: all assertions passed")
