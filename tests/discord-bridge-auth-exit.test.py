#!/usr/bin/env python3
"""A rejected Discord token exits EXIT_AUTH_REJECTED (78), so the wrapper holds instead of looping.

Pins the shared constant against the wrapper's literal mirror too.
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-authexit-")
os.environ.pop("CLAUDE_HOME", None)
os.environ["SUTANDO_TEST_MODE"] = "1"
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n", encoding="utf-8")
(_cfg / "access.json").write_text('{"allowFrom": []}', encoding="utf-8")

sys.path.insert(0, str(REPO / "src"))

from channel_auth_exit import EXIT_AUTH_REJECTED  # noqa: E402

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS += 1


wrapper = (REPO / "src" / "launchd" / "channel-bridge-wrapper.sh").read_text()
check(EXIT_AUTH_REJECTED == 78, "EXIT_AUTH_REJECTED is 78 (EX_CONFIG)")
check(re.search(rf"^EXIT_AUTH_REJECTED={EXIT_AUTH_REJECTED}$", wrapper, re.M) is not None,
      "wrapper mirrors the same literal")

try:
    import discord
except ImportError:
    print("SKIP: discord.py not installed in this runner")
    raise SystemExit(1 if FAILS else 0)

_spec = importlib.util.spec_from_file_location("dbridge_authexit", REPO / "src" / "discord-bridge.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def run_with(exc: BaseException):
    """Drive _run_gateway with a client whose run() raises exc; returns (outcome, stderr)."""
    def _run(token, log_handler=None):
        raise exc
    _mod.client = SimpleNamespace(run=_run)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            _mod._run_gateway()
    except SystemExit as e:
        return ("exit", e.code), err.getvalue()
    except BaseException as e:  # noqa: BLE001 — the outcome under test
        return ("raised", type(e).__name__), err.getvalue()
    return ("returned", None), err.getvalue()


outcome, err = run_with(discord.LoginFailure("Improper token has been passed."))
check(outcome == ("exit", 78), f"LoginFailure -> exit 78 (got {outcome})")
check("DISCORD_BOT_TOKEN" in err and "vault set DISCORD_BOT_TOKEN" in err,
      "log names the key and the fix")
check("test-stub-token" not in err, "log never carries the token value")


def http_exc(status: int):
    return discord.HTTPException(SimpleNamespace(status=status, reason="x"), "msg")


outcome, _ = run_with(http_exc(401))
check(outcome == ("exit", 78), f"HTTPException 401 -> exit 78 (got {outcome})")
outcome, _ = run_with(http_exc(503))
check(outcome == ("raised", "HTTPException"), f"HTTPException 503 stays a crash (got {outcome})")
outcome, _ = run_with(RuntimeError("boom"))
check(outcome == ("raised", "RuntimeError"), f"other errors stay crashes (got {outcome})")

print("PASS" if FAILS == 0 else f"{FAILS} FAILED")
raise SystemExit(1 if FAILS else 0)
