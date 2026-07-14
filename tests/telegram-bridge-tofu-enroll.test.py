#!/usr/bin/env python3
"""
Structural + module-state tests for Telegram bridge TOFU enrollment-code gate.

Security finding #3: without a gate, the first DM to the Telegram bot
auto-enrolls the sender as owner via TOFU. The fix generates a 6-char hex
enrollment code at startup (printed to the operator log) and requires the
code to be present in the first DM before auto-enrolling.

This test verifies:
  (a) Module-level _TOFU_ENROLLMENT_CODE starts as None after import.
  (b) The module exposes _TOFU_ENROLLMENT_CODE so main() can set it.
  (c) Structural check: enrollment code generation is wired into main().
  (d) Structural check: the gate condition is present in the poll loop.
  (e) Structural check: code is RETAINED (not cleared) after tofu_onboard, so
      a later external access.json deletion (#899) re-enters TOFU state still
      gated. Functional proof of the enroll→delete→reject sequence lives in
      tests/slack-bridge-tofu-enroll.test.py (identical gate logic).
  (f) Structural check: startup banner prints are wired (operator-visible log).

Run: python3 tests/telegram-bridge-tofu-enroll.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE_SRC = REPO / "src" / "telegram-bridge.py"


def _load_bridge():
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-placeholder-token")
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge_tofu_enroll", BRIDGE_SRC
    )
    sys.path.insert(0, str(REPO / "src"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BRIDGE = _load_bridge()
SRC = BRIDGE_SRC.read_text()


def _main_body() -> str:
    """Extract the text starting at 'def main():' (first 6000 chars)."""
    pos = SRC.find("def main():")
    assert pos != -1, "def main(): not found in telegram-bridge.py"
    return SRC[pos: pos + 6000]


# ---------------------------------------------------------------------------
# (a) Module-level _TOFU_ENROLLMENT_CODE defaults to None
# ---------------------------------------------------------------------------

def test_tofu_enrollment_code_default_is_none():
    """(a) _TOFU_ENROLLMENT_CODE is None on fresh module load (bridge not in TOFU state)."""
    assert hasattr(BRIDGE, "_TOFU_ENROLLMENT_CODE"), (
        "bridge module must expose _TOFU_ENROLLMENT_CODE"
    )
    assert BRIDGE._TOFU_ENROLLMENT_CODE is None, (
        f"expected None (no TOFU state at module load), got {BRIDGE._TOFU_ENROLLMENT_CODE!r}"
    )


# ---------------------------------------------------------------------------
# (b) Module state is writable (main() can set it)
# ---------------------------------------------------------------------------

def test_tofu_enrollment_code_is_settable():
    """(b) Module-level _TOFU_ENROLLMENT_CODE can be set — confirms global scope works."""
    orig = BRIDGE._TOFU_ENROLLMENT_CODE
    try:
        BRIDGE._TOFU_ENROLLMENT_CODE = "abc999"
        assert BRIDGE._TOFU_ENROLLMENT_CODE == "abc999", (
            "module-level _TOFU_ENROLLMENT_CODE must be writable"
        )
    finally:
        BRIDGE._TOFU_ENROLLMENT_CODE = orig


# ---------------------------------------------------------------------------
# (c) Structural: code generation wired into main()
# ---------------------------------------------------------------------------

def test_main_generates_enrollment_code():
    """(c) main() generates secrets.token_hex enrollment code when ACCESS_FILE missing."""
    body = _main_body()
    assert re.search(r"secrets\.token_hex\(", body), (
        "main() must call secrets.token_hex() to generate the enrollment code"
    )
    assert re.search(r"_TOFU_ENROLLMENT_CODE\s*=\s*secrets\.token_hex", body), (
        "main() must assign the generated code to _TOFU_ENROLLMENT_CODE"
    )
    assert re.search(r"if not ACCESS_FILE\.exists\(\)", body), (
        "code generation must be conditional on ACCESS_FILE not existing"
    )


# ---------------------------------------------------------------------------
# (d) Structural: gate condition in poll loop
# ---------------------------------------------------------------------------

def test_poll_loop_contains_gate_condition():
    """(d) Enrollment gate check (_TOFU_ENROLLMENT_CODE not in text) is in the poll loop."""
    assert re.search(
        r"_TOFU_ENROLLMENT_CODE\s+and\s+_TOFU_ENROLLMENT_CODE\s+not\s+in",
        SRC,
    ), (
        "TOFU enrollment gate check must appear in the bridge source "
        "(_TOFU_ENROLLMENT_CODE not in text)"
    )


# ---------------------------------------------------------------------------
# (e) Structural: code RETAINED (not cleared) after successful enrollment
# ---------------------------------------------------------------------------

def test_enrollment_code_retained_after_onboard():
    """(e) _TOFU_ENROLLMENT_CODE must NOT be cleared after tofu_onboard.

    Security finding #3 follow-up: clearing the code left the gate inert if
    access.json was later deleted externally (#899) — the next DM re-entered
    tofu_onboard() with no code check. Keeping the code valid for the process
    lifetime keeps the gate armed on that re-TOFU path."""
    assert not re.search(
        r"tofu_onboard\([^)]*\)\s*\n\s*_TOFU_ENROLLMENT_CODE\s*=\s*None",
        SRC,
    ), (
        "_TOFU_ENROLLMENT_CODE must NOT be cleared immediately after tofu_onboard() — "
        "keeping it valid keeps the post-deletion (#899) re-TOFU path gated"
    )


# ---------------------------------------------------------------------------
# (f) Structural: startup banner is printed (operator-visible log)
# ---------------------------------------------------------------------------

def test_startup_banner_present():
    """(f) main() prints an operator-visible enrollment code banner."""
    body = _main_body()
    assert re.search(r"Enrollment code:", body), (
        "main() must print 'Enrollment code: ...' so the operator can see it"
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        ("a-default-none", test_tofu_enrollment_code_default_is_none),
        ("b-settable", test_tofu_enrollment_code_is_settable),
        ("c-code-gen-in-main", test_main_generates_enrollment_code),
        ("d-gate-in-poll-loop", test_poll_loop_contains_gate_condition),
        ("e-code-retained-after-onboard", test_enrollment_code_retained_after_onboard),
        ("f-banner-printed", test_startup_banner_present),
    ]
    failures = 0
    for label, fn in tests:
        try:
            fn()
            print(f"PASS: {label}")
        except AssertionError as e:
            print(f"FAIL: {label} — {e}", file=sys.stderr)
            failures += 1
        except Exception as e:
            print(f"ERROR: {label} — {type(e).__name__}: {e}", file=sys.stderr)
            failures += 1

    if failures:
        print(f"\n{failures}/{len(tests)} tests failed.", file=sys.stderr)
        return 1
    print(f"\nAll {len(tests)} Telegram TOFU enrollment gate tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
