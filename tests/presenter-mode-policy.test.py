#!/usr/bin/env python3
"""Contract tests for the shared presenter-mode sentinel policy."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import presenter_mode  # noqa: E402

failures = []


def check(name, condition):
    print(("  ok  " if condition else "  FAIL ") + name)
    if not condition:
        failures.append(name)


with tempfile.TemporaryDirectory(prefix="sutando-presenter-mode-") as tmp:
    workspace = Path(tmp)
    sentinel = workspace / "state" / "presenter-mode.sentinel"

    check("missing sentinel is inactive", not presenter_mode.presenter_mode_active(workspace, now=0))

    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("")
    check("empty sentinel is inactive", not presenter_mode.presenter_mode_active(workspace, now=0))

    sentinel.write_text("garbage")
    check("malformed sentinel is inactive", not presenter_mode.presenter_mode_active(workspace, now=0))

    # Digit-prefixed malformed (#2516 review canary): starts with a digit and
    # lexically compares as future, so a first-byte check + raw compare reads
    # it ACTIVE — the documented fail-closed contract requires full-shape validation.
    sentinel.write_text("9999-not-a-date")
    check("digit-prefixed malformed sentinel is inactive (fail closed)",
          not presenter_mode.presenter_mode_active(workspace, now=0))

    sentinel.write_text("1970-01-01T00:00:01Z\n")
    check("future expiry is active", presenter_mode.presenter_mode_active(workspace, now=0))

    sentinel.write_text("1970-01-01T00:00:00Z")
    check("expiry is exclusive", not presenter_mode.presenter_mode_active(workspace, now=0))

    sentinel.write_text("1969-12-31T23:59:59Z")
    check("past expiry is inactive", not presenter_mode.presenter_mode_active(workspace, now=0))

    sentinel.unlink()
    sentinel.mkdir()
    check("unreadable sentinel fails closed", not presenter_mode.presenter_mode_active(workspace, now=0))

# Regression guard: consumers import the policy instead of growing another
# private copy. Provider-specific suppression remains in each caller.
consumers = {
    "discord": (REPO / "src" / "discord-bridge.py", "presenter_mode_active(REPO)"),
    "slack": (REPO / "src" / "slack-bridge.py", "presenter_mode_active(REPO)"),
    "telegram": (REPO / "src" / "telegram-bridge.py", "presenter_mode_active(REPO)"),
    "pending questions": (
        REPO / "src" / "check-pending-questions.py",
        "presenter_mode_active(WORKSPACE)",
    ),
}
for name, (path, call) in consumers.items():
    source = path.read_text()
    check(f"{name} imports shared presenter policy", "from presenter_mode import presenter_mode_active" in source)
    check(f"{name} delegates to shared presenter policy", call in source)
    check(f"{name} has no private presenter policy", "def presenter_mode_active" not in source)

if failures:
    print(f"\nFAIL — {len(failures)} check(s) failed: {failures}")
    raise SystemExit(1)
print("\nPASS — presenter-mode policy contract")
