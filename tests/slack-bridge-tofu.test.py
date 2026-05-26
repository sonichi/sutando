#!/usr/bin/env python3
"""
Structural regression tests for slack-bridge.py TOFU + load_allowed() (port
of the behavioral guards from tests/telegram-bridge-tofu.test.py).

slack_bolt is an optional heavy dep — importing the module in CI is not
reliable. These tests source-inspect src/slack-bridge.py with regex, matching
the convention in tests/slack-bridge-access.test.py.

Guards:
  (a) tofu_onboard() writes the file with the correct payload shape
      (allowFrom, tofuOwner, tofuOnboardedAt, tofuOnboardedUsername).
  (b) Race-safety: ACCESS_FILE.exists() guard prevents clobber.
  (c) chmod 0o600 applied after write — same security fix as #810 for Slack.
  (d) username or None pattern: None username stored as null, not the string "None".
  (e) py39-compat: str | None type annotation on tofu_onboard is present AND
      from __future__ import annotations appears BEFORE the function def.
  (f) Tri-state: load_allowed() returns None on FileNotFoundError (TOFU signal),
      set() on other exceptions (fail-closed), and set(allowFrom) on success.

Run: python3 tests/slack-bridge-tofu.test.py
Exit code: 0 on pass, 1 on fail.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "slack-bridge.py"


def fail(msg: str, context: str = "") -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    if context:
        print("---context---", file=sys.stderr)
        print(context[:1500], file=sys.stderr)
    raise AssertionError(msg)


def _load_src() -> str:
    if not BRIDGE.exists():
        fail(f"{BRIDGE} not found")
    return BRIDGE.read_text()


def _extract_tofu_onboard_body(src: str) -> str:
    m = re.search(
        r"def tofu_onboard\([^)]*\)[^:]*:\s*\n([\s\S]{0,2000}?)(?=\n\ndef |\Z)",
        src,
    )
    if not m:
        fail("tofu_onboard() function not found in slack-bridge.py")
    return m.group(1)


def _extract_load_allowed_body(src: str) -> str:
    m = re.search(
        r"def load_allowed\(\)[^:]*:\s*\n([\s\S]{0,1500}?)(?=\n\ndef |\Z)",
        src,
    )
    if not m:
        fail("load_allowed() function not found in slack-bridge.py")
    return m.group(1)


# -------- (f) Tri-state load_allowed() --------

def test_load_allowed_returns_none_on_file_not_found():
    """(f) FileNotFoundError → return None (TOFU-eligible sentinel)."""
    body = _extract_load_allowed_body(_load_src())
    if not re.search(r"except\s+FileNotFoundError:\s*\n\s+return\s+None", body):
        fail(
            "load_allowed must `return None` on FileNotFoundError "
            "(None vs empty-set TOFU distinction)", body
        )


def test_load_allowed_fail_closed_on_other_exceptions():
    """(f) Non-FileNotFoundError exceptions → return set() (fail-closed)."""
    body = _extract_load_allowed_body(_load_src())
    # Should have a bare `except` or `except Exception` that returns set()
    if not re.search(r"except[^F][^i].*:\s*\n\s+return\s+set\(\)", body):
        fail(
            "load_allowed must return set() (not None) for non-FileNotFoundError "
            "exceptions — fail-closed to avoid false TOFU triggers", body
        )


def test_load_allowed_returns_set_on_success():
    """(f) Valid file → return set(allowFrom)."""
    body = _extract_load_allowed_body(_load_src())
    if not re.search(r"return\s+set\(", body):
        fail("load_allowed must return a set() of allowFrom IDs on success", body)


# -------- (a) tofu_onboard payload shape --------

def test_tofu_writes_allowfrom_and_tofu_owner():
    """(a) Payload must include allowFrom and tofuOwner."""
    body = _extract_tofu_onboard_body(_load_src())
    if '"allowFrom"' not in body and "'allowFrom'" not in body:
        fail('tofu_onboard payload missing "allowFrom" key', body)
    if '"tofuOwner"' not in body and "'tofuOwner'" not in body:
        fail('tofu_onboard payload missing "tofuOwner" key', body)


def test_tofu_writes_tofu_onboarded_at():
    """(a) Payload must include tofuOnboardedAt timestamp."""
    body = _extract_tofu_onboard_body(_load_src())
    if "tofuOnboardedAt" not in body:
        fail('tofu_onboard payload missing "tofuOnboardedAt" key', body)


def test_tofu_writes_username_field():
    """(a) Payload must include tofuOnboardedUsername."""
    body = _extract_tofu_onboard_body(_load_src())
    if "tofuOnboardedUsername" not in body:
        fail('tofu_onboard payload missing "tofuOnboardedUsername" key', body)


# -------- (b) Race-safety --------

def test_tofu_race_guard_exists():
    """(b) `if ACCESS_FILE.exists()` guard must appear before the write."""
    body = _extract_tofu_onboard_body(_load_src())
    if not re.search(r"if\s+ACCESS_FILE\.exists\(\)", body):
        fail(
            "tofu_onboard must guard against clobbering an existing access.json "
            "with `if ACCESS_FILE.exists()` — race-safety for concurrent TOFU triggers",
            body,
        )


# -------- (c) chmod 0o600 --------

def test_tofu_chmod_600():
    """(c) chmod 0o600 must be applied after write."""
    body = _extract_tofu_onboard_body(_load_src())
    if not re.search(r"os\.chmod\s*\(\s*ACCESS_FILE\s*,\s*0o600\s*\)", body):
        fail(
            "tofu_onboard must chmod ACCESS_FILE to 0o600 — "
            "owner's Slack user ID must not be world-readable", body
        )


# -------- (d) None username stored as null --------

def test_tofu_username_none_stored_as_null():
    """(d) `username or None` pattern ensures None → null, not string 'None'."""
    body = _extract_tofu_onboard_body(_load_src())
    # The value assigned to tofuOnboardedUsername must use `or None` / `if username`
    # to avoid writing the literal string "None" to JSON.
    if not re.search(r"tofuOnboardedUsername.*(?:username or None|if username)", body):
        fail(
            'tofu_onboard must use `username or None` (or equivalent) for '
            'tofuOnboardedUsername to avoid storing the literal string "None"',
            body,
        )


# -------- (e) py39-compat: str | None annotation --------

def test_tofu_signature_has_str_or_none():
    """(e) tofu_onboard signature uses `str | None` for username param."""
    src = _load_src()
    m = re.search(r"def tofu_onboard\(([^)]+)\)", src)
    if not m:
        fail("tofu_onboard signature not found")
    sig = m.group(1)
    if "str | None" not in sig:
        fail(
            f"tofu_onboard signature must use `str | None` for username — "
            f"got: {sig.strip()!r}"
        )


def test_future_annotations_before_tofu_onboard():
    """(e) `from __future__ import annotations` must appear before tofu_onboard
    def so that `str | None` is valid syntax on Python 3.9 (stock macOS)."""
    src = _load_src()
    future_pos = src.find("from __future__ import annotations")
    tofu_pos = src.find("def tofu_onboard(")
    if future_pos < 0:
        fail("from __future__ import annotations not found in slack-bridge.py")
    if tofu_pos < 0:
        fail("def tofu_onboard( not found in slack-bridge.py")
    if future_pos >= tofu_pos:
        fail(
            "from __future__ import annotations must appear BEFORE def tofu_onboard "
            f"(found at char {future_pos}, tofu_onboard at char {tofu_pos})"
        )


# -------- Driver --------

def main() -> int:
    tests = [
        # (f) tri-state load_allowed
        test_load_allowed_returns_none_on_file_not_found,
        test_load_allowed_fail_closed_on_other_exceptions,
        test_load_allowed_returns_set_on_success,
        # (a) payload shape
        test_tofu_writes_allowfrom_and_tofu_owner,
        test_tofu_writes_tofu_onboarded_at,
        test_tofu_writes_username_field,
        # (b) race-safety
        test_tofu_race_guard_exists,
        # (c) chmod
        test_tofu_chmod_600,
        # (d) None username
        test_tofu_username_none_stored_as_null,
        # (e) py39-compat
        test_tofu_signature_has_str_or_none,
        test_future_annotations_before_tofu_onboard,
    ]

    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}", file=sys.stderr)
            failures += 1
        except Exception as e:
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}", file=sys.stderr)
            failures += 1

    print()
    if failures:
        print(f"{failures}/{len(tests)} tests failed", file=sys.stderr)
        return 1
    print(f"All {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
