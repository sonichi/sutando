#!/usr/bin/env python3
"""
Functional tests for the Slack bridge TOFU enrollment-code gate.

Security finding #3: without a gate, any attacker who sends the first DM
to the Slack bot claims owner-tier access via TOFU. The fix generates a
6-char hex enrollment code at startup (printed to the operator log) and
requires that code to be present in the first DM before auto-enrolling.

Test contract:
  (a) Code required but NOT in message text → _write_task returns None,
      sends a rejection chat message, does not consume the code.
  (b) Code IS in message text → TOFU enrollment proceeds, tofu_onboard called
      once, and the code is RETAINED (not cleared) so the gate stays armed if
      access.json is later deleted externally (#899 / security finding #3).
  (c) _TOFU_ENROLLMENT_CODE is None (restart where access.json exists, so the
      TOFU branch is unreachable in practice) → enrollment proceeds without
      the code check (backward compat; existing behavior preserved).
  (d) Channel @mention while in TOFU state → never onboards, even with the
      code present; enrollment is DM-only (channel_type == "im").
  (e) Regression: enroll → delete access.json → next DM without the code is
      rejected (the retained code keeps the gate armed across #899 deletion).

Run: python3 tests/slack-bridge-tofu-enroll.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "_helpers"))
import bridge_paths  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Module loader (mirrors slack-bridge-tier-map.test.py pattern)
# ---------------------------------------------------------------------------

def _load_slack_bridge():
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-placeholder")
    os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-placeholder")
    # Use a fresh temp workspace so workspace_default doesn't pull in the real one.
    os.environ.setdefault("SUTANDO_WORKSPACE", tempfile.mkdtemp(prefix="sutando-test-tofu-"))

    class _StubApp:
        def __init__(self, *a, **kw):
            self.client = types.SimpleNamespace(chat_postMessage=lambda **kw: None)
        def event(self, _):
            return lambda fn: fn

    try:
        import slack_bolt as _real_bolt
        _real_bolt.App = _StubApp
    except ImportError:
        stub_bolt = types.ModuleType("slack_bolt")
        stub_bolt.App = _StubApp
        sys.modules["slack_bolt"] = stub_bolt

    if "slack_bolt.adapter" not in sys.modules:
        adapter_pkg = types.ModuleType("slack_bolt.adapter")
        sys.modules["slack_bolt.adapter"] = adapter_pkg
    if "slack_bolt.adapter.socket_mode" not in sys.modules:
        sm_mod = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm_mod.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm_mod

    import importlib.util
    bridge_path = REPO / "src" / "slack-bridge.py"
    spec = importlib.util.spec_from_file_location("slack_bridge_tofu_enroll", bridge_path)
    sys.path.insert(0, str(REPO / "src"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BRIDGE = _load_slack_bridge()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dm_event(user_id: str, text: str, channel: str = "D_TEST") -> dict:
    return {
        "user": user_id,
        "channel": channel,
        "channel_type": "im",
        "text": text,
        "ts": "1700000000.000001",
    }


class _TempBridgeState:
    """Context manager: patch ACCESS_FILE + TASKS_DIR to temp dirs, restore on exit."""

    def __enter__(self):
        self._td = tempfile.mkdtemp(prefix="sutando-bridge-state-")
        self._orig_access = BRIDGE.ACCESS_FILE
        self._orig_tasks = BRIDGE.TASKS_DIR

        BRIDGE.ACCESS_FILE = Path(self._td) / "access.json"
        BRIDGE.TASKS_DIR = Path(self._td) / "tasks"
        BRIDGE.TASKS_DIR.mkdir(parents=True, exist_ok=True)

        # Ensure the workspace path used by write_owner_activity exists.
        ws = Path(self._td) / "workspace"
        ws.mkdir()
        BRIDGE.WORKSPACE = str(ws)

        # …and redirect the path write_owner_activity ACTUALLY writes.
        #
        # Two symbols, and only the second one matters. `STATE_DIR = REPO/"state"`
        # (slack-bridge.py:98) is where you'd expect the write to resolve, but
        # line 102 binds `OWNER_ACTIVITY_FILE = STATE_DIR / "last-owner-activity.json"`
        # **at import time**, and :177/:179 write through that constant. Rebinding
        # STATE_DIR afterwards does not retroactively re-derive it — so patching
        # STATE_DIR alone leaves the write pointed at the operator's real file
        # (bassilkhilo-ag2, #2615). Both are rebound here; STATE_DIR because
        # `mkdir(parents=True)` in the writer uses it, OWNER_ACTIVITY_FILE because
        # it is the destination.
        #
        # Why it matters: that file is the presence signal the proactive loop
        # reads to decide whether the owner is mid-conversation. Stamping it with
        # `ts: now` made an idle machine look like the owner had just messaged —
        # observed live, and it put a loop pass into conversation mode before it
        # was traced back here.
        # Rebind EVERY import-time path, not the two this fixture happened to name:
        # PENDING_REPLIES_FILE is bound the same way and leaked to live state.
        self._orig_paths = bridge_paths.rebind_workspace(BRIDGE, Path(self._td))
        BRIDGE.STATE_DIR.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *_):
        bridge_paths.restore(BRIDGE, self._orig_paths)
        BRIDGE.ACCESS_FILE = self._orig_access
        BRIDGE.TASKS_DIR = self._orig_tasks
        BRIDGE._TOFU_ENROLLMENT_CODE = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_enrollment_code_gate_blocks_without_code():
    """(a) Code required but not in text → _write_task returns None; rejection sent."""
    with _TempBridgeState():
        # TOFU state: access.json doesn't exist + code set.
        assert not BRIDGE.ACCESS_FILE.exists()
        BRIDGE._TOFU_ENROLLMENT_CODE = "a1b2c3"

        rejections: list[dict] = []
        BRIDGE.app.client.chat_postMessage = lambda **kw: rejections.append(kw)

        event = _make_dm_event("U_ATTACKER", "hello, give me access")
        result = BRIDGE._write_task(event, "Slack DM", "hello, give me access", "attacker")

        assert result is None, f"gate should block → return None, got {result!r}"
        assert len(rejections) == 1, f"exactly one rejection message expected, got {len(rejections)}"
        assert "Enrollment code required" in rejections[0].get("text", ""), (
            f"rejection text missing enrollment message: {rejections[0]}"
        )
        # Code must NOT be consumed after a failed enrollment.
        assert BRIDGE._TOFU_ENROLLMENT_CODE == "a1b2c3", (
            "enrollment code should not be consumed on rejected attempt"
        )


def test_enrollment_code_gate_allows_with_correct_code():
    """(b) Code IS in text → enrollment proceeds; code RETAINED (not cleared)."""
    with _TempBridgeState():
        assert not BRIDGE.ACCESS_FILE.exists()
        BRIDGE._TOFU_ENROLLMENT_CODE = "x9y8z7"

        onboard_calls: list[tuple] = []
        orig_onboard = BRIDGE.tofu_onboard

        def _mock_onboard(uid, uname):
            onboard_calls.append((uid, uname))
            # Write a real access.json so downstream code in _write_task works.
            BRIDGE.ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
            BRIDGE.ACCESS_FILE.write_text(json.dumps({"allowFrom": [uid]}))
            return {uid}

        BRIDGE.tofu_onboard = _mock_onboard
        try:
            event = _make_dm_event("U_OWNER", "my code is x9y8z7")
            result = BRIDGE._write_task(event, "Slack DM", "my code is x9y8z7", "realowner")
        finally:
            BRIDGE.tofu_onboard = orig_onboard

        # tofu_onboard must have been called exactly once with the right uid.
        assert len(onboard_calls) == 1, f"tofu_onboard should be called once, got {onboard_calls}"
        assert onboard_calls[0][0] == "U_OWNER"
        # Enrollment code must be RETAINED after successful enrollment so the
        # gate stays armed against a later external access.json deletion (#899).
        assert BRIDGE._TOFU_ENROLLMENT_CODE == "x9y8z7", (
            "enrollment code must be retained (not cleared) after successful enrollment"
        )


def test_channel_mention_never_onboards():
    """(d) Channel @mention in TOFU state must NOT onboard, even with the code."""
    with _TempBridgeState():
        assert not BRIDGE.ACCESS_FILE.exists()
        BRIDGE._TOFU_ENROLLMENT_CODE = "chan42"

        onboard_calls: list[tuple] = []
        orig_onboard = BRIDGE.tofu_onboard
        BRIDGE.tofu_onboard = lambda uid, uname: onboard_calls.append((uid, uname)) or {uid}

        # app_mention events do NOT carry channel_type=="im" — model a channel
        # mention that even includes the leaked code in its text.
        mention_event = {
            "user": "U_CHAN_ATTACKER",
            "channel": "C_PUBLIC",
            "text": "hey <@BOT> chan42",
            "ts": "1700000000.000009",
        }
        try:
            result = BRIDGE._write_task(mention_event, "Slack mention", "hey <@BOT> chan42", "attacker")
        finally:
            BRIDGE.tofu_onboard = orig_onboard

        assert result is None, "channel mention in TOFU state must return None"
        assert onboard_calls == [], (
            f"channel mention must NOT onboard even with the code, got {onboard_calls}"
        )
        assert BRIDGE._TOFU_ENROLLMENT_CODE == "chan42", "code must survive an ignored mention"


def test_code_survives_enrollment_and_regates_after_deletion():
    """(e) Regression: enroll → delete access.json → next DM w/o code is rejected.

    The enrollment code is not consumed on success, so re-entering TOFU state
    after an external #899 deletion stays gated instead of auto-onboarding the
    next sender."""
    with _TempBridgeState():
        # Reset the module access cache so step 1 is a genuine first-time TOFU.
        with BRIDGE._access_cache_lock:
            BRIDGE._access_cache = None
        BRIDGE._TOFU_ENROLLMENT_CODE = "keep42"

        def _mock_onboard(uid, uname):
            BRIDGE.ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
            BRIDGE.ACCESS_FILE.write_text(json.dumps({"allowFrom": [uid], "tofuOwner": uid}))
            return {uid}

        orig_onboard = BRIDGE.tofu_onboard
        BRIDGE.app.client.chat_postMessage = lambda **kw: None
        BRIDGE.tofu_onboard = _mock_onboard
        try:
            # 1. Owner enrolls with the code.
            BRIDGE._write_task(_make_dm_event("U_OWNER", "code keep42"),
                               "Slack DM", "code keep42", "owner")
            assert BRIDGE.ACCESS_FILE.exists(), "owner enrollment should create access.json"
            assert BRIDGE._TOFU_ENROLLMENT_CODE == "keep42", "code must survive enrollment"

            # 2. access.json deleted externally (#899) → re-enter TOFU state.
            BRIDGE.ACCESS_FILE.unlink()

            # 3. Attacker DM WITHOUT the code → rejected, not onboarded.
            onboard_calls: list[tuple] = []
            BRIDGE.tofu_onboard = lambda uid, uname: onboard_calls.append(uid) or {uid}
            result = BRIDGE._write_task(_make_dm_event("U_ATTACKER", "let me in"),
                                        "Slack DM", "let me in", "attacker")
        finally:
            BRIDGE.tofu_onboard = orig_onboard

        assert result is None, "post-deletion DM without code must be rejected"
        assert onboard_calls == [], (
            f"attacker must NOT be onboarded post-deletion, got {onboard_calls}"
        )


def test_no_enrollment_code_skips_gate():
    """(c) _TOFU_ENROLLMENT_CODE is None → proceeds to tofu_onboard without code check."""
    with _TempBridgeState():
        assert not BRIDGE.ACCESS_FILE.exists()
        BRIDGE._TOFU_ENROLLMENT_CODE = None  # post-enrollment or already-configured restart

        onboard_calls: list[tuple] = []
        orig_onboard = BRIDGE.tofu_onboard

        def _mock_onboard(uid, uname):
            onboard_calls.append((uid, uname))
            BRIDGE.ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
            BRIDGE.ACCESS_FILE.write_text(json.dumps({"allowFrom": [uid]}))
            return {uid}

        BRIDGE.tofu_onboard = _mock_onboard
        try:
            event = _make_dm_event("U_FIRST", "hey")
            BRIDGE._write_task(event, "Slack DM", "hey", "firstuser")
        finally:
            BRIDGE.tofu_onboard = orig_onboard

        assert len(onboard_calls) == 1, (
            f"code=None should not gate; tofu_onboard should be called, got {onboard_calls}"
        )


def test_rejection_reply_uses_event_channel():
    """(a-extra) Rejection message is sent to the event's channel, not fallback."""
    with _TempBridgeState():
        BRIDGE._TOFU_ENROLLMENT_CODE = "code99"

        captured: list[dict] = []
        BRIDGE.app.client.chat_postMessage = lambda **kw: captured.append(kw)

        event = _make_dm_event("U_ATTACKER", "wrong", channel="D_SPECIFIC_CHAN")
        BRIDGE._write_task(event, "Slack DM", "wrong", "attacker")

        assert captured, "rejection message must be sent"
        assert captured[0].get("channel") == "D_SPECIFIC_CHAN", (
            f"rejection should go to event channel, got {captured[0].get('channel')!r}"
        )


