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

    # Shape-valid but SEMANTICALLY impossible (#2516 second review round). The
    # full-shape regex above pins field widths only, so each of these matched
    # and then lexically compared as future — holding the gate open forever on a
    # corrupted sentinel. Mirrored case-for-case in tests/presenter-mode.test.ts.
    #
    # The rollover cases are why a bare parse is not enough: the TS twin's Date
    # ACCEPTS 2026-02-30 and silently means 2026-03-02, so both twins require
    # the canonical round-trip to equal the input.
    for value, why in [
        ("9999-99-99T99:99:99Z", "impossible in every field"),
        ("2026-13-01T00:00:00Z", "month 13"),
        ("2026-00-01T00:00:00Z", "month 00"),
        ("2026-01-32T00:00:00Z", "day 32"),
        ("2027-01-01T24:00:00Z", "hour 24"),
        ("2027-02-30T00:00:00Z", "Feb 30 — rolls over rather than failing"),
        ("2027-06-31T00:00:00Z", "Jun 31 — rolls over rather than failing"),
        ("2027-02-29T00:00:00Z", "Feb 29 in a non-leap year"),
    ]:
        sentinel.write_text(value)
        check(f"shape-valid but impossible sentinel is inactive: {value} ({why})",
              not presenter_mode.presenter_mode_active(workspace, now=0))

    # CONTROLS — without these the fix could pass by rejecting everything, which
    # would silently disable presenter mode instead of hardening it.
    sentinel.write_text("2028-02-29T00:00:00Z")
    check("CONTROL: a real leap day is still ACTIVE",
          presenter_mode.presenter_mode_active(workspace, now=0))

    sentinel.write_text("2099-12-31T23:59:59Z")
    check("CONTROL: a genuinely future expiry is still ACTIVE",
          presenter_mode.presenter_mode_active(workspace, now=0))

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
