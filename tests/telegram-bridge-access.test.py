#!/usr/bin/env python3
"""
Tests for load_allowed() tri-state and tofu_onboard() in telegram-bridge.py.

Issue #811: PR #808 introduced a load-bearing distinction in load_allowed():
  - None  → access.json doesn't exist → TOFU-eligible
  - set() → file malformed (fail-closed) OR explicitly empty allowFrom → never TOFU
  - set([ids]) → normal allow check

No unit test locked this in — a future refactor could silently collapse
None → set() and regress the bridge to the silent-drop bug without anyone
noticing. This test locks in the tri-state and covers the full TOFU flow.

Acceptance criteria (from #811):
  (a) Missing access.json → TOFU
  (b) Malformed JSON → drop, no TOFU
  (c) Empty allowFrom: [] → drop, no TOFU (admin lockdown)
  (d) Pre-existing populated allowFrom → standard allow check
  (e) Race-safety: file appears between None-detection and write → no clobber
  (f) Tri-state pin: None when missing, set() when malformed, set(["x"]) when valid

Run: python3 tests/telegram-bridge-access.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Stub external dependencies BEFORE exec'ing the bridge source.
# ---------------------------------------------------------------------------

# Stub task_priority (imports from src/)
_tp = types.ModuleType("task_priority")
_tp.default_priority_for_source = lambda source: "normal"
sys.modules["task_priority"] = _tp

# Stub workspace_default
_wd = types.ModuleType("workspace_default")
_wd.resolve_workspace = lambda: REPO
sys.modules["workspace_default"] = _wd

# Stub vision_push
_vp = types.ModuleType("vision_push")
_vp.push_image = lambda path, source="telegram": False
sys.modules["vision_push"] = _vp

# Stub dotenv
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **kw: None
sys.modules["dotenv"] = _dotenv

# Set a fake token so the bridge doesn't exit(1) at module level
os.environ["TELEGRAM_BOT_TOKEN"] = "test-stub-token"


def _load_bridge():
    """Exec telegram-bridge.py without running main()."""
    src = (REPO / "src" / "telegram-bridge.py").read_text()
    spec = importlib.util.spec_from_loader("telegram_bridge", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(REPO / "src" / "telegram-bridge.py")
    exec(src, mod.__dict__)
    return mod


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run():
    bridge = _load_bridge()
    fails: list[str] = []
    passed = 0

    # ── (f) Tri-state pin: load_allowed returns correct type ──────────────

    with tempfile.TemporaryDirectory() as td:
        fake_access = Path(td) / "access.json"
        orig = bridge.ACCESS_FILE
        try:
            bridge.ACCESS_FILE = fake_access

            # File missing → None
            result = bridge.load_allowed()
            if result is not None:
                fails.append("(f) missing file: expected None, got {!r}".format(result))
            else:
                passed += 1

            # Malformed JSON → set()
            fake_access.write_text("NOT JSON {{{")
            result = bridge.load_allowed()
            if result is not None and not isinstance(result, set):
                fails.append("(f) malformed: expected set(), got {!r}".format(result))
            elif result is None:
                fails.append("(f) malformed: expected set() (fail-closed), got None")
            elif len(result) != 0:
                fails.append("(f) malformed: expected empty set(), got {!r}".format(result))
            else:
                passed += 1

            # Valid JSON, populated allowFrom → set(["x"])
            fake_access.write_text(json.dumps({"allowFrom": ["alice", "bob"]}))
            result = bridge.load_allowed()
            if result != {"alice", "bob"}:
                fails.append("(f) valid: expected {'alice','bob'}, got {!r}".format(result))
            else:
                passed += 1

            # Valid JSON, no allowFrom key → set()
            fake_access.write_text(json.dumps({"otherKey": 123}))
            result = bridge.load_allowed()
            if result is None:
                fails.append("(f) missing key: expected set(), got None")
            elif len(result) != 0:
                fails.append("(f) missing key: expected empty set(), got {!r}".format(result))
            else:
                passed += 1
        finally:
            bridge.ACCESS_FILE = orig

    # ── (c) Empty allowFrom: [] → drop, no TOFU ──────────────────────────

    with tempfile.TemporaryDirectory() as td:
        fake_access = Path(td) / "access.json"
        orig = bridge.ACCESS_FILE
        try:
            bridge.ACCESS_FILE = fake_access
            fake_access.write_text(json.dumps({"allowFrom": []}))

            result = bridge.load_allowed()
            if result is None:
                fails.append("(c) empty allowFrom: expected set(), got None")
            elif len(result) != 0:
                fails.append("(c) empty allowFrom: expected empty set(), got {!r}".format(result))
            else:
                passed += 1
        finally:
            bridge.ACCESS_FILE = orig

    # ── (a) Missing access.json → TOFU auto-onboard ──────────────────────

    with tempfile.TemporaryDirectory() as td:
        fake_access = Path(td) / "access.json"
        orig = bridge.ACCESS_FILE
        try:
            bridge.ACCESS_FILE = fake_access
            # File must NOT exist for TOFU
            assert not fake_access.exists(), "setup error: file should not exist"

            allowed = bridge.tofu_onboard("12345", "testuser")

            # Should return {"12345"}
            if allowed != {"12345"}:
                fails.append("(a) TOFU: expected {'12345'}, got {!r}".format(allowed))
            else:
                passed += 1

            # File should now exist with correct content
            data = json.loads(fake_access.read_text())
            if data.get("allowFrom") != ["12345"]:
                fails.append("(a) TOFU: file allowFrom wrong: {!r}".format(data.get("allowFrom")))
            else:
                passed += 1

            if data.get("tofuOwner") != "12345":
                fails.append("(a) TOFU: tofuOwner wrong: {!r}".format(data.get("tofuOwner")))
            else:
                passed += 1

            # File permissions should be 0o600
            mode = fake_access.stat().st_mode & 0o777
            if mode != 0o600:
                fails.append("(a) TOFU: expected 0o600, got {:o}".format(mode))
            else:
                passed += 1
        finally:
            bridge.ACCESS_FILE = orig

    # ── (b) Malformed JSON → drop, no TOFU ───────────────────────────────

    with tempfile.TemporaryDirectory() as td:
        fake_access = Path(td) / "access.json"
        orig = bridge.ACCESS_FILE
        try:
            bridge.ACCESS_FILE = fake_access
            fake_access.write_text("BROKEN{{{")

            result = bridge.load_allowed()
            # Should return empty set (fail-closed), not None
            if result is None:
                fails.append("(b) malformed: expected set(), got None (would trigger TOFU)")
            elif len(result) != 0:
                fails.append("(b) malformed: expected empty set(), got {!r}".format(result))
            else:
                passed += 1

            # File should be unchanged (still broken)
            content = fake_access.read_text()
            if content != "BROKEN{{{":
                fails.append("(b) malformed: file was modified unexpectedly")
            else:
                passed += 1
        finally:
            bridge.ACCESS_FILE = orig

    # ── (d) Pre-existing populated allowFrom → standard allow check ───────

    with tempfile.TemporaryDirectory() as td:
        fake_access = Path(td) / "access.json"
        orig = bridge.ACCESS_FILE
        try:
            bridge.ACCESS_FILE = fake_access
            fake_access.write_text(json.dumps({"allowFrom": ["111", "222"]}))

            result = bridge.load_allowed()
            if result != {"111", "222"}:
                fails.append("(d) populated: expected {'111','222'}, got {!r}".format(result))
            else:
                passed += 1

            # In-list check
            if "111" not in result:
                fails.append("(d) '111' should be allowed")
            else:
                passed += 1

            # Out-of-list check
            if "999" in result:
                fails.append("(d) '999' should NOT be allowed")
            else:
                passed += 1
        finally:
            bridge.ACCESS_FILE = orig

    # ── (e) Race-safety: file appears between None-detection and write ────

    with tempfile.TemporaryDirectory() as td:
        fake_access = Path(td) / "access.json"
        orig = bridge.ACCESS_FILE
        try:
            bridge.ACCESS_FILE = fake_access
            # File starts absent (TOFU eligible)
            assert not fake_access.exists()

            # Simulate a race: write a file BEFORE tofu_onboard runs
            # The existing file has a different owner
            fake_access.write_text(json.dumps({"allowFrom": ["existing_owner"], "tofuOwner": "existing_owner"}))

            # tofu_onboard should detect the file exists and NOT clobber it
            allowed = bridge.tofu_onboard("new_user", "newuser")

            # File content should be UNCHANGED (existing owner preserved)
            data = json.loads(fake_access.read_text())
            if data.get("tofuOwner") == "new_user":
                fails.append("(e) race: tofu_onboard clobbered existing file!")
            else:
                passed += 1

            if data.get("allowFrom") == ["new_user"]:
                fails.append("(e) race: allowFrom was overwritten!")
            else:
                passed += 1

            # Should return existing owner's set (or empty if load_allowed returns empty)
            if allowed is not None and "new_user" in allowed:
                fails.append("(e) race: new_user should not be in allowed set")
            else:
                passed += 1
        finally:
            bridge.ACCESS_FILE = orig

    # ── Summary ───────────────────────────────────────────────────────────

    print()
    if fails:
        print("━━━ telegram-bridge access tests: {} FAILED ━━━".format(len(fails)))
        for f in fails:
            print("  ✗ {}".format(f))
        print("\n  {}/{} passed".format(passed, passed + len(fails)))
        return 1
    else:
        print("━━━ telegram-bridge access tests: {} passed / 0 failed ━━━".format(passed))
        return 0


if __name__ == "__main__":
    sys.exit(run())