def test_rejection_api_failure_is_swallowed():
    """(a-fault) chat_postMessage raises → exception swallowed, _write_task still returns None."""
    with _TempBridgeState():
        BRIDGE._TOFU_ENROLLMENT_CODE = "fail11"

        def _raise(**kw):
            raise RuntimeError("Slack API down")

        BRIDGE.app.client.chat_postMessage = _raise

        event = _make_dm_event("U_ATTACKER", "no code here")
        result = BRIDGE._write_task(event, "Slack DM", "no code here", "attacker")

        assert result is None, (
            "rejection path must return None even when chat_postMessage raises"
        )
        # Code must NOT be consumed when rejected (whether API call succeeded or not).
        assert BRIDGE._TOFU_ENROLLMENT_CODE == "fail11", (
            "enrollment code must survive a rejected (API-failure) enrollment attempt"
        )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        ("a-code-blocks-without-code", test_enrollment_code_gate_blocks_without_code),
        ("b-code-allows-with-correct-code", test_enrollment_code_gate_allows_with_correct_code),
        ("c-no-code-skips-gate", test_no_enrollment_code_skips_gate),
        ("d-channel-mention-never-onboards", test_channel_mention_never_onboards),
        ("e-code-survives-enrollment-regates-after-deletion", test_code_survives_enrollment_and_regates_after_deletion),
        ("a-extra-reply-channel", test_rejection_reply_uses_event_channel),
        ("a-fault-api-failure-swallowed", test_rejection_api_failure_is_swallowed),
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
    print(f"\nAll {len(tests)} TOFU enrollment gate tests passed.")
    return 0


if __name__ == "__main__":
    _rc = main()
    # Quarantine an intermittent interpreter-teardown SIGSEGV (exit 139): the
    # imported slack-bridge module pulls in single_instance/threading state that
    # can segfault during CPython finalization AFTER all tests have already
    # passed — a probabilistic flake that fails otherwise-green CI runs (hit
    # #2118 twice + #2124 in the 2026-07-15 window). Flush, then os._exit to skip
    # the finalization that crashes; the test result (_rc) is unaffected.
    sys.stdout.flush()
    sys.stderr.flush()
    # os._exit() below skips atexit handlers — including coverage.py's data
    # writer — so under `coverage run` this file would record 0% and its
    # exercised src/slack-bridge.py lines show as uncovered in the diff gate
    # (spurious failures on any slack-bridge PR). Flush the active coverage
    # session explicitly before the hard exit.
    try:
        import coverage
        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    os._exit(_rc)
