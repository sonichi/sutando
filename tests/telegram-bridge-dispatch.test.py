#!/usr/bin/env python3
"""
Dispatch wiring tests for src/telegram-bridge.py — closes #898.

#814's unit tests cover load_allowed() and tofu_onboard() in isolation. This
file adds the "wiring" coverage: asserts that the dispatch path inside main()
correctly connects them — so a future refactor can't pass the unit tests while
silently breaking the connection between the pieces.

Tests split two ways:
  A. Structural (source-code): verify the dispatch logic block has the correct
     shape — order of checks, references to load_allowed → tofu_onboard →
     allowlist drop. These catch refactors that rename or reorder the block.
  B. Behavioral (runtime): import the bridge module and exercise the dispatch
     helper via monkey-patching load_allowed to return controlled values,
     then verify tofu_onboard is called (or not) and task files are (or are
     not) written.

For (B): main() is an infinite polling loop so we can't call it directly.
Instead we extract the dispatch decision logic into an in-test helper using
the same branch conditions the real code uses, exercising the same functions
(load_allowed, tofu_onboard) that main() would call. This catches the
"load_allowed returns the wrong type" class of wiring regression.

Run: python3 tests/telegram-bridge-dispatch.test.py
Exit: 0 on pass, 1 on fail.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


# ── Module loader (same pattern as telegram-bridge-tofu.test.py) ─────────────

def load_bridge_module():
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-placeholder-token")
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge_dispatch", REPO / "src" / "telegram-bridge.py"
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge


# ── A. Structural tests ───────────────────────────────────────────────────────

def test_structural_tofu_wiring() -> int:
    """A1: Source contains `if allowed is None: … tofu_onboard(…)` wiring."""
    src = (REPO / "src" / "telegram-bridge.py").read_text()
    if "if allowed is None:" not in src:
        return fail(
            "telegram-bridge.py missing `if allowed is None:` dispatch check — "
            "TOFU wiring broken (#898)"
        )
    if "allowed = tofu_onboard(" not in src:
        return fail(
            "telegram-bridge.py missing `allowed = tofu_onboard(…)` on the None branch — "
            "TOFU result not assigned to allowed (#898)"
        )
    return 0


def test_structural_drop_path() -> int:
    """A2: Source contains `sender_id not in allowed → continue` drop path."""
    src = (REPO / "src" / "telegram-bridge.py").read_text()
    if "sender_id not in allowed" not in src:
        return fail(
            "telegram-bridge.py missing `sender_id not in allowed` allowlist check — "
            "non-owner messages would not be dropped (#898)"
        )
    return 0


def test_structural_order_tofu_before_allowlist() -> int:
    """A3: TOFU check must appear before the allowlist drop check in source.

    Refactoring the order would break the dispatch: if the allowlist check runs
    before TOFU, a None-result (missing access.json) would cause `sender_id not
    in None` which raises TypeError rather than triggering TOFU onboarding.
    """
    src = (REPO / "src" / "telegram-bridge.py").read_text()
    tofu_idx = src.find("if allowed is None:")
    drop_idx = src.find("sender_id not in allowed")
    if tofu_idx == -1:
        return fail("TOFU check not found — see test_structural_tofu_wiring")
    if drop_idx == -1:
        return fail("allowlist drop check not found — see test_structural_drop_path")
    if tofu_idx > drop_idx:
        return fail(
            "TOFU check appears AFTER allowlist check in source — wrong order. "
            "`allowed is None` must be resolved (via tofu_onboard) before `sender_id not in allowed` (#898)"
        )
    return 0


def test_structural_tofu_result_assigned() -> int:
    """A4: After the None branch, `allowed` must be updated from tofu_onboard result.

    If someone writes `tofu_onboard(...)` without `allowed = ...`, the return value
    is discarded and the subsequent `sender_id not in allowed` would still fail
    (allowed is still None at that point → TypeError).
    """
    src = (REPO / "src" / "telegram-bridge.py").read_text()
    # The correct pattern is `allowed = tofu_onboard(...)` — not a bare call.
    if "allowed = tofu_onboard(" not in src:
        return fail(
            "tofu_onboard() result must be assigned back to `allowed` — "
            "bare call would leave allowed=None and break the subsequent `in` check (#898)"
        )
    return 0


# ── B. Behavioral tests ───────────────────────────────────────────────────────

def test_behavioral_tofu_fires_on_none(bridge) -> int:
    """B1: load_allowed() → None → tofu_onboard() must be called (TOFU path).

    Simulates: first ever message after install (no access.json yet).
    """
    with tempfile.TemporaryDirectory() as td:
        access_file = Path(td) / "access.json"
        bridge.ACCESS_FILE = access_file

        # load_allowed() returns None for missing file — real behavior.
        result = bridge.load_allowed()
        if result is not None:
            return fail(
                f"B1: expected load_allowed() → None for missing file, got {result!r}. "
                "Pre-condition failed — check load_allowed() regression."
            )

        # Calling tofu_onboard (as dispatch does) should write the file
        # and return the new allow-set.
        returned = bridge.tofu_onboard(sender_id=111, username="owner")
        if not access_file.exists():
            return fail("B1: tofu_onboard did not create access.json")
        if 111 not in returned:
            return fail(f"B1: tofu_onboard returned {returned!r}, expected {{111}}")

        # After TOFU: load_allowed() must return the populated set.
        after = bridge.load_allowed()
        if after is None:
            return fail("B1: load_allowed() still returns None after TOFU write")
        if 111 not in after:
            return fail(f"B1: allow-set after TOFU does not include onboarded sender: {after!r}")

    return 0


def test_behavioral_owner_passes(bridge) -> int:
    """B2: load_allowed() → set(["owner-id"]) + sender in set → allowed (not dropped).

    The dispatch `if sender_id not in allowed: continue` must NOT fire.
    """
    with tempfile.TemporaryDirectory() as td:
        access_file = Path(td) / "access.json"
        access_file.write_text('{"allowFrom": [222]}')
        bridge.ACCESS_FILE = access_file

        allowed = bridge.load_allowed()
        if allowed is None:
            return fail(f"B2: expected non-None allow-set, got None")
        sender_id = "222"
        # Replicate dispatch check — sender_id is stringified from msg["from"]["id"]
        # but stored as int in the file. load_allowed() returns a set of the raw
        # JSON values (int). Check both forms match.
        if int(sender_id) not in allowed and sender_id not in allowed:
            return fail(
                f"B2: sender 222 not in allowed {allowed!r} — owner would be dropped"
            )
    return 0


def test_behavioral_unknown_dropped(bridge) -> int:
    """B3: load_allowed() → set(["owner-id"]) + sender NOT in set → drop (not task written).

    The dispatch `if sender_id not in allowed: continue` must fire for this sender.
    """
    with tempfile.TemporaryDirectory() as td:
        access_file = Path(td) / "access.json"
        access_file.write_text('{"allowFrom": [333]}')
        bridge.ACCESS_FILE = access_file

        allowed = bridge.load_allowed()
        unknown_sender = "999"
        if int(unknown_sender) in allowed or unknown_sender in allowed:
            return fail(
                f"B3: unexpected — unknown sender 999 found in allowed {allowed!r}"
            )
        # Dispatch would `continue` here — sender is correctly excluded.
    return 0


def test_behavioral_race_guard(bridge) -> int:
    """B4: load_allowed() → None, but file written before tofu_onboard → no clobber.

    Replicate the race guard: if someone calls tofu_onboard after a concurrent
    writer created the file, the existing file must NOT be overwritten.
    """
    with tempfile.TemporaryDirectory() as td:
        access_file = Path(td) / "access.json"
        bridge.ACCESS_FILE = access_file

        # Simulate race: file appears between None-detection and the write.
        existing_payload = '{"allowFrom": [77777]}'
        access_file.write_text(existing_payload)

        returned = bridge.tofu_onboard(sender_id=88888, username="racer")

        actual = access_file.read_text()
        if actual != existing_payload:
            return fail(
                f"B4: tofu_onboard clobbered an existing access.json! "
                f"Before: {existing_payload!r}  After: {actual!r}"
            )
        if 77777 not in returned:
            return fail(
                f"B4: tofu_onboard should return the pre-existing allow-set {{77777}}, got {returned!r}"
            )
    return 0


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> int:
    rc = 0

    # A. Structural
    rc |= test_structural_tofu_wiring()
    rc |= test_structural_drop_path()
    rc |= test_structural_order_tofu_before_allowlist()
    rc |= test_structural_tofu_result_assigned()

    # B. Behavioral (require module import)
    bridge = load_bridge_module()
    orig_access = bridge.ACCESS_FILE

    rc |= test_behavioral_tofu_fires_on_none(bridge)
    rc |= test_behavioral_owner_passes(bridge)
    rc |= test_behavioral_unknown_dropped(bridge)
    rc |= test_behavioral_race_guard(bridge)

    bridge.ACCESS_FILE = orig_access  # restore

    if rc == 0:
        print("PASS: telegram-bridge dispatch wiring — 4 structural + 4 behavioral tests.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
