#!/usr/bin/env python3
"""Functional tests for Slack's first-owner enrollment gate.

Enrollment requires the entire direct-message text to match the current
single-use code. Missing challenges are closed, and prior access is restored
before any new enrollment attempt.

Run: python3 tests/slack-bridge-tofu-enroll.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "_helpers"))
import bridge_paths  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


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
        sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
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


def _make_dm_event(user_id: str, text: str, channel: str = "D_TEST") -> dict:
    return {
        "user": user_id,
        "channel": channel,
        "channel_type": "im",
        "text": text,
        "ts": "1700000000.000001",
    }


class _TempBridgeState:
    def __enter__(self):
        self._td = tempfile.mkdtemp(prefix="sutando-bridge-state-")
        self._orig_access = BRIDGE.ACCESS_FILE
        self._orig_tasks = BRIDGE.TASKS_DIR
        self._orig_post = BRIDGE.app.client.chat_postMessage

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
        assert BRIDGE.ACCESS_BACKUP_FILE.is_relative_to(Path(self._td))
        with BRIDGE._access_cache_lock:
            BRIDGE._access_cache = None
            BRIDGE._access_cache_mtime = 0.0
        return self

    def __exit__(self, *_):
        bridge_paths.restore(BRIDGE, self._orig_paths)
        BRIDGE.ACCESS_FILE = self._orig_access
        BRIDGE.TASKS_DIR = self._orig_tasks
        BRIDGE.app.client.chat_postMessage = self._orig_post
        with BRIDGE._access_cache_lock:
            BRIDGE._access_cache = None
            BRIDGE._access_cache_mtime = 0.0
        BRIDGE._TOFU_ENROLLMENT = None


def _set_challenge(code: str = "a1b2c3d4e5f60718293a4b5c6d7e8f90"):
    BRIDGE._TOFU_ENROLLMENT = BRIDGE.tofu_enrollment.EnrollmentChallenge(code)


def _mock_onboard(onboard_calls: list[tuple]):
    def onboard(uid, uname):
        onboard_calls.append((uid, uname))
        BRIDGE.ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        BRIDGE.ACCESS_FILE.write_text(json.dumps({"allowFrom": [uid]}))
        return {uid}

    BRIDGE.tofu_onboard = onboard


def test_wrong_text_is_rejected():
    with _TempBridgeState():
        _set_challenge()
        replies: list[dict] = []
        BRIDGE.app.client.chat_postMessage = lambda **kw: replies.append(kw)
        result = BRIDGE._write_task(
            _make_dm_event("U_ATTACKER", "hello"),
            "Slack DM",
            "hello",
            "attacker",
        )
        assert result is None
        assert len(replies) == 1
        assert "Enrollment code required" in replies[0]["text"]


def test_exact_code_enrolls_owner():
    with _TempBridgeState():
        _set_challenge()
        onboard_calls: list[tuple] = []
        original = BRIDGE.tofu_onboard
        _mock_onboard(onboard_calls)
        try:
            code = BRIDGE._TOFU_ENROLLMENT.code
            BRIDGE._write_task(
                _make_dm_event("U_OWNER", code),
                "Slack DM",
                code,
                "realowner",
            )
        finally:
            BRIDGE.tofu_onboard = original
        assert onboard_calls == [("U_OWNER", "realowner")]


def test_substring_is_rejected():
    with _TempBridgeState():
        _set_challenge()
        onboard_calls: list[tuple] = []
        original = BRIDGE.tofu_onboard
        _mock_onboard(onboard_calls)
        replies: list[dict] = []
        BRIDGE.app.client.chat_postMessage = lambda **kw: replies.append(kw)
        code = BRIDGE._TOFU_ENROLLMENT.code
        text = "my code is " + code
        try:
            result = BRIDGE._write_task(
                _make_dm_event("U_ATTACKER", text), "Slack DM", text, "attacker"
            )
        finally:
            BRIDGE.tofu_onboard = original
        assert result is None
        assert onboard_calls == []
        assert "Enrollment code required" in replies[0]["text"]


def test_five_wrong_attempts_close_enrollment():
    with _TempBridgeState():
        _set_challenge()
        replies: list[dict] = []
        onboard_calls: list[tuple] = []
        original = BRIDGE.tofu_onboard
        _mock_onboard(onboard_calls)
        BRIDGE.app.client.chat_postMessage = lambda **kw: replies.append(kw)
        try:
            for _ in range(5):
                BRIDGE._write_task(
                    _make_dm_event("U_ATTACKER", "wrong"), "Slack DM", "wrong", "attacker"
                )
            code = BRIDGE._TOFU_ENROLLMENT.code
            result = BRIDGE._write_task(
                _make_dm_event("U_ATTACKER", code), "Slack DM", code, "attacker"
            )
        finally:
            BRIDGE.tofu_onboard = original
        assert result is None
        assert onboard_calls == []
        assert replies[-1]["text"] == BRIDGE.tofu_enrollment.reply_for(BRIDGE.tofu_enrollment.CLOSED)


def test_missing_challenge_is_closed():
    with _TempBridgeState():
        BRIDGE._TOFU_ENROLLMENT = None
        replies: list[dict] = []
        onboard_calls: list[tuple] = []
        original = BRIDGE.tofu_onboard
        _mock_onboard(onboard_calls)
        BRIDGE.app.client.chat_postMessage = lambda **kw: replies.append(kw)
        try:
            result = BRIDGE._write_task(_make_dm_event("U_FIRST", "hey"), "Slack DM", "hey", "firstuser")
        finally:
            BRIDGE.tofu_onboard = original
        assert result is None
        assert onboard_calls == []
        assert replies[-1]["text"] == BRIDGE.tofu_enrollment.reply_for(BRIDGE.tofu_enrollment.CLOSED)


def test_channel_mention_does_not_spend_challenge():
    with _TempBridgeState():
        _set_challenge()
        onboard_calls: list[tuple] = []
        original = BRIDGE.tofu_onboard
        _mock_onboard(onboard_calls)
        replies: list[dict] = []
        BRIDGE.app.client.chat_postMessage = lambda **kw: replies.append(kw)
        code = BRIDGE._TOFU_ENROLLMENT.code
        mention = {
            "user": "U_ATTACKER",
            "channel": "C_PUBLIC",
            "text": code,
            "ts": "1700000000.000009",
        }
        try:
            result = BRIDGE._write_task(mention, "Slack mention", code, "attacker")
            assert result is None
            assert replies == []
            result = BRIDGE._write_task(
                _make_dm_event("U_OWNER", code), "Slack DM", code, "owner"
            )
        finally:
            BRIDGE.tofu_onboard = original
        assert onboard_calls == [("U_OWNER", "owner")]


def test_deleted_access_file_is_restored_for_trusted_owner():
    with _TempBridgeState():
        _set_challenge()
        BRIDGE.app.client.chat_postMessage = lambda **kw: None
        code = BRIDGE._TOFU_ENROLLMENT.code
        BRIDGE._write_task(_make_dm_event("U_OWNER", code), "Slack DM", code, "owner")
        assert BRIDGE.ACCESS_FILE.exists()
        BRIDGE.ACCESS_FILE.unlink()
        result = BRIDGE._write_task(
            _make_dm_event("U_ATTACKER", "hello"), "Slack DM", "hello", "attacker"
        )
        assert result is None
        restored = json.loads(BRIDGE.ACCESS_FILE.read_text())
        assert "U_OWNER" in restored["allowFrom"]
        assert "U_ATTACKER" not in restored["allowFrom"]


def test_enrollment_check_delegates_to_policy():
    with _TempBridgeState():
        _set_challenge()
        calls: list[tuple] = []
        original_check = BRIDGE.tofu_enrollment.check_enrollment
        BRIDGE.tofu_enrollment.check_enrollment = (
            lambda challenge, text: calls.append((challenge, text))
            or BRIDGE.tofu_enrollment.REJECTED
        )
        BRIDGE.app.client.chat_postMessage = lambda **kw: None
        try:
            result = BRIDGE._write_task(
                _make_dm_event("U_ATTACKER", "delegated"),
                "Slack DM",
                "delegated",
                "attacker",
            )
        finally:
            BRIDGE.tofu_enrollment.check_enrollment = original_check
        assert result is None
        assert calls == [(BRIDGE._TOFU_ENROLLMENT, "delegated")]


def test_rejection_reply_uses_event_channel():
    with _TempBridgeState():
        _set_challenge()
        captured: list[dict] = []
        BRIDGE.app.client.chat_postMessage = lambda **kw: captured.append(kw)
        event = _make_dm_event("U_ATTACKER", "wrong", channel="D_SPECIFIC_CHAN")
        BRIDGE._write_task(event, "Slack DM", "wrong", "attacker")
        assert captured
        assert captured[0]["channel"] == "D_SPECIFIC_CHAN"


def test_rejection_api_failure_is_swallowed():
    with _TempBridgeState():
        _set_challenge()

        def raise_error(**kw):
            raise RuntimeError("Slack API down")

        BRIDGE.app.client.chat_postMessage = raise_error
        result = BRIDGE._write_task(
            _make_dm_event("U_ATTACKER", "wrong"), "Slack DM", "wrong", "attacker"
        )
        assert result is None


def main() -> int:
    tests = [
        ("rejects wrong text", test_wrong_text_is_rejected),
        ("accepts the exact code", test_exact_code_enrolls_owner),
        ("rejects a code embedded in a longer message", test_substring_is_rejected),
        ("closes enrollment after five wrong attempts", test_five_wrong_attempts_close_enrollment),
        ("closes enrollment when no challenge exists", test_missing_challenge_is_closed),
        ("ignores channel mentions without spending the challenge", test_channel_mention_does_not_spend_challenge),
        ("restores deleted access for the trusted owner", test_deleted_access_file_is_restored_for_trusted_owner),
        ("delegates enrollment checks to shared policy", test_enrollment_check_delegates_to_policy),
        ("replies in the event channel", test_rejection_reply_uses_event_channel),
        ("swallows reply API failures", test_rejection_api_failure_is_swallowed),
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
    print(f"\nAll {len(tests)} Slack enrollment tests passed.")
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
