#!/usr/bin/env python3
"""First-coverage unit tests for `src/telegram-bridge.py`.

Telegram bridge ships 613 LOC with zero tests, so any regression — pairing,
allowlist, TOFU onboarding, file-send gating — has been landing in
production blind. This file pins the access-control surface: what
`load_allowed()` returns under each access.json state, and what
`tofu_onboard()` writes on first install.

Mirrors tests/discord-chunker.test.py conventions:
- importlib loads the hyphenated module
- `TELEGRAM_BOT_TOKEN` is materialized so the bridge doesn't `exit(1)`
- `SUTANDO_WORKSPACE` points at a temp dir to keep workspace bootstrap
  side effects (TASKS_DIR/RESULTS_DIR.mkdir on import) out of the repo
- `ACCESS_FILE` is rebound after import so each case uses a fresh file
"""

import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Module-load needs both env vars set. SUTANDO_WORKSPACE redirects the
# bridge's TASKS_DIR/RESULTS_DIR/STATE_DIR.mkdir(...) calls into a temp dir
# so importing the bridge doesn't touch the user's real workspace or the
# repo tree. TELEGRAM_BOT_TOKEN is gated with `exit(1)` if absent.
_WORKSPACE_TMP = tempfile.mkdtemp(prefix="sutando-telegram-test-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE_TMP
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bridge = _load("tbridge", REPO / "src" / "telegram-bridge.py")


def _with_access_file(fn):
    """Run `fn(access_path)` against a fresh ACCESS_FILE override. Restores
    the original after, even on failure, so other cases see a clean slate."""

    def wrapper():
        original = bridge.ACCESS_FILE
        tmpdir = tempfile.mkdtemp(prefix="sutando-telegram-access-")
        access_path = Path(tmpdir) / "access.json"
        bridge.ACCESS_FILE = access_path
        try:
            fn(access_path)
        finally:
            bridge.ACCESS_FILE = original
            # Best-effort cleanup; tempdir hangs around if test failed,
            # which is fine — `tempfile` cleans on reboot.
            if access_path.exists():
                access_path.unlink()
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass

    return wrapper


@_with_access_file
def test_load_allowed_returns_none_when_file_absent(access_path):
    """File-missing is the TOFU window signal: the next DM auto-onboards
    the sender as owner. `None` MUST be distinguishable from an empty set
    (which means the admin locked the bridge down) — see the docstring of
    `load_allowed`. Conflating the two would silently re-enable TOFU on a
    locked install."""
    assert not access_path.exists()
    got = bridge.load_allowed()
    assert got is None, f"expected None for missing file, got {got!r}"


@_with_access_file
def test_load_allowed_returns_empty_set_when_allowfrom_empty(access_path):
    """`{"allowFrom": []}` means the admin explicitly locked the bridge
    — `load_allowed()` must return a (truthy-`is-not-None`) empty set so
    main-loop TOFU does NOT fire on the next DM."""
    access_path.write_text(json.dumps({"allowFrom": []}))
    got = bridge.load_allowed()
    assert got == set(), f"expected empty set, got {got!r}"
    assert got is not None  # distinct from the TOFU window signal


@_with_access_file
def test_load_allowed_returns_string_set_for_populated_allowfrom(access_path):
    """Telegram user IDs are stored as strings in access.json (matches the
    `sender_id = str(msg["from"]["id"])` cast in the main loop). The
    membership check `sender_id not in allowed` only works when both sides
    are strings."""
    access_path.write_text(json.dumps({"allowFrom": ["111", "222", "333"]}))
    got = bridge.load_allowed()
    assert got == {"111", "222", "333"}
    assert all(isinstance(x, str) for x in got)


@_with_access_file
def test_load_allowed_failsafe_on_malformed_json(access_path):
    """Corrupted access.json must NOT raise — `load_allowed()` catches
    `Exception` and returns an empty set. Fail-closed: a broken file
    rejects all senders rather than throwing in the polling loop."""
    access_path.write_text("{ this is not json")
    got = bridge.load_allowed()
    assert got == set(), f"expected empty-set fail-closed, got {got!r}"


@_with_access_file
def test_tofu_onboard_writes_access_file_with_sender(access_path):
    """First-install path: file is absent, a DM arrives, `tofu_onboard`
    records the sender as the sole allowlisted user and stamps the TOFU
    metadata so the act is auditable later."""
    assert not access_path.exists()
    got = bridge.tofu_onboard("12345", "alice")
    assert got == {"12345"}
    assert access_path.exists()
    payload = json.loads(access_path.read_text())
    assert payload["allowFrom"] == ["12345"]
    assert payload["tofuOwner"] == "12345"
    assert payload["tofuOnboardedUsername"] == "alice"
    assert isinstance(payload["tofuOnboardedAt"], int)


@_with_access_file
def test_tofu_onboard_writes_mode_600(access_path):
    """access.json holds the owner's Telegram user ID — a stable identifier
    that should not be world-readable. The bridge explicitly chmods to 0o600
    to override umask 022 (which would otherwise leave it 0o644)."""
    bridge.tofu_onboard("12345", "alice")
    mode = stat.S_IMODE(access_path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


@_with_access_file
def test_tofu_onboard_does_not_clobber_existing_file(access_path):
    """Race-safety: if access.json materializes between the
    `not ACCESS_FILE.exists()` check in main() and the `tofu_onboard()`
    call (e.g., admin runs `/telegram:access allow` concurrently with the
    first DM), TOFU must NOT overwrite the explicit config."""
    access_path.write_text(json.dumps({"allowFrom": ["existing-owner-id"]}))
    original = access_path.read_text()
    got = bridge.tofu_onboard("intruder-id", "intruder")
    # The intruder must not appear; the existing config must be untouched.
    assert access_path.read_text() == original
    assert got == {"existing-owner-id"}
    assert "intruder-id" not in got


@_with_access_file
def test_tofu_onboard_handles_missing_username(access_path):
    """Telegram users without a `@username` set send `msg["from"].get("username", sender_id)`
    as the fallback in the main loop, but `tofu_onboard` may also receive an
    empty/None value if the caller resolves it differently. `tofuOnboardedUsername`
    must record `None` rather than crash, so the access.json stays valid."""
    bridge.tofu_onboard("12345", None)
    payload = json.loads(access_path.read_text())
    assert payload["tofuOnboardedUsername"] is None


def main():
    test_load_allowed_returns_none_when_file_absent()
    test_load_allowed_returns_empty_set_when_allowfrom_empty()
    test_load_allowed_returns_string_set_for_populated_allowfrom()
    test_load_allowed_failsafe_on_malformed_json()
    test_tofu_onboard_writes_access_file_with_sender()
    test_tofu_onboard_writes_mode_600()
    test_tofu_onboard_does_not_clobber_existing_file()
    test_tofu_onboard_handles_missing_username()
    print("All telegram-bridge allowlist tests passed.")


if __name__ == "__main__":
    main()
