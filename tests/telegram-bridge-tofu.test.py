#!/usr/bin/env python3
"""
Behavioral tests for telegram-bridge's TOFU + tri-state load_allowed().

Closes #811. The TOFU bug-fix in PR #808 rests on a load-bearing tri-state
distinction in load_allowed():

  - None        → access.json doesn't exist → TOFU-eligible
  - set()       → file malformed (fail-closed) OR explicitly empty allowFrom → never TOFU
  - set([ids])  → normal allow check

Without these tests, a future refactor could silently collapse None → set()
and the bridge would regress to the silent-drop bug. Each test pins one
acceptance-criterion case from #811:

  (a) Missing access.json → tofu_onboard() writes the file with sender as
      tofuOwner, message accepted.
  (b) Malformed JSON → load_allowed() returns set() (fail-closed), no TOFU.
  (c) Empty allowFrom: [] → load_allowed() returns set() (admin lockdown),
      no TOFU.
  (d) Pre-existing populated allowFrom → normal allow check works.
  (e) Race-safety: ACCESS_FILE.exists() returning True between
      None-detection and tofu_onboard() write does NOT clobber.
  (f) Tri-state pin: load_allowed() returns the exactly three documented
      types for missing / malformed / valid file.

Plus one chmod test that pins the security fix from #810 (chmod 0o600 on
TOFU-written access.json).

Run: python3 tests/telegram-bridge-tofu.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# telegram-bridge.py imports `task_priority` and `workspace_default` from src/.
sys.path.insert(0, str(REPO / "src"))


def load_bridge_module():
    """Load src/telegram-bridge.py as a module under the name 'telegram_bridge'.

    Hyphenated filename can't be `import`ed directly, hence the importlib
    dance. The module's `if __name__ == '__main__'` guard means the polling
    loop does NOT start on import — only the module-level definitions run.

    Note: src/telegram-bridge.py calls `exit(1)` at module level if
    TELEGRAM_BOT_TOKEN is unset — CI doesn't have one. Set a placeholder
    BEFORE exec_module so the module loads cleanly. The token is never
    used in these tests (no API calls fire), only the access-control
    helpers are exercised.
    """
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-placeholder-token")
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge", REPO / "src" / "telegram-bridge.py"
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge


def with_temp_access_file(test_fn):
    """Decorator: monkey-patch ACCESS_FILE to a per-test tmp path."""

    def wrapper(bridge):
        with tempfile.TemporaryDirectory() as td:
            tmp_access = Path(td) / "access.json"
            orig = bridge.ACCESS_FILE
            bridge.ACCESS_FILE = tmp_access
            try:
                return test_fn(bridge, tmp_access)
            finally:
                bridge.ACCESS_FILE = orig

    wrapper.__name__ = test_fn.__name__
    return wrapper


# -------- (f) Tri-state pin on load_allowed() --------


@with_temp_access_file
def test_load_allowed_missing_file_returns_none(bridge, access_file):
    """(f) load_allowed() returns None when ACCESS_FILE doesn't exist.

    This is the load-bearing distinction TOFU depends on. If a future
    refactor collapses this to set(), the silent-drop bug returns.
    """
    assert not access_file.exists(), "precondition: file shouldn't exist yet"
    result = bridge.load_allowed()
    assert result is None, (
        f"expected None for missing file (TOFU-eligible signal), got {result!r}"
    )


@with_temp_access_file
def test_load_allowed_malformed_returns_empty_set(bridge, access_file):
    """(b, f) Malformed JSON → fail-closed empty set (never TOFU)."""
    access_file.write_text("not-valid-json{")
    result = bridge.load_allowed()
    assert result == set(), (
        f"expected set() for malformed JSON (fail-closed), got {result!r}"
    )
    assert result is not None, "must NOT collapse to None — that would re-trigger TOFU"


@with_temp_access_file
def test_load_allowed_empty_allowlist_returns_empty_set(bridge, access_file):
    """(c, f) {'allowFrom': []} → empty set (admin explicit lockdown)."""
    access_file.write_text('{"allowFrom": []}')
    result = bridge.load_allowed()
    assert result == set(), (
        f"expected set() for empty allowFrom (lockdown), got {result!r}"
    )
    assert result is not None, "explicit lockdown must NOT collapse to None"


@with_temp_access_file
def test_load_allowed_populated_allowlist_returns_set(bridge, access_file):
    """(d, f) Populated allowFrom → set of IDs (normal allow check)."""
    access_file.write_text('{"allowFrom": [123, 456]}')
    result = bridge.load_allowed()
    assert result == {123, 456}, f"got {result!r}"


# -------- (a) TOFU auto-onboard --------


@with_temp_access_file
def test_tofu_onboard_writes_file_with_payload(bridge, access_file):
    """(a) Missing file + tofu_onboard() → file written with full payload."""
    assert not access_file.exists()
    returned = bridge.tofu_onboard(sender_id=789, username="alice")

    assert access_file.exists(), "tofu_onboard should have created the file"
    data = json.loads(access_file.read_text())
    assert data["allowFrom"] == [789], data
    assert data["tofuOwner"] == 789, data
    assert data["tofuOnboardedUsername"] == "alice", data
    assert "tofuOnboardedAt" in data and isinstance(data["tofuOnboardedAt"], int), data
    assert returned == {789}, f"return value should be the new allow-set, got {returned!r}"


@with_temp_access_file
def test_tofu_onboard_handles_missing_username(bridge, access_file):
    """tofu_onboard with username=None records null, not the literal string 'None'."""
    bridge.tofu_onboard(sender_id=42, username=None)
    data = json.loads(access_file.read_text())
    assert data["tofuOnboardedUsername"] is None, data


# -------- (e) Race-safety --------


@with_temp_access_file
def test_tofu_onboard_race_safety_does_not_clobber(bridge, access_file):
    """(e) If ACCESS_FILE exists at write time, tofu_onboard MUST NOT clobber.

    Race scenario: dispatch saw `allowed is None` and called tofu_onboard,
    but between that read and this write, /telegram:configure ran and
    wrote the real access.json. Onboarding must yield to the explicit
    config rather than overwrite it.
    """
    access_file.parent.mkdir(parents=True, exist_ok=True)
    access_file.write_text('{"allowFrom": [99999]}')
    pre = access_file.read_text()

    returned = bridge.tofu_onboard(sender_id=88888, username="newcomer")

    post = access_file.read_text()
    assert pre == post, (
        "tofu_onboard MUST NOT overwrite a file that exists at write time"
    )
    assert returned == {99999}, (
        f"return value should reflect the pre-existing allow-set, got {returned!r}"
    )


# -------- Security follow-up (#810) --------


@with_temp_access_file
def test_tofu_onboard_chmod_600(bridge, access_file):
    """(#810) TOFU-written access.json must be mode 0o600, not 0o644.

    Defense-in-depth: file holds the owner's Telegram user ID; on a shared
    Mac account or multi-user box, 0o644 would expose it to other local
    users. Closes the loop on the chmod patch landed in #813.
    """
    bridge.tofu_onboard(sender_id=1234, username="sec_test")
    mode = access_file.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600 (defense-in-depth), got {oct(mode)}"


# -------- Driver --------


def main():
    bridge = load_bridge_module()

    tests = [
        # (f) tri-state pin
        test_load_allowed_missing_file_returns_none,
        test_load_allowed_malformed_returns_empty_set,
        test_load_allowed_empty_allowlist_returns_empty_set,
        test_load_allowed_populated_allowlist_returns_set,
        # (a) TOFU happy path
        test_tofu_onboard_writes_file_with_payload,
        test_tofu_onboard_handles_missing_username,
        # (e) race-safety
        test_tofu_onboard_race_safety_does_not_clobber,
        # #810 chmod
        test_tofu_onboard_chmod_600,
    ]

    failures = 0
    for t in tests:
        try:
            t(bridge)
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}", file=sys.stderr)
            failures += 1
        except Exception as e:
            print(
                f"ERROR: {t.__name__}: {type(e).__name__}: {e}", file=sys.stderr
            )
            failures += 1

    if failures:
        print(f"\n{failures}/{len(tests)} tests failed", file=sys.stderr)
        return 1
    print(f"\nAll {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
