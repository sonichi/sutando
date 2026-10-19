"""Keep the host's relay credentials out of a test process.

`src/relay_credentials.py` reads env, the channel env file, the vault and the
repo `.env`; `isolate()` blanks all four for the process and then proves it by
resolving once, so a suite cannot pass or fail on a developer's real token.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from typing import Callable

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO / "src"), str(_REPO / "shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import relay_credentials  # noqa: E402
from ag2_gateway_credentials import (  # noqa: E402
    TOKEN_ALIAS_PRECEDENCE, URL_ALIAS_PRECEDENCE)

# Everything that names a credential or a place one is read from.
RELAY_ENV_KEYS = (*TOKEN_ALIAS_PRECEDENCE, *URL_ALIAS_PRECEDENCE,
                  "AG2_DEVICE_ENV", "REMOTE_TASK_CHANNEL_DIR", "SUTANDO_APP_SUPPORT",
                  "CLAUDE_HOME", "CLAUDE_CONFIG_DIR", "HOME")


def _no_vault_key(key: str) -> str:
    raise KeyError(key)


def redact(value: object) -> str:
    """A secret-free stand-in for an assertion message: presence and length only."""
    if value is None:
        return "<none>"
    return f"<{type(value).__name__} len={len(str(value))}>"


def isolate() -> Callable[[], None]:
    """Blank env, channel env (temp HOME/CLAUDE_CONFIG_DIR), vault and repo `.env`
    for this process; returns the undo. Raises if a credential still resolves."""
    home = tempfile.mkdtemp(prefix="relay-isolation-")
    saved_env = {k: os.environ.get(k) for k in RELAY_ENV_KEYS}
    saved_vault = sys.modules.get("vault_intercept")
    saved_repo = relay_credentials.repo_env_file

    for k in RELAY_ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["HOME"] = home
    os.environ["CLAUDE_CONFIG_DIR"] = str(Path(home) / ".claude")
    vault = types.ModuleType("vault_intercept")
    vault.get_vault_key = _no_vault_key
    vault.list_vault_keys = lambda: []
    sys.modules["vault_intercept"] = vault
    relay_credentials.repo_env_file = lambda: None

    def undo() -> None:
        relay_credentials.repo_env_file = saved_repo
        if saved_vault is None:
            sys.modules.pop("vault_intercept", None)
        else:
            sys.modules["vault_intercept"] = saved_vault
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)

    origin = relay_credentials.resolve_relay_credentials().origin
    if origin != "none":
        undo()
        raise RuntimeError(f"relay isolation leaked a credential from {origin}")
    return undo


def assert_secret_equal(tc, got, want, what: str = "credential") -> None:
    """`tc.assertEqual` for values that may hold a token: the failure names no value."""
    if got != want:
        tc.fail(f"{what} mismatch: got {redact(got)}, want {redact(want)}")


def assert_secret_none(tc, got, what: str = "credential") -> None:
    if got is not None:
        tc.fail(f"{what} unexpectedly resolved: {redact(got)}")
