#!/usr/bin/env python3
"""A rejected Slack token exits EXIT_AUTH_REJECTED (78), so the wrapper holds instead of looping.

Covers both rejection points: Bolt's eager auth.test (bot token) and Socket
Mode's apps.connections.open (app token). slack_bolt is stubbed so the module
loads without the SDK or a network.
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class _FakeApp:
    def __init__(self, token=None):
        self.client = None

    def _decorator(self, *a, **k):
        return lambda fn: fn

    event = message = command = action = shortcut = view = _decorator


_bolt = types.ModuleType("slack_bolt")
_bolt.App = _FakeApp
_adapter = types.ModuleType("slack_bolt.adapter")
_socket = types.ModuleType("slack_bolt.adapter.socket_mode")
_socket.SocketModeHandler = type("SocketModeHandler", (), {"__init__": lambda self, *a, **k: None})
sys.modules.update({"slack_bolt": _bolt, "slack_bolt.adapter": _adapter,
                    "slack_bolt.adapter.socket_mode": _socket})

os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp()
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-slack-authexit-")
os.environ.pop("CLAUDE_HOME", None)
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}', encoding="utf-8")
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"

spec = importlib.util.spec_from_file_location("slackbridge_authexit", REPO / "src" / "slack-bridge.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS += 1


def outcome_of(fn):
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            fn()
    except SystemExit as e:
        return ("exit", e.code), err.getvalue()
    except BaseException as e:  # noqa: BLE001 — the outcome under test
        return ("raised", type(e).__name__), err.getvalue()
    return ("returned", None), err.getvalue()


class BoltError(Exception):
    pass


def raising_app(exc):
    def _cls(token=None):
        raise exc
    return _cls


# Bolt's own wording for a failed eager auth.test.
bolt_rejected = BoltError("`token` is invalid (auth.test result: {'ok': False, 'error': 'invalid_auth'})")
out, err = outcome_of(lambda: mod._build_app(raising_app(bolt_rejected)))
check(out == ("exit", 78), f"auth.test invalid_auth -> exit 78 (got {out})")
check("vault set SLACK_BOT_TOKEN" in err and "xoxb-test-not-real" not in err,
      "log names SLACK_BOT_TOKEN and the fix, never the value")
out, _ = outcome_of(lambda: mod._build_app(raising_app(OSError("network down"))))
check(out == ("raised", "OSError"), f"a transport fault stays a crash (got {out})")


class SlackApiError(Exception):
    def __init__(self, error):
        super().__init__("Failed")
        self.response = {"ok": False, "error": error}


class Handler:
    def __init__(self, exc):
        self.exc = exc

    def start(self):
        raise self.exc


out, err = outcome_of(lambda: mod._start_socket_mode(Handler(SlackApiError("invalid_auth"))))
check(out == ("exit", 78), f"apps.connections.open invalid_auth -> exit 78 (got {out})")
check("vault set SLACK_APP_TOKEN" in err, "log names SLACK_APP_TOKEN")
out, _ = outcome_of(lambda: mod._start_socket_mode(Handler(SlackApiError("token_revoked"))))
check(out == ("exit", 78), f"token_revoked -> exit 78 (got {out})")
out, _ = outcome_of(lambda: mod._start_socket_mode(Handler(SlackApiError("internal_error"))))
check(out == ("raised", "SlackApiError"), f"a non-auth API error stays a crash (got {out})")


class UnreadableResponse:
    def get(self, key):
        raise RuntimeError("unreadable")


class OpaqueError(Exception):
    response = UnreadableResponse()


check(not mod._slack_auth_error(OpaqueError("boom")),
      "an unreadable response with no error in the message is not an auth answer")
check(mod._slack_auth_error(OpaqueError("{'ok': False, 'error': 'not_authed'}")),
      "an unreadable response falls back to the message's error code")

print("PASS" if FAILS == 0 else f"{FAILS} FAILED")
raise SystemExit(1 if FAILS else 0)
